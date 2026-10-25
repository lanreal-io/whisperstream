//! Windows-only recorded-lineage process-tree operations.
//!
//! This is the only production module allowed to know about `taskkill`.

#![cfg(target_os = "windows")]

use std::collections::{BTreeMap, BTreeSet};
use std::os::windows::process::CommandExt;
use std::process::Command;
use std::thread;
use std::time::{Duration, Instant};
use windows::Win32::Foundation::{CloseHandle, FILETIME, HANDLE, WAIT_TIMEOUT};
use windows::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W, TH32CS_SNAPPROCESS,
};
use windows::Win32::System::Threading::{
    GetProcessTimes, OpenProcess, WaitForSingleObject, PROCESS_QUERY_LIMITED_INFORMATION,
    PROCESS_SYNCHRONIZE, PROCESS_TERMINATE, TerminateProcess,
};

const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const CLEANUP_POLL_INTERVAL: Duration = Duration::from_millis(25);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ProcessGeneration {
    pub pid: u32,
    pub parent_pid: u32,
    pub creation_time_100ns: u64,
}

#[derive(Debug)]
struct ProcessHandle(usize);

impl ProcessHandle {
    fn new(handle: HANDLE) -> Self {
        Self(handle.0 as usize)
    }

    fn as_handle(&self) -> HANDLE {
        HANDLE(self.0 as *mut core::ffi::c_void)
    }
}

impl Drop for ProcessHandle {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseHandle(self.as_handle());
        }
    }
}

#[derive(Debug)]
struct RecordedProcess {
    generation: ProcessGeneration,
    handle: ProcessHandle,
}

impl RecordedProcess {
    fn capture(pid: u32, parent_pid: u32) -> Result<Self, ()> {
        unsafe {
            let handle = OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE | PROCESS_TERMINATE,
                false,
                pid,
            )
            .map_err(|_| ())?;
            let mut creation = FILETIME::default();
            let mut exit = FILETIME::default();
            let mut kernel = FILETIME::default();
            let mut user = FILETIME::default();
            if GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user).is_err() {
                let _ = CloseHandle(handle);
                return Err(());
            }
            Ok(Self {
                generation: ProcessGeneration {
                    pid,
                    parent_pid,
                    creation_time_100ns: filetime_value(creation),
                },
                handle: ProcessHandle::new(handle),
            })
        }
    }

    fn is_running(&self) -> bool {
        unsafe { WaitForSingleObject(self.handle.as_handle(), 0) == WAIT_TIMEOUT }
    }
}

fn filetime_value(value: FILETIME) -> u64 {
    (u64::from(value.dwHighDateTime) << 32) | u64::from(value.dwLowDateTime)
}

fn creation_within_lineage(parent_created: u64, child_created: u64, captured: u64) -> bool {
    child_created >= parent_created && child_created <= captured
}

#[derive(Debug)]
pub struct WindowsProcessTree {
    root_pid: u32,
    recorded: BTreeMap<u32, RecordedProcess>,
}

impl WindowsProcessTree {
    pub fn new(root_pid: u32) -> Result<Self, ()> {
        let parent_pid = process_snapshot()?.get(&root_pid).copied().ok_or(())?;
        let root = RecordedProcess::capture(root_pid, parent_pid)?;
        Ok(Self {
            root_pid,
            recorded: BTreeMap::from([(root_pid, root)]),
        })
    }

    pub fn record_lineage(&mut self) -> Result<Vec<ProcessGeneration>, ()> {
        let captured = filetime_value(unsafe {
            windows::Win32::System::SystemInformation::GetSystemTimeAsFileTime()
        });
        let processes = process_snapshot()?;
        let mut by_parent: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
        for (pid, parent_pid) in &processes {
            by_parent.entry(*parent_pid).or_default().push(*pid);
        }

        let mut pending = vec![self.root_pid];
        let mut visited = BTreeSet::new();
        while let Some(parent) = pending.pop() {
            if !visited.insert(parent) {
                continue;
            }
            if let Some(children) = by_parent.get(&parent) {
                for child in children {
                    if !self.recorded.contains_key(child) {
                        if let Ok(process) = RecordedProcess::capture(*child, parent) {
                            let parent_created = self.recorded[&parent].generation.creation_time_100ns;
                            let child_created = process.generation.creation_time_100ns;
                            if creation_within_lineage(parent_created, child_created, captured) {
                                self.recorded.insert(*child, process);
                            }
                        }
                    }
                    if self.recorded.contains_key(child) {
                        pending.push(*child);
                    }
                }
            }
        }
        Ok(self
            .recorded
            .values()
            .map(|process| process.generation)
            .collect())
    }

