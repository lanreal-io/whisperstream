//! Tauri packaged-engine adapter for the internal macOS application.

#![cfg(target_os = "macos")]

use crate::engine_http::{new_generation_token, probe_health, request_shutdown, HealthProbe};
use crate::engine_lifecycle_contract::{
    EngineExit, EngineExitDisposition, EngineExitPhase, EngineLifecycleError,
    EngineLifecycleFailure, EngineLifecycleOperation, EngineProcessAdapter, EngineProcessIdentity,
    HEALTH_POLL_INTERVAL,
};
use crate::macos_process_adapter::MacosProcessGroup;
use std::ffi::OsStr;
#[cfg(test)]
use std::ffi::OsString;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};
use tauri::Manager;
use tauri_plugin_shell::ShellExt;

const BUNDLE_RESOURCES_ENV: &str = "WHISPERSTREAM_BUNDLE_RESOURCES_DIR";
const MACOS_STT_WORKER_ENV: &str = "WHISPERSTREAM_MACOS_STT_WORKER_BIN";
pub(crate) const MACOS_ENGINE_SPAWN_MODE_ENV: &str = "WHISPERSTREAM_MACOS_ENGINE_SPAWN_MODE";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum MacosEngineSpawnMode {
    Dictation,
    LifecycleProbe,
}

impl MacosEngineSpawnMode {
    pub(crate) fn from_environment_value(value: Option<&OsStr>) -> Result<Self, ()> {
        match value {
            None => Ok(Self::Dictation),
            Some(value) if value == OsStr::new("lifecycle-probe") => Ok(Self::LifecycleProbe),
            Some(_) => Err(()),
        }
    }
}

fn packaged_engine_arguments(
    mode: MacosEngineSpawnMode,
    owner_pid: u32,
    generation_token: &str,
) -> Vec<String> {
    let owner_pid = owner_pid.to_string();
    match mode {
        MacosEngineSpawnMode::Dictation => vec![
            "--serve".to_string(),
            "--port".to_string(),
            "38420".to_string(),
            "--owner-pid".to_string(),
            owner_pid,
            "--engine-generation".to_string(),
            generation_token.to_string(),
        ],
        MacosEngineSpawnMode::LifecycleProbe => vec![
            "--serve".to_string(),
            "--port".to_string(),
            "38420".to_string(),
            "--no-hotkeys".to_string(),
            "--file-only".to_string(),
            "--lifecycle-probe-worker".to_string(),
            "--owner-pid".to_string(),
            owner_pid,
            "--engine-generation".to_string(),
            generation_token.to_string(),
        ],
    }
}

enum SpawnCommand {
    Packaged(tauri::AppHandle),
    #[cfg(test)]
    Direct {
        program: PathBuf,
        args: Vec<OsString>,
        environment: Vec<(OsString, OsString)>,
    },
}

#[derive(Clone, Copy, Debug)]
struct ObservedExit {
    status_code: Option<i32>,
    signal: Option<i32>,
}

pub struct MacosEngineAdapter {
    command: SpawnCommand,
    spawn_mode: MacosEngineSpawnMode,
    child: Option<Child>,
    process_group: Option<MacosProcessGroup>,
    observed_exit: Option<ObservedExit>,
    generation: u64,
    generation_token: Option<String>,
    termination_requested: bool,
}

impl MacosEngineAdapter {
    pub(crate) fn new(app: tauri::AppHandle, spawn_mode: MacosEngineSpawnMode) -> Self {
        Self {
            command: SpawnCommand::Packaged(app),
            spawn_mode,
            child: None,
            process_group: None,
            observed_exit: None,
            generation: 0,
            generation_token: None,
            termination_requested: false,
        }
    }

    #[cfg(test)]
    fn direct(program: PathBuf, args: &[&str]) -> Self {
        Self {
            command: SpawnCommand::Direct {
                program,
                args: args.iter().map(OsString::from).collect(),
                environment: Vec::new(),
            },
            spawn_mode: MacosEngineSpawnMode::Dictation,
            child: None,
            process_group: None,
            observed_exit: None,
            generation: 0,
            generation_token: None,
            termination_requested: false,
        }
    }

    fn error(
        operation: EngineLifecycleOperation,
        failure: EngineLifecycleFailure,
    ) -> EngineLifecycleError {
        EngineLifecycleError { operation, failure }
    }

    fn packaged_transport_or<T>(&self, _unavailable: T, operation: impl FnOnce() -> T) -> T {
        // Direct commands exist only in tests and must never contact an app's
        // shared backend port. Their real child/group lifecycle stays intact.
        #[cfg(test)]
        if matches!(&self.command, SpawnCommand::Direct { .. }) {
            return _unavailable;
        }
        operation()
    }

