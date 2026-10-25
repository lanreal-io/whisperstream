//! Platform-neutral ownership contract for the packaged Python engine.
//!
//! Windows process-tree handling and POSIX signals belong in adapters that
//! implement `EngineProcessAdapter`. This module owns only the shared state
//! machine, operation deadlines, escalation order, and content-free results.

use std::time::Duration;

pub const SPAWN_TIMEOUT: Duration = Duration::from_secs(5);
pub const READINESS_TIMEOUT: Duration = Duration::from_secs(30);
pub const GRACEFUL_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);
pub const TERMINATE_TIMEOUT: Duration = Duration::from_secs(3);
pub const FORCE_KILL_TIMEOUT: Duration = Duration::from_secs(3);
pub const CLEANUP_TIMEOUT: Duration = Duration::from_secs(5);
pub const HEALTH_POLL_INTERVAL: Duration = Duration::from_millis(100);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EngineLifecycleState {
    Stopped,
    Starting,
    Ready,
    Stopping,
    Exited,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EngineLifecycleOperation {
    Spawn,
    AwaitReady,
    RequestGracefulShutdown,
    Terminate,
    ForceKill,
    VerifyCleanup,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EngineLifecycleFailure {
    InvalidTransition,
    MissingBinary,
    SpawnFailed,
    ExitedBeforeReady,
    ReadinessTimeout,
    HealthProbeFailed,
    GracefulShutdownTimeout,
    EscalationFailed,
    CleanupTimeout,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EngineLifecycleError {
    pub operation: EngineLifecycleOperation,
    pub failure: EngineLifecycleFailure,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EngineExitPhase {
    BeforeReady,
    Running,
    ShuttingDown,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EngineExitDisposition {
    Clean,
    Failed,
    Terminated,
    Unknown,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EngineShutdownReason {
    AppExit,
    Update,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EngineShutdownStage {
    AlreadyExited,
    Graceful,
    Terminate,
    ForceKill,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EngineProcessIdentity {
    pub pid: u32,
    pub parent_pid: Option<u32>,
    pub generation: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EngineExit {
    pub phase: EngineExitPhase,
    pub disposition: EngineExitDisposition,
    pub status_code: Option<i32>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EngineShutdownReport {
    pub reason: EngineShutdownReason,
    pub stage: EngineShutdownStage,
    pub exit: EngineExit,
    pub cleanup_verified: bool,
}

pub fn transition_allowed(from: EngineLifecycleState, to: EngineLifecycleState) -> bool {
    use EngineLifecycleState::{Exited, Ready, Starting, Stopped, Stopping};
    matches!(
        (from, to),
        (Stopped, Starting)
            | (Starting, Ready)
            | (Starting, Stopping)
            | (Starting, Exited)
            | (Ready, Stopping)
            | (Ready, Exited)
            | (Stopping, Exited)
            | (Exited, Stopped)
    )
}

pub trait EngineProcessAdapter {
    fn spawn(&mut self, timeout: Duration) -> Result<EngineProcessIdentity, EngineLifecycleError>;

    fn await_ready(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError>;

    fn request_graceful_shutdown(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError>;

    fn terminate(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError>;

    fn force_kill(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError>;

    fn verify_cleanup(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError>;

    fn classify_exit(&self, phase: EngineExitPhase) -> EngineExit;
}

pub struct EngineLifecycle<A: EngineProcessAdapter> {
    adapter: A,
    state: EngineLifecycleState,
    identity: Option<EngineProcessIdentity>,
    exit: Option<EngineExit>,
}

impl<A: EngineProcessAdapter> EngineLifecycle<A> {
    pub fn new(adapter: A) -> Self {
        Self {
            adapter,
            state: EngineLifecycleState::Stopped,
            identity: None,
            exit: None,
        }
    }

    pub fn state(&self) -> EngineLifecycleState {
        self.state
    }

    pub fn identity(&self) -> Option<EngineProcessIdentity> {
        self.identity
    }

    pub fn exit(&self) -> Option<EngineExit> {
        self.exit
    }

    pub fn spawn(&mut self) -> Result<EngineProcessIdentity, EngineLifecycleError> {
        self.transition(
            EngineLifecycleState::Starting,
            EngineLifecycleOperation::Spawn,
        )?;
        match self.adapter.spawn(SPAWN_TIMEOUT) {
            Ok(identity) => {
                self.identity = Some(identity);
                Ok(identity)
            }
            Err(error) => {
                self.state = EngineLifecycleState::Exited;
                self.exit = None;
                Err(error)
            }
        }
    }

    pub fn await_ready(&mut self) -> Result<(), EngineLifecycleError> {
        if self.state != EngineLifecycleState::Starting {
            return Err(invalid_transition(EngineLifecycleOperation::AwaitReady));
        }
        match self.adapter.await_ready(READINESS_TIMEOUT) {
            Ok(()) => {
                self.transition(
                    EngineLifecycleState::Ready,
                    EngineLifecycleOperation::AwaitReady,
                )?;
                Ok(())
            }
            Err(error) => {
                if error.failure == EngineLifecycleFailure::ExitedBeforeReady {
                    self.state = EngineLifecycleState::Exited;
                    self.exit = Some(self.adapter.classify_exit(EngineExitPhase::BeforeReady));
                }
                Err(error)
            }
        }
    }

    pub fn reset_after_exit(&mut self) -> Result<(), EngineLifecycleError> {
        self.transition(
            EngineLifecycleState::Stopped,
            EngineLifecycleOperation::Spawn,
        )?;
        self.identity = None;
        self.exit = None;
        Ok(())
    }

    pub fn shutdown(
        &mut self,
        reason: EngineShutdownReason,
    ) -> Result<EngineShutdownReport, EngineLifecycleError> {
        if self.state == EngineLifecycleState::Exited {
            // An exited bootstrap process can leave owned descendants alive.
            // Root exit is not proof that the recorded process tree is gone.
            if self.adapter.verify_cleanup(CLEANUP_TIMEOUT).is_err() {
                self.adapter.force_kill(FORCE_KILL_TIMEOUT)?;
                self.adapter.verify_cleanup(CLEANUP_TIMEOUT)?;
                return Ok(self.finish_shutdown(
                    reason,
                    EngineShutdownStage::ForceKill,
                    self.exit.map_or(EngineExitPhase::ShuttingDown, |exit| exit.phase),
                ));
            }
            let exit = self
                .exit
                .unwrap_or_else(|| self.adapter.classify_exit(EngineExitPhase::ShuttingDown));
            return Ok(EngineShutdownReport {
                reason,
                stage: EngineShutdownStage::AlreadyExited,
                exit,
                cleanup_verified: true,
            });
        }
        if !matches!(
            self.state,
            EngineLifecycleState::Starting | EngineLifecycleState::Ready
        ) {
            return Err(invalid_transition(
                EngineLifecycleOperation::RequestGracefulShutdown,
            ));
        }
        self.transition(
            EngineLifecycleState::Stopping,
            EngineLifecycleOperation::RequestGracefulShutdown,
        )?;

        if self
            .adapter
            .request_graceful_shutdown(GRACEFUL_SHUTDOWN_TIMEOUT)
            .is_ok()
            && self.adapter.verify_cleanup(CLEANUP_TIMEOUT).is_ok()
        {
            return Ok(self.finish_shutdown(
                reason,
                EngineShutdownStage::Graceful,
                EngineExitPhase::ShuttingDown,
            ));
        }

        if self.adapter.terminate(TERMINATE_TIMEOUT).is_ok()
            && self.adapter.verify_cleanup(CLEANUP_TIMEOUT).is_ok()
        {
            return Ok(self.finish_shutdown(
                reason,
                EngineShutdownStage::Terminate,
                EngineExitPhase::ShuttingDown,
            ));
        }

        self.adapter.force_kill(FORCE_KILL_TIMEOUT)?;
        self.adapter.verify_cleanup(CLEANUP_TIMEOUT)?;
        Ok(self.finish_shutdown(
            reason,
            EngineShutdownStage::ForceKill,
            EngineExitPhase::ShuttingDown,
        ))
    }

    fn transition(
        &mut self,
        to: EngineLifecycleState,
        operation: EngineLifecycleOperation,
    ) -> Result<(), EngineLifecycleError> {
        if !transition_allowed(self.state, to) {
            return Err(invalid_transition(operation));
        }
        self.state = to;
        Ok(())
    }

    fn finish_shutdown(
        &mut self,
        reason: EngineShutdownReason,
        stage: EngineShutdownStage,
        phase: EngineExitPhase,
    ) -> EngineShutdownReport {
        self.state = EngineLifecycleState::Exited;
        let exit = self.adapter.classify_exit(phase);
        self.exit = Some(exit);
        EngineShutdownReport {
            reason,
            stage,
            exit,
            cleanup_verified: true,
        }
    }
}

fn invalid_transition(operation: EngineLifecycleOperation) -> EngineLifecycleError {
    EngineLifecycleError {
        operation,
        failure: EngineLifecycleFailure::InvalidTransition,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    struct FakeAdapter {
        generation: u64,
        calls: Vec<EngineLifecycleOperation>,
        spawn_results: VecDeque<Result<(), EngineLifecycleFailure>>,
        ready_results: VecDeque<Result<(), EngineLifecycleFailure>>,
        graceful_results: VecDeque<Result<(), EngineLifecycleFailure>>,
        terminate_results: VecDeque<Result<(), EngineLifecycleFailure>>,
        force_results: VecDeque<Result<(), EngineLifecycleFailure>>,
        cleanup_results: VecDeque<Result<(), EngineLifecycleFailure>>,
        status_code: Option<i32>,
        terminated: bool,
    }

    impl Default for FakeAdapter {
        fn default() -> Self {
            Self {
                generation: 0,
                calls: Vec::new(),
                spawn_results: VecDeque::new(),
                ready_results: VecDeque::new(),
                graceful_results: VecDeque::new(),
                terminate_results: VecDeque::new(),
                force_results: VecDeque::new(),
                cleanup_results: VecDeque::new(),
                status_code: Some(0),
                terminated: false,
            }
        }
    }

    impl FakeAdapter {
        fn result(
            queue: &mut VecDeque<Result<(), EngineLifecycleFailure>>,
            operation: EngineLifecycleOperation,
        ) -> Result<(), EngineLifecycleError> {
            queue
                .pop_front()
                .unwrap_or(Ok(()))
                .map_err(|failure| EngineLifecycleError { operation, failure })
        }
    }

    impl EngineProcessAdapter for FakeAdapter {
        fn spawn(
            &mut self,
            _timeout: Duration,
        ) -> Result<EngineProcessIdentity, EngineLifecycleError> {
            self.calls.push(EngineLifecycleOperation::Spawn);
            Self::result(&mut self.spawn_results, EngineLifecycleOperation::Spawn)?;
            self.generation += 1;
            Ok(EngineProcessIdentity {
                pid: 4_100 + self.generation as u32,
                parent_pid: Some(4_000),
                generation: self.generation,
            })
        }

        fn await_ready(&mut self, _timeout: Duration) -> Result<(), EngineLifecycleError> {
            self.calls.push(EngineLifecycleOperation::AwaitReady);
            Self::result(
                &mut self.ready_results,
                EngineLifecycleOperation::AwaitReady,
            )
        }

        fn request_graceful_shutdown(
            &mut self,
            _timeout: Duration,
        ) -> Result<(), EngineLifecycleError> {
            self.calls
                .push(EngineLifecycleOperation::RequestGracefulShutdown);
            Self::result(
                &mut self.graceful_results,
                EngineLifecycleOperation::RequestGracefulShutdown,
            )
        }

        fn terminate(&mut self, _timeout: Duration) -> Result<(), EngineLifecycleError> {
            self.calls.push(EngineLifecycleOperation::Terminate);
            self.terminated = true;
            Self::result(
                &mut self.terminate_results,
                EngineLifecycleOperation::Terminate,
            )
        }

        fn force_kill(&mut self, _timeout: Duration) -> Result<(), EngineLifecycleError> {
            self.calls.push(EngineLifecycleOperation::ForceKill);
            self.terminated = true;
            Self::result(&mut self.force_results, EngineLifecycleOperation::ForceKill)
        }

        fn verify_cleanup(&mut self, _timeout: Duration) -> Result<(), EngineLifecycleError> {
            self.calls.push(EngineLifecycleOperation::VerifyCleanup);
            Self::result(
                &mut self.cleanup_results,
                EngineLifecycleOperation::VerifyCleanup,
            )
        }

        fn classify_exit(&self, phase: EngineExitPhase) -> EngineExit {
            EngineExit {
                phase,
                disposition: if self.terminated {
                    EngineExitDisposition::Terminated
                } else if self.status_code == Some(0) {
                    EngineExitDisposition::Clean
                } else {
                    EngineExitDisposition::Failed
                },
                status_code: self.status_code,
            }
        }
    }

    #[test]
    fn lifecycle_transition_table_is_closed() {
        let states = [
            EngineLifecycleState::Stopped,
            EngineLifecycleState::Starting,
            EngineLifecycleState::Ready,
            EngineLifecycleState::Stopping,
            EngineLifecycleState::Exited,
        ];
        let allowed = states
            .into_iter()
            .flat_map(|from| states.into_iter().map(move |to| (from, to)))
            .filter(|(from, to)| transition_allowed(*from, *to))
            .collect::<Vec<_>>();
        assert_eq!(allowed.len(), 8);
        assert!(allowed.contains(&(EngineLifecycleState::Ready, EngineLifecycleState::Stopping)));
        assert!(!transition_allowed(
            EngineLifecycleState::Stopped,
            EngineLifecycleState::Ready
        ));
        assert!(!transition_allowed(
            EngineLifecycleState::Exited,
            EngineLifecycleState::Starting
        ));
    }

    #[test]
    fn escalation_timeouts_are_finite_and_ordered() {
        assert_eq!(SPAWN_TIMEOUT, Duration::from_secs(5));
        assert_eq!(READINESS_TIMEOUT, Duration::from_secs(30));
        assert_eq!(GRACEFUL_SHUTDOWN_TIMEOUT, Duration::from_secs(5));
        assert_eq!(TERMINATE_TIMEOUT, Duration::from_secs(3));
        assert_eq!(FORCE_KILL_TIMEOUT, Duration::from_secs(3));
        assert_eq!(CLEANUP_TIMEOUT, Duration::from_secs(5));
        assert!(HEALTH_POLL_INTERVAL < GRACEFUL_SHUTDOWN_TIMEOUT);
    }

    #[test]
    fn operation_and_failure_vocabularies_are_content_free() {
        let operations = [
            EngineLifecycleOperation::Spawn,
            EngineLifecycleOperation::AwaitReady,
            EngineLifecycleOperation::RequestGracefulShutdown,
            EngineLifecycleOperation::Terminate,
            EngineLifecycleOperation::ForceKill,
            EngineLifecycleOperation::VerifyCleanup,
        ];
        let failures = [
            EngineLifecycleFailure::InvalidTransition,
            EngineLifecycleFailure::MissingBinary,
            EngineLifecycleFailure::SpawnFailed,
            EngineLifecycleFailure::ExitedBeforeReady,
            EngineLifecycleFailure::ReadinessTimeout,
            EngineLifecycleFailure::HealthProbeFailed,
            EngineLifecycleFailure::GracefulShutdownTimeout,
            EngineLifecycleFailure::EscalationFailed,
            EngineLifecycleFailure::CleanupTimeout,
        ];
        assert_eq!(operations.len(), 6);
        assert_eq!(failures.len(), 9);
        assert_eq!(
            EngineLifecycleError {
                operation: operations[1],
                failure: failures[4],
            },
            EngineLifecycleError {
                operation: EngineLifecycleOperation::AwaitReady,
                failure: EngineLifecycleFailure::ReadinessTimeout,
            }
        );
    }

    #[test]
    fn identity_and_exit_data_are_platform_neutral() {
        let identity = EngineProcessIdentity {
            pid: 4100,
            parent_pid: Some(4000),
            generation: 2,
        };
        let exit = EngineExit {
            phase: EngineExitPhase::ShuttingDown,
            disposition: EngineExitDisposition::Clean,
            status_code: Some(0),
        };
        assert_eq!(identity.pid, 4100);
        assert_eq!(exit.status_code, Some(0));
    }

    #[test]
    fn normal_start_and_app_quit_are_graceful() {
        let mut lifecycle = EngineLifecycle::new(FakeAdapter::default());
        let identity = lifecycle.spawn().expect("spawn should pass");
        lifecycle.await_ready().expect("readiness should pass");
        let report = lifecycle
            .shutdown(EngineShutdownReason::AppExit)
            .expect("shutdown should pass");

        assert_eq!(identity.generation, 1);
        assert_eq!(lifecycle.state(), EngineLifecycleState::Exited);
        assert_eq!(report.reason, EngineShutdownReason::AppExit);
        assert_eq!(report.stage, EngineShutdownStage::Graceful);
        assert!(report.cleanup_verified);
    }

    #[test]
    fn missing_sidecar_is_a_bounded_spawn_failure() {
        let mut adapter = FakeAdapter::default();
        adapter
            .spawn_results
            .push_back(Err(EngineLifecycleFailure::MissingBinary));
        let mut lifecycle = EngineLifecycle::new(adapter);

        let error = lifecycle.spawn().expect_err("missing binary must fail");
        assert_eq!(error.operation, EngineLifecycleOperation::Spawn);
        assert_eq!(error.failure, EngineLifecycleFailure::MissingBinary);
        assert_eq!(lifecycle.state(), EngineLifecycleState::Exited);
        assert_eq!(lifecycle.identity(), None);
    }

    #[test]
    fn early_root_exit_still_escalates_owned_descendant_cleanup() {
        let mut adapter = FakeAdapter::default();
        adapter.ready_results.push_back(Err(EngineLifecycleFailure::ExitedBeforeReady));
        adapter.cleanup_results.push_back(Err(EngineLifecycleFailure::CleanupTimeout));
        let mut lifecycle = EngineLifecycle::new(adapter);
        lifecycle.spawn().unwrap();
        lifecycle.await_ready().unwrap_err();
        let report = lifecycle.shutdown(EngineShutdownReason::AppExit).unwrap();
        assert_eq!(report.stage, EngineShutdownStage::ForceKill);
        assert_eq!(report.exit.phase, EngineExitPhase::BeforeReady);
        assert!(report.cleanup_verified);
        assert_eq!(&lifecycle.adapter.calls[2..], &[
            EngineLifecycleOperation::VerifyCleanup,
            EngineLifecycleOperation::ForceKill,
            EngineLifecycleOperation::VerifyCleanup,
        ]);
    }

    #[test]
    fn early_exit_before_readiness_is_classified() {
        let mut adapter = FakeAdapter::default();
        adapter
            .ready_results
            .push_back(Err(EngineLifecycleFailure::ExitedBeforeReady));
        adapter.status_code = Some(17);
        let mut lifecycle = EngineLifecycle::new(adapter);
        lifecycle.spawn().expect("spawn should pass");

        let error = lifecycle
            .await_ready()
            .expect_err("early exit must fail readiness");
        assert_eq!(error.failure, EngineLifecycleFailure::ExitedBeforeReady);
        assert_eq!(lifecycle.state(), EngineLifecycleState::Exited);
        assert_eq!(
            lifecycle.exit(),
            Some(EngineExit {
                phase: EngineExitPhase::BeforeReady,
                disposition: EngineExitDisposition::Failed,
                status_code: Some(17),
            })
        );
    }

    #[test]
    fn hung_graceful_shutdown_escalates_in_bounded_order() {
        let mut adapter = FakeAdapter::default();
        adapter
            .graceful_results
            .push_back(Err(EngineLifecycleFailure::GracefulShutdownTimeout));
        adapter
            .terminate_results
            .push_back(Err(EngineLifecycleFailure::EscalationFailed));
        let mut lifecycle = EngineLifecycle::new(adapter);
        lifecycle.spawn().expect("spawn should pass");
        lifecycle.await_ready().expect("readiness should pass");

        let report = lifecycle
            .shutdown(EngineShutdownReason::Update)
            .expect("force escalation should pass");
        assert_eq!(report.reason, EngineShutdownReason::Update);
        assert_eq!(report.stage, EngineShutdownStage::ForceKill);
        assert_eq!(report.exit.disposition, EngineExitDisposition::Terminated);
        assert_eq!(lifecycle.state(), EngineLifecycleState::Exited);
    }

    #[test]
    fn repeated_lifecycle_requires_reset_and_increments_generation() {
        let mut lifecycle = EngineLifecycle::new(FakeAdapter::default());
        for expected_generation in 1..=2 {
            let identity = lifecycle.spawn().expect("spawn should pass");
            assert_eq!(identity.generation, expected_generation);
            lifecycle.await_ready().expect("readiness should pass");
            lifecycle
                .shutdown(EngineShutdownReason::AppExit)
                .expect("shutdown should pass");
            if expected_generation == 1 {
                lifecycle
                    .reset_after_exit()
                    .expect("explicit reset should pass");
            }
        }
        assert_eq!(lifecycle.state(), EngineLifecycleState::Exited);
    }
}
