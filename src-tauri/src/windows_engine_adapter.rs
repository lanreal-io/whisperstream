//! Tauri packaged-engine adapter for Windows.

#![cfg(target_os = "windows")]

use crate::engine_http::{new_generation_token, probe_health, request_shutdown, HealthProbe};
use crate::engine_lifecycle_contract::{
    EngineExit, EngineExitDisposition, EngineExitPhase, EngineLifecycleError,
    EngineLifecycleFailure, EngineLifecycleOperation, EngineProcessAdapter, EngineProcessIdentity,
    HEALTH_POLL_INTERVAL,
};
use crate::windows_process_adapter::WindowsProcessTree;
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

pub struct WindowsEngineAdapter {
    app: tauri::AppHandle,
    child: Option<CommandChild>,
    process_tree: Option<WindowsProcessTree>,
    exit_status: Arc<Mutex<Option<i32>>>,
    generation: u64,
    generation_token: Option<String>,
    termination_requested: bool,
}

impl WindowsEngineAdapter {
    pub fn new(app: tauri::AppHandle) -> Self {
        Self {
            app,
            child: None,
            process_tree: None,
            exit_status: Arc::new(Mutex::new(None)),
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

    fn has_exited(&self) -> bool {
        if self
            .exit_status
            .lock()
            .ok()
            .and_then(|status| *status)
            .is_some()
        {
            return true;
        }
        self.process_tree
            .as_ref()
            .and_then(|tree| tree.root_is_running().ok())
            == Some(false)
    }

    fn record_lineage(
        &mut self,
        operation: EngineLifecycleOperation,
    ) -> Result<(), EngineLifecycleError> {
        let Some(tree) = self.process_tree.as_mut() else {
            return Err(Self::error(
                operation,
                EngineLifecycleFailure::EscalationFailed,
            ));
        };
        tree.record_lineage()
            .map(|_| ())
            .map_err(|_| Self::error(operation, EngineLifecycleFailure::EscalationFailed))
    }

    fn wait_for_cleanup(
        &self,
        timeout: Duration,
        operation: EngineLifecycleOperation,
        failure: EngineLifecycleFailure,
    ) -> Result<(), EngineLifecycleError> {
        let Some(tree) = self.process_tree.as_ref() else {
            return Err(Self::error(operation, failure));
        };
        tree.wait_for_cleanup(timeout)
            .map_err(|_| Self::error(operation, failure))
    }
}

impl EngineProcessAdapter for WindowsEngineAdapter {
    fn spawn(&mut self, timeout: Duration) -> Result<EngineProcessIdentity, EngineLifecycleError> {
        let started = Instant::now();
        let next_generation = self.generation + 1;
        let generation_token = new_generation_token(next_generation);
        let sidecar = self
            .app
            .shell()
            .sidecar("whisperstream-engine")
            .map_err(|_| {
                Self::error(
                    EngineLifecycleOperation::Spawn,
                    EngineLifecycleFailure::MissingBinary,
                )
            })?;
        let (mut rx, child) = sidecar
            .args([
                "--serve",
                "--port",
                "38420",
                "--engine-generation",
                &generation_token,
            ])
            .spawn()
            .map_err(|_| {
                Self::error(
                    EngineLifecycleOperation::Spawn,
                    EngineLifecycleFailure::SpawnFailed,
                )
            })?;
        if started.elapsed() > timeout {
            let _ = child.kill();
            return Err(Self::error(
                EngineLifecycleOperation::Spawn,
                EngineLifecycleFailure::SpawnFailed,
            ));
        }

        self.generation = next_generation;
        self.generation_token = Some(generation_token);
        self.termination_requested = false;
        if let Ok(mut status) = self.exit_status.lock() {
            *status = None;
        }
        let pid = child.pid();
        let process_tree = match WindowsProcessTree::new(pid) {
            Ok(tree) => tree,
            Err(()) => {
                let _ = child.kill();
                return Err(Self::error(
                    EngineLifecycleOperation::Spawn,
                    EngineLifecycleFailure::SpawnFailed,
                ));
            }
        };
        self.process_tree = Some(process_tree);
        self.child = Some(child);

        let exit_status = Arc::clone(&self.exit_status);
        tauri::async_runtime::spawn(async move {
            while let Some(event) = rx.recv().await {
                match event {
                    CommandEvent::Stdout(line) => {
                        eprintln!("[sidecar stdout] {}", String::from_utf8_lossy(&line));
                    }
                    CommandEvent::Stderr(line) => {
                        eprintln!("[sidecar stderr] {}", String::from_utf8_lossy(&line));
                    }
                    CommandEvent::Terminated(status) => {
                        if let Ok(mut slot) = exit_status.lock() {
                            *slot = status.code;
                        }
                        eprintln!("[sidecar] terminated status_code={:?}", status.code);
                        break;
                    }
                    CommandEvent::Error(_) => {
                        eprintln!("[sidecar] event error");
                    }
                    _ => {}
                }
            }
        });

        Ok(EngineProcessIdentity {
            pid,
            parent_pid: Some(std::process::id()),
            generation: self.generation,
        })
    }

    fn await_ready(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError> {
        let started = Instant::now();
        loop {
            if self.has_exited() {
                return Err(Self::error(
                    EngineLifecycleOperation::AwaitReady,
                    EngineLifecycleFailure::ExitedBeforeReady,
                ));
            }
            match probe_health(HEALTH_POLL_INTERVAL, self.generation_token.as_deref()) {
                HealthProbe::Ready => return Ok(()),
                HealthProbe::InvalidResponse => {
                    return Err(Self::error(
                        EngineLifecycleOperation::AwaitReady,
                        EngineLifecycleFailure::HealthProbeFailed,
                    ));
                }
                HealthProbe::NotReady => {}
            }
            if started.elapsed() >= timeout {
                return Err(Self::error(
                    EngineLifecycleOperation::AwaitReady,
                    EngineLifecycleFailure::ReadinessTimeout,
                ));
            }
            thread::sleep(HEALTH_POLL_INTERVAL);
        }
    }

    fn request_graceful_shutdown(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError> {
        let operation = EngineLifecycleOperation::RequestGracefulShutdown;
        self.record_lineage(operation)?;
        let _ = request_shutdown(HEALTH_POLL_INTERVAL, self.generation_token.as_deref());
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
        let Some(tree) = self.process_tree.as_mut() else {
            return Err(Self::error(
                operation,
                EngineLifecycleFailure::EscalationFailed,
            ));
        };
        tree.terminate(false, timeout)
            .map_err(|_| Self::error(operation, EngineLifecycleFailure::EscalationFailed))
    }

    fn force_kill(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError> {
        let operation = EngineLifecycleOperation::ForceKill;
        let started = Instant::now();
        self.termination_requested = true;
        self.record_lineage(operation)?;
        let tree_result = self
            .process_tree
            .as_mut()
            .map(|tree| tree.terminate(true, timeout))
            .unwrap_or(Err(()));
        if tree_result.is_err() {
            if let Some(child) = self.child.take() {
                let _ = child.kill();
            }
        }
        self.wait_for_cleanup(
            timeout.saturating_sub(started.elapsed()),
            operation,
            EngineLifecycleFailure::EscalationFailed,
        )
    }

    fn verify_cleanup(&mut self, timeout: Duration) -> Result<(), EngineLifecycleError> {
        // Readiness can fail before the ordinary shutdown path records the
        // extracted bootstrap child's descendants. Capture them before
        // treating root exit as verified cleanup.
        self.record_lineage(EngineLifecycleOperation::VerifyCleanup)?;
        self.wait_for_cleanup(
            timeout,
            EngineLifecycleOperation::VerifyCleanup,
            EngineLifecycleFailure::CleanupTimeout,
        )
    }

    fn classify_exit(&self, phase: EngineExitPhase) -> EngineExit {
        let status_code = self.exit_status.lock().ok().and_then(|status| *status);
        let disposition = if self.termination_requested {
            EngineExitDisposition::Terminated
        } else {
            match status_code {
                Some(0) => EngineExitDisposition::Clean,
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