    fn build_command(&self, generation_token: &str) -> Result<Command, EngineLifecycleError> {
        let operation = EngineLifecycleOperation::Spawn;
        match &self.command {
            SpawnCommand::Packaged(app) => {
                let resource_dir = app
                    .path()
                    .resource_dir()
                    .map_err(|_| Self::error(operation, EngineLifecycleFailure::SpawnFailed))?
                    .join("resources");
                let executable_dir = std::env::current_exe()
                    .ok()
                    .and_then(|path| path.parent().map(PathBuf::from))
                    .ok_or_else(|| Self::error(operation, EngineLifecycleFailure::SpawnFailed))?;
                let worker = executable_dir.join("whisperstream-macos-stt-worker");
                if !resource_dir.is_dir() || !worker.is_file() {
                    return Err(Self::error(
                        operation,
                        EngineLifecycleFailure::MissingBinary,
                    ));
                }
                let sidecar = app
                    .shell()
                    .sidecar("whisperstream-engine")
                    .map_err(|_| Self::error(operation, EngineLifecycleFailure::MissingBinary))?
                    .args(packaged_engine_arguments(
                        self.spawn_mode,
                        std::process::id(),
                        generation_token,
                    ))
                    .env(BUNDLE_RESOURCES_ENV, resource_dir)
                    .env(MACOS_STT_WORKER_ENV, worker);
                Ok(sidecar.into())
            }
            #[cfg(test)]
            SpawnCommand::Direct {
                program,
                args,
                environment,
            } => {
                if !program.is_file() {
                    return Err(Self::error(
                        operation,
                        EngineLifecycleFailure::MissingBinary,
                    ));
                }
                let mut command = Command::new(program);
                command.args(args).envs(environment.iter().cloned());
                Ok(command)
            }
        }
    }

    fn refresh_exit(&mut self) -> Result<bool, ()> {
        if self.observed_exit.is_some() {
            return Ok(true);
        }
        if let Some(child) = self.child.as_mut() {
            if let Some(status) = child.try_wait().map_err(|_| ())? {
                self.observed_exit = Some(ObservedExit {
                    status_code: status.code(),
                    signal: status.signal(),
                });
                return Ok(true);
            }
        }
        if self
            .process_group
            .as_ref()
            .is_some_and(|group| group.root_is_running().ok() == Some(false))
        {
            return Ok(true);
        }
        Ok(false)
    }

    fn record_lineage(
        &mut self,
        operation: EngineLifecycleOperation,
    ) -> Result<(), EngineLifecycleError> {
        let Some(group) = self.process_group.as_mut() else {
            return Err(Self::error(
                operation,
                EngineLifecycleFailure::EscalationFailed,
            ));
        };
        group
            .record_lineage()
            .map(|_| ())
            .map_err(|_| Self::error(operation, EngineLifecycleFailure::EscalationFailed))
    }

    fn wait_for_cleanup(
        &mut self,
        timeout: Duration,
        operation: EngineLifecycleOperation,
        failure: EngineLifecycleFailure,
    ) -> Result<(), EngineLifecycleError> {
        let started = Instant::now();
        loop {
            let _ = self.refresh_exit();
            let Some(group) = self.process_group.as_mut() else {
                return Err(Self::error(operation, failure));
            };
            let slice = CLEANUP_SLICE.min(timeout.saturating_sub(started.elapsed()));
            if group.wait_for_cleanup(slice).is_ok() {
                let _ = self.refresh_exit();
                return Ok(());
            }
            if started.elapsed() >= timeout {
                return Err(Self::error(operation, failure));
            }
        }
    }
}

const CLEANUP_SLICE: Duration = Duration::from_millis(100);

impl EngineProcessAdapter for MacosEngineAdapter {
    fn spawn(&mut self, timeout: Duration) -> Result<EngineProcessIdentity, EngineLifecycleError> {
        let operation = EngineLifecycleOperation::Spawn;
        let started = Instant::now();
        let next_generation = self.generation + 1;
        let generation_token = new_generation_token(next_generation);
        let mut command = self.build_command(&generation_token)?;
        if !Path::new(command.get_program()).is_file() {
            return Err(Self::error(
                operation,
                EngineLifecycleFailure::MissingBinary,
            ));
        }
        command
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        let mut child = command.spawn().map_err(|error| {
            let failure = if error.kind() == std::io::ErrorKind::NotFound {
                EngineLifecycleFailure::MissingBinary
            } else {
                EngineLifecycleFailure::SpawnFailed
            };
            Self::error(operation, failure)
        })?;
        let pid = child.id();
        if started.elapsed() > timeout {
            let _ = child.kill();
            let _ = child.wait();
            return Err(Self::error(operation, EngineLifecycleFailure::SpawnFailed));
        }
        let process_group = match MacosProcessGroup::new(pid) {
            Ok(group) => group,
            Err(()) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(Self::error(operation, EngineLifecycleFailure::SpawnFailed));
            }
        };