    pub fn terminate(&mut self, force: bool, timeout: Duration) -> Result<(), ()> {
        let started = Instant::now();
        let _ = self.record_lineage();
        if !self.root_is_running()? {
            // taskkill /T cannot walk an exited root. Retained handles still
            // identify its recorded descendants, including an extracted
            // Nuitka child that outlived the bootstrap process.
            for process in self.recorded.values() {
                if process.is_running() {
                    unsafe { TerminateProcess(process.handle.as_handle(), 1) }.map_err(|_| ())?;
                }
            }
            return self.wait_for_cleanup(timeout.saturating_sub(started.elapsed()));
        }
        // Session end can begin after the shared shutdown path admitted an
        // ordinary cleanup. Recheck before starting an external executable.
        if crate::windows_shutdown::session_ending() {
            crate::windows_shutdown::terminate_tree(
                self.root_pid,
                timeout.saturating_sub(started.elapsed()).min(Duration::from_millis(500)),
            ).map_err(|_| ())?;
            return self.wait_for_cleanup(timeout.saturating_sub(started.elapsed()));
        }
        let mut command = Command::new("taskkill");
        if force {
            command.arg("/F");
        }
        let mut taskkill = command
            .args(["/T", "/PID", &self.root_pid.to_string()])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .map_err(|_| ())?;
        let status = loop {
            if let Some(status) = taskkill.try_wait().map_err(|_| ())? {
                break status;
            }
            if started.elapsed() >= timeout {
                let _ = taskkill.kill();
                let _ = taskkill.wait();
                return Err(());
            }
            thread::sleep(CLEANUP_POLL_INTERVAL.min(timeout.saturating_sub(started.elapsed())));
        };
        if status.success() || self.recorded_lineage_exited()? {
            self.wait_for_cleanup(timeout.saturating_sub(started.elapsed()))
        } else {
            Err(())
        }
    }

    pub fn wait_for_cleanup(&self, timeout: Duration) -> Result<(), ()> {
        let started = Instant::now();
        loop {
            if self.recorded_lineage_exited()? {
                return Ok(());
            }
            if started.elapsed() >= timeout {
                return Err(());
            }
            thread::sleep(CLEANUP_POLL_INTERVAL.min(timeout.saturating_sub(started.elapsed())));
        }
    }

    pub fn root_is_running(&self) -> Result<bool, ()> {
        self.recorded
            .get(&self.root_pid)
            .map(|process| process.is_running())
            .ok_or(())
    }

    fn recorded_lineage_exited(&self) -> Result<bool, ()> {
        Ok(self.recorded.values().all(|process| !process.is_running()))
    }
}

