//! macOS-only exact process-group ownership for the packaged engine.
//!
//! The engine is spawned as the leader of a fresh process group. Every native
//! STT worker it creates inherits that group, so escalation can target the
//! owned generation without searching by executable name. PID start times are
//! retained as part of cleanup proof so PID reuse cannot make an unrelated
//! process look like a surviving descendant.

#![cfg(target_os = "macos")]

use std::collections::BTreeMap;
use std::mem::{size_of, zeroed};
use std::thread;
use std::time::{Duration, Instant};

const CLEANUP_POLL_INTERVAL: Duration = Duration::from_millis(25);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct RecordedProcess {
    pid: i32,
    parent_pid: i32,
    process_group_id: i32,
    start_seconds: u64,
    start_microseconds: u64,
    status: u32,
}

#[derive(Debug)]
pub struct MacosProcessGroup {
    root_pid: i32,
    process_group_id: i32,
    recorded: BTreeMap<i32, RecordedProcess>,
}

impl MacosProcessGroup {
    pub fn new(root_pid: u32) -> Result<Self, ()> {
        let root_pid = i32::try_from(root_pid).map_err(|_| ())?;
        let root = process_info(root_pid)?.ok_or(())?;
        if root.process_group_id != root_pid {
            return Err(());
        }
        Ok(Self {
            root_pid,
            process_group_id: root_pid,
            recorded: BTreeMap::from([(root_pid, root)]),
        })
    }

    #[cfg(test)]
    fn process_group_id(&self) -> i32 {
        self.process_group_id
    }

    pub fn record_lineage(&mut self) -> Result<Vec<u32>, ()> {
        for pid in process_group_pids(self.process_group_id)? {
            if let Some(process) = process_info(pid)? {
                if process.process_group_id == self.process_group_id {
                    self.recorded.entry(pid).or_insert(process);
                }
            }
        }
        Ok(self
            .recorded
            .keys()
            .filter_map(|pid| u32::try_from(*pid).ok())
            .collect())
    }

    pub fn root_is_running(&self) -> Result<bool, ()> {
        let Some(root) = self.recorded.get(&self.root_pid) else {
            return Ok(false);
        };
        identity_is_running(*root)
    }

    pub fn terminate(&mut self, timeout: Duration) -> Result<(), ()> {
        self.signal(libc::SIGTERM, timeout)
    }

    pub fn force_kill(&mut self, timeout: Duration) -> Result<(), ()> {
        self.signal(libc::SIGKILL, timeout)
    }

    fn signal(&mut self, signal: i32, timeout: Duration) -> Result<(), ()> {
        let started = Instant::now();
        let _ = self.record_lineage();
        let result = unsafe { libc::killpg(self.process_group_id, signal) };
        if result != 0 {
            let errno = std::io::Error::last_os_error().raw_os_error();
            if errno != Some(libc::ESRCH) {
                return Err(());
            }
        }
        self.wait_for_cleanup(timeout.saturating_sub(started.elapsed()))
    }

    pub fn wait_for_cleanup(&mut self, timeout: Duration) -> Result<(), ()> {
        let started = Instant::now();
        loop {
            let any_recorded_running =
                self.recorded.values().try_fold(false, |running, process| {
                    identity_is_running(*process).map(|current| running || current)
                })?;
            if any_recorded_running {
                let _ = self.record_lineage();
            }
            let recorded_exited = self.recorded.values().try_fold(true, |exited, process| {
                identity_is_running(*process).map(|current| exited && !current)
            })?;
            let group_empty = process_group_pids(self.process_group_id)?
                .into_iter()
                .try_fold(true, |empty, pid| {
                    process_info(pid).map(|process| {
                        empty && process.is_none_or(|current| current.status == libc::SZOMB)
                    })
                })?;
            if recorded_exited && group_empty {
                return Ok(());
            }
            if started.elapsed() >= timeout {
                return Err(());
            }
            thread::sleep(CLEANUP_POLL_INTERVAL.min(timeout.saturating_sub(started.elapsed())));
        }
    }

    #[cfg(test)]
    fn recorded_pids(&self) -> Vec<u32> {
        self.recorded
            .keys()
            .filter_map(|pid| u32::try_from(*pid).ok())
            .collect()
    }
}