        self.generation = next_generation;
        self.generation_token = Some(generation_token);
        self.termination_requested = false;
        self.observed_exit = None;
        self.process_group = Some(process_group);
        self.child = Some(child);
        Ok(EngineProcessIdentity {
            pid,
            parent_pid: Some(std::process::id()),
            generation: self.generation,
        })
    }

    fn await_ready(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError> {
        let operation = EngineLifecycleOperation::AwaitReady;
        let started = Instant::now();
        loop {
            let _ = self.record_lineage(operation);
            if self.refresh_exit().unwrap_or(true) {
                return Err(Self::error(
                    operation,
                    EngineLifecycleFailure::ExitedBeforeReady,
                ));
            }
            let health = self.packaged_transport_or(HealthProbe::NotReady, || {
                probe_health(HEALTH_POLL_INTERVAL, self.generation_token.as_deref())
            });
            match health {
                HealthProbe::Ready => return Ok(()),
                HealthProbe::InvalidResponse => {
                    return Err(Self::error(
                        operation,
                        EngineLifecycleFailure::HealthProbeFailed,
                    ));
                }
                HealthProbe::NotReady => {}
            }
            if started.elapsed() >= timeout {
                return Err(Self::error(
                    operation,
                    EngineLifecycleFailure::ReadinessTimeout,
                ));
            }
            thread::sleep(HEALTH_POLL_INTERVAL);
        }
    }

    fn request_graceful_shutdown(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError> {
        let operation = EngineLifecycleOperation::RequestGracefulShutdown;
        self.record_lineage(operation)?;
        let _ = self.packaged_transport_or(false, || {
            request_shutdown(HEALTH_POLL_INTERVAL, self.generation_token.as_deref())
        });
        self.wait_for_cleanup(
            timeout,
            operation,
            EngineLifecycleFailure::GracefulShutdownTimeout,
        )
    }

    fn terminate(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError> {
        let operation = EngineLifecycleOperation::Terminate;
        self.termination_requested = true;
        self.record_lineage(operation)?;
        let Some(group) = self.process_group.as_mut() else {
            return Err(Self::error(
                operation,
                EngineLifecycleFailure::EscalationFailed,
            ));
        };
        group
            .terminate(timeout)
            .map_err(|_| Self::error(operation, EngineLifecycleFailure::EscalationFailed))?;
        let _ = self.refresh_exit();
        Ok(())
    }

    fn force_kill(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError> {
        let operation = EngineLifecycleOperation::ForceKill;
        self.termination_requested = true;
        self.record_lineage(operation)?;
        let Some(group) = self.process_group.as_mut() else {
            return Err(Self::error(
                operation,
                EngineLifecycleFailure::EscalationFailed,
            ));
        };
        group
            .force_kill(timeout)
            .map_err(|_| Self::error(operation, EngineLifecycleFailure::EscalationFailed))?;
        let _ = self.refresh_exit();
        Ok(())
    }

    fn verify_cleanup(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError> {
        self.wait_for_cleanup(
            timeout,
            EngineLifecycleOperation::VerifyCleanup,
            EngineLifecycleFailure::CleanupTimeout,
        )
    }

    fn classify_exit(&self, phase: EngineExitPhase) -> EngineExit {
        let status_code = self.observed_exit.and_then(|exit| exit.status_code);
        let disposition = if self.termination_requested {
            EngineExitDisposition::Terminated
        } else {
            match self.observed_exit {
                Some(ObservedExit {
                    status_code: Some(0),
                    signal: None,
                }) => EngineExitDisposition::Clean,
                Some(_) => EngineExitDisposition::Failed,
                None => EngineExitDisposition::Unknown,
            }
        };
        EngineExit {
            phase,
            disposition,
            status_code,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine_lifecycle_contract::{
        EngineLifecycle, EngineLifecycleState, EngineShutdownReason, EngineShutdownStage,
    };
    use std::ffi::OsStr;
    use std::os::unix::ffi::OsStrExt;

    #[test]
    fn packaged_spawn_mode_selector_is_strict() {
        assert_eq!(
            MacosEngineSpawnMode::from_environment_value(None),
            Ok(MacosEngineSpawnMode::Dictation),
        );
        assert_eq!(
            MacosEngineSpawnMode::from_environment_value(Some(OsStr::new("lifecycle-probe"))),
            Ok(MacosEngineSpawnMode::LifecycleProbe),
        );
        assert_eq!(
            MacosEngineSpawnMode::from_environment_value(Some(OsStr::new(""))),
            Err(()),
        );
        assert_eq!(
            MacosEngineSpawnMode::from_environment_value(Some(OsStr::new("dictation"))),
            Err(()),
        );
        assert_eq!(
            MacosEngineSpawnMode::from_environment_value(Some(OsStr::from_bytes(
                b"lifecycle-probe\xff",
            ))),
            Err(()),
        );
    }

    #[test]
    fn lifecycle_probe_arguments_are_byte_identical_to_phase_two() {
        let arguments = packaged_engine_arguments(
            MacosEngineSpawnMode::LifecycleProbe,
            4242,
            "generation-token",
        );
        let actual: Vec<&[u8]> = arguments
            .iter()
            .map(|argument| argument.as_bytes())
            .collect();

        assert_eq!(
            actual,
            vec![
                b"--serve".as_slice(),
                b"--port".as_slice(),
                b"38420".as_slice(),
                b"--no-hotkeys".as_slice(),
                b"--file-only".as_slice(),
                b"--lifecycle-probe-worker".as_slice(),
                b"--owner-pid".as_slice(),
                b"4242".as_slice(),
                b"--engine-generation".as_slice(),
                b"generation-token".as_slice(),
            ],
        );
    }

    #[test]
    fn dictation_arguments_are_exact_and_exclude_probe_flags() {
        let arguments =
            packaged_engine_arguments(MacosEngineSpawnMode::Dictation, 4242, "generation-token");

        assert_eq!(
            arguments,
            [
                "--serve",
                "--port",
                "38420",
                "--owner-pid",
                "4242",
                "--engine-generation",
                "generation-token",
            ],
        );
        for probe_flag in ["--no-hotkeys", "--file-only", "--lifecycle-probe-worker"] {
            assert!(!arguments.iter().any(|argument| argument == probe_flag));
        }
    }

    #[test]
    fn direct_fixture_never_invokes_packaged_http_transport() {
        let direct = MacosEngineAdapter::direct(PathBuf::from("/usr/bin/false"), &[]);
        assert_eq!(
            direct.packaged_transport_or(HealthProbe::NotReady, || {
                panic!("direct fixture must not invoke health HTTP")
            }),
            HealthProbe::NotReady,
        );
        assert!(!direct.packaged_transport_or(false, || {
            panic!("direct fixture must not invoke shutdown HTTP")
        }));
    }

    #[test]
    fn missing_and_early_exit_are_classified_without_native_detail() {
        let mut missing = EngineLifecycle::new(MacosEngineAdapter::direct(
            PathBuf::from("/private/tmp/whisperstream-unit02-missing-engine"),
            &[],
        ));
        let error = missing.spawn().expect_err("missing engine should fail");
        assert_eq!(error.operation, EngineLifecycleOperation::Spawn);
        assert_eq!(error.failure, EngineLifecycleFailure::MissingBinary);
        assert_eq!(missing.state(), EngineLifecycleState::Exited);

        let mut early = EngineLifecycle::new(MacosEngineAdapter::direct(
            PathBuf::from("/usr/bin/false"),
            &[],
        ));
        early.spawn().expect("false should spawn");
        let error = early
            .await_ready()
            .expect_err("false should exit before ready");
        assert_eq!(error.failure, EngineLifecycleFailure::ExitedBeforeReady);
        let exit = early.exit().expect("early exit should be retained");
        assert_eq!(exit.phase, EngineExitPhase::BeforeReady);
        assert_eq!(exit.disposition, EngineExitDisposition::Failed);
        assert_eq!(exit.status_code, Some(1));
        early
            .shutdown(EngineShutdownReason::AppExit)
            .expect("early process cleanup should verify");
    }

    #[test]
    fn hung_shutdown_crosses_term_and_finishes_at_force_kill() {
        let mut lifecycle = EngineLifecycle::new(MacosEngineAdapter::direct(
            PathBuf::from("/bin/sh"),
            &["-c", "trap '' TERM; /bin/sleep 30 & wait"],
        ));
        let identity = lifecycle.spawn().expect("hung group should spawn");
        let started = Instant::now();
        let report = lifecycle
            .shutdown(EngineShutdownReason::AppExit)
            .expect("force escalation should clean the exact group");
        let elapsed = started.elapsed();
        assert_eq!(report.stage, EngineShutdownStage::ForceKill);
        assert!(report.cleanup_verified);
        assert!(elapsed >= Duration::from_secs(8));
        assert!(elapsed < Duration::from_secs(13));
        eprintln!(
            "hung_shutdown pid={} elapsed_ms={:.3} stage=force_kill cleanup_verified=true",
            identity.pid,
            elapsed.as_secs_f64() * 1_000.0,
        );
    }
}