fn process_snapshot() -> Result<BTreeMap<u32, u32>, ()> {
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0).map_err(|_| ())?;
        let mut entry = PROCESSENTRY32W {
            dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
            ..Default::default()
        };
        let mut processes = BTreeMap::new();
        if Process32FirstW(snapshot, &mut entry).is_ok() {
            loop {
                processes.insert(entry.th32ProcessID, entry.th32ParentProcessID);
                if Process32NextW(snapshot, &mut entry).is_err() {
                    break;
                }
            }
        }
        let _ = CloseHandle(snapshot);
        Ok(processes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::process::{Child, Stdio};

    struct ChildGuard(Child);

    impl Drop for ChildGuard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    struct TreeGuard {
        child: Child,
        root_pid: u32,
    }

    #[test]
    fn lineage_rejects_stale_parent_links_and_post_snapshot_processes() {
        assert!(!creation_within_lineage(100, 99, 200));
        assert!(!creation_within_lineage(100, 201, 200));
        assert!(creation_within_lineage(100, 100, 200));
        assert!(creation_within_lineage(100, 200, 200));
    }

    #[test]
    fn same_pid_with_different_creation_time_is_a_different_generation() {
        let first = ProcessGeneration {
            pid: 4100,
            parent_pid: 4000,
            creation_time_100ns: 100,
        };
        let recycled = ProcessGeneration {
            creation_time_100ns: 101,
            ..first
        };

        assert_ne!(first, recycled);
    }

    impl Drop for TreeGuard {
        fn drop(&mut self) {
            if let Ok(mut tree) = WindowsProcessTree::new(self.root_pid) {
                let _ = tree.terminate(true, Duration::from_secs(1));
            }
            let _ = self.child.wait();
        }
    }

    #[test]
    fn recorded_descendant_cleanup_survives_an_exited_root() {
        let script = "$child = Start-Process -FilePath ping.exe -ArgumentList '-t','127.0.0.1' -WindowStyle Hidden -PassThru; [Console]::Out.WriteLine($child.Id); [Console]::Out.Flush(); Wait-Process -Id $child.Id";
        let mut root = ChildGuard(Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", script])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn().expect("owned root should spawn"));
        let mut line = String::new();
        BufReader::new(root.0.stdout.take().unwrap()).read_line(&mut line).unwrap();
        let child_pid: u32 = line.trim().parse().unwrap();
        let child = RecordedProcess::capture(child_pid, root.0.id()).unwrap();
        struct DescendantGuard(RecordedProcess);
        impl Drop for DescendantGuard {
            fn drop(&mut self) {
                unsafe { let _ = TerminateProcess(self.0.handle.as_handle(), 1); }
            }
        }
        let _child = DescendantGuard(child);
        let mut tree = WindowsProcessTree::new(root.0.id()).unwrap();
        assert!(tree.record_lineage().unwrap().iter().any(|entry| entry.pid == child_pid));
        root.0.kill().unwrap();
        root.0.wait().unwrap();
        assert!(!tree.root_is_running().unwrap());
        tree.terminate(true, Duration::from_secs(5)).expect("live descendant must terminate after root exit");
        assert!(tree.recorded_lineage_exited().unwrap());
    }

    #[test]
    fn exact_recorded_tree_cleanup_preserves_unrelated_process() {
        let script = "$child = Start-Process -FilePath ping.exe -ArgumentList '-t','127.0.0.1' -WindowStyle Hidden -PassThru; [Console]::Out.WriteLine($child.Id); [Console]::Out.Flush(); Wait-Process -Id $child.Id";
        let mut parent = Command::new("powershell.exe")
            .args(["-NoProfile", "-NonInteractive", "-Command", script])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("owned parent should spawn");
        let root_pid = parent.id();
        let stdout = parent
            .stdout
            .take()
            .expect("owned parent stdout should exist");
        let mut line = String::new();
        BufReader::new(stdout)
            .read_line(&mut line)
            .expect("owned child pid should be readable");
        let child_pid: u32 = line.trim().parse().expect("owned child pid should parse");
        let mut owned = TreeGuard {
            child: parent,
            root_pid,
        };

        let unrelated = Command::new("ping.exe")
            .args(["-t", "127.0.0.1"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("unrelated process should spawn");
        let unrelated_pid = unrelated.id();
        let _unrelated = ChildGuard(unrelated);

        let mut tree = WindowsProcessTree::new(root_pid).expect("root identity should resolve");
        let lineage = tree.record_lineage().expect("lineage should record");
        assert!(lineage.iter().any(|identity| identity.pid == root_pid));
        assert!(lineage.iter().any(|identity| identity.pid == child_pid));
        assert!(!lineage.iter().any(|identity| identity.pid == unrelated_pid));
        assert!(lineage
            .iter()
            .all(|identity| identity.creation_time_100ns > 0));

        let cleanup_started = Instant::now();
        tree.terminate(true, Duration::from_secs(5))
            .expect("owned tree should terminate");
        tree.wait_for_cleanup(Duration::from_secs(5))
            .expect("owned lineage should exit");
        let cleanup_ms = cleanup_started.elapsed().as_secs_f64() * 1_000.0;
        assert!(
            process_snapshot()
                .expect("snapshot should pass")
                .contains_key(&unrelated_pid),
            "unrelated process must remain alive"
        );
        eprintln!(
            "process_lineage root_pid={root_pid} child_pid={child_pid} unrelated_pid={unrelated_pid} lineage={lineage:?} cleanup_ms={cleanup_ms:.3} cleanup_verified=true unrelated_preserved=true identity_bound=handle+creation_time",
        );
        let _ = owned.child.wait();
    }
}