fn process_group_pids(process_group_id: i32) -> Result<Vec<i32>, ()> {
    let mut capacity = 32_usize;
    loop {
        let mut buffer = vec![0_i32; capacity];
        let count = unsafe {
            libc::proc_listpgrppids(
                process_group_id,
                buffer.as_mut_ptr().cast(),
                i32::try_from(buffer.len() * size_of::<i32>()).map_err(|_| ())?,
            )
        };
        if count < 0 {
            return Err(());
        }
        let count = usize::try_from(count).map_err(|_| ())?;
        if count < capacity {
            buffer.truncate(count);
            buffer.retain(|pid| *pid > 0);
            return Ok(buffer);
        }
        capacity = capacity
            .checked_mul(2)
            .filter(|value| *value <= 4096)
            .ok_or(())?;
    }
}

fn process_info(pid: i32) -> Result<Option<RecordedProcess>, ()> {
    let mut info: libc::proc_bsdinfo = unsafe { zeroed() };
    let expected = i32::try_from(size_of::<libc::proc_bsdinfo>()).map_err(|_| ())?;
    let result = unsafe {
        libc::proc_pidinfo(
            pid,
            libc::PROC_PIDTBSDINFO,
            0,
            (&mut info as *mut libc::proc_bsdinfo).cast(),
            expected,
        )
    };
    if result == 0 {
        return Ok(None);
    }
    if result != expected {
        return Err(());
    }
    Ok(Some(RecordedProcess {
        pid: i32::try_from(info.pbi_pid).map_err(|_| ())?,
        parent_pid: i32::try_from(info.pbi_ppid).map_err(|_| ())?,
        process_group_id: i32::try_from(info.pbi_pgid).map_err(|_| ())?,
        start_seconds: info.pbi_start_tvsec,
        start_microseconds: info.pbi_start_tvusec,
        status: info.pbi_status,
    }))
}

fn identity_is_running(recorded: RecordedProcess) -> Result<bool, ()> {
    Ok(process_info(recorded.pid)?.is_some_and(|current| {
        current.pid == recorded.pid
            && current.start_seconds == recorded.start_seconds
            && current.start_microseconds == recorded.start_microseconds
            && current.status != libc::SZOMB
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::process::CommandExt;
    use std::process::{Child, Command, Stdio};

    struct ChildGuard(Child);

    impl Drop for ChildGuard {
        fn drop(&mut self) {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    struct GroupGuard {
        child: Child,
        root_pid: u32,
    }

    impl Drop for GroupGuard {
        fn drop(&mut self) {
            if let Ok(mut group) = MacosProcessGroup::new(self.root_pid) {
                let _ = group.force_kill(Duration::from_secs(1));
            }
            let _ = self.child.wait();
        }
    }

    #[test]
    fn exact_recorded_group_cleanup_preserves_unrelated_process() {
        let mut command = Command::new("/bin/sh");
        command
            .args(["-c", "/bin/sleep 30 & wait"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        let owned_child = command.spawn().expect("owned process group should spawn");
        let root_pid = owned_child.id();
        let mut owned = GroupGuard {
            child: owned_child,
            root_pid,
        };

        let unrelated_child = Command::new("/bin/sleep")
            .arg("30")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("unrelated sentinel should spawn");
        let unrelated_pid = unrelated_child.id();
        let mut unrelated = ChildGuard(unrelated_child);

        let mut group = MacosProcessGroup::new(root_pid).expect("group should resolve");
        let deadline = Instant::now() + Duration::from_secs(2);
        let lineage = loop {
            let lineage = group.record_lineage().expect("lineage should record");
            if lineage.len() >= 2 || Instant::now() >= deadline {
                break lineage;
            }
            thread::sleep(Duration::from_millis(25));
        };
        assert!(lineage.contains(&root_pid));
        assert!(lineage.len() >= 2, "owned shell child should be recorded");
        assert!(!lineage.contains(&unrelated_pid));
        assert_eq!(group.process_group_id(), i32::try_from(root_pid).unwrap());

        let cleanup_started = Instant::now();
        group
            .terminate(Duration::from_secs(3))
            .expect("owned group should terminate");
        let cleanup_ms = cleanup_started.elapsed().as_secs_f64() * 1_000.0;
        assert!(identity_is_running(
            *process_info(i32::try_from(unrelated_pid).unwrap())
                .unwrap()
                .as_ref()
                .unwrap()
        )
        .unwrap());
        assert!(!group.root_is_running().expect("root state should resolve"));
        eprintln!(
            "process_lineage root_pid={root_pid} pgid={} lineage_count={} unrelated_pid={unrelated_pid} cleanup_ms={cleanup_ms:.3} cleanup_verified=true unrelated_preserved=true",
            group.process_group_id(),
            lineage.len(),
        );
        let _ = owned.child.wait();
        let _ = unrelated.0.kill();
        let _ = unrelated.0.wait();
    }
}
