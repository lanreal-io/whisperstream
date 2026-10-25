//! Session-end cleanup must not start a new executable while Windows is exiting.
//! Ordinary cleanup stays in the process adapter. Observe ALL UI-thread windows (including
//! tao's hidden event target) before their window procedures can emit Exit.

use std::collections::HashSet;
use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};
use windows::Win32::Foundation::{FILETIME, HANDLE, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::Diagnostics::ToolHelp::*;
use windows::Win32::System::Threading::*;
use windows::Win32::UI::WindowsAndMessaging::*;

static SESSION_ENDING: AtomicBool = AtomicBool::new(false);

pub struct SessionEndHook(HHOOK);

impl SessionEndHook {
    pub fn install() -> windows::core::Result<Self> {
        // Thread-local hook, not a global/injected hook. The run() stack owns it
        // until Tauri returns. Installing before setup also covers hidden HWNDs.
        unsafe {
            SetWindowsHookExW(
                WH_CALLWNDPROC,
                Some(observe_session_end),
                None,
                GetCurrentThreadId(),
            )
            .map(Self)
        }
    }
}

impl Drop for SessionEndHook {
    fn drop(&mut self) {
        unsafe {
            let _ = UnhookWindowsHookEx(self.0);
        }
    }
}

unsafe extern "system" fn observe_session_end(
    code: i32,
    wparam: WPARAM,
    lparam: LPARAM,
) -> LRESULT {
    if code >= 0 {
        let message = unsafe { &*(lparam.0 as *const CWPSTRUCT) };
        // QUERY is only a vote. FALSE ENDSESSION is cancellation. Neither may
        // destroy the backend or change subsequent ordinary-Quit behavior.
        if message.message == WM_ENDSESSION && message.wParam.0 != 0 {
            SESSION_ENDING.store(true, Ordering::Release);
        }
    }
    unsafe { CallNextHookEx(None, code, wparam, lparam) }
}

pub fn session_ending() -> bool {
    SESSION_ENDING.load(Ordering::Acquire)
        // Also cover a user/update exit racing the OS's notification delivery.
        || unsafe { GetSystemMetrics(SM_SHUTTINGDOWN) != 0 }
}

/// Call at the end of RunEvent::Exit, after owned-sidecar cleanup. tao 0.34.5
/// emits Exit inside WM_ENDSESSION but then resumes a Destroyed event loop.
/// Do not enter Tauri's ordinary window/resource teardown or that event loop
/// again after confirmed session end. Mirrors tao's upstream fix (#1157).
pub fn exit_if_session_ended() {
    // Unlike the cleanup fallback above, process exit requires an actual
    // confirmed notification. QUERYENDSESSION and cancellation are inert.
    if SESSION_ENDING.load(Ordering::Acquire) {
        std::process::exit(0);
    }
}

pub fn stop_sidecar_tree(pid: u32) {
    // Never launch an executable on this session-end path, even on failure.
    // Snapshot failure still permits a native root kill using its pinned PID.
    if terminate_tree(pid, Duration::from_millis(500)).is_err() {
        if let Ok(root) = Process::open(pid) {
            unsafe {
                let _ = TerminateProcess(root.raw(), 1);
            }
        }
        eprintln!("[tauri] sidecar cleanup result=failed mode=session_end");
    }
}

pub struct Process {
    handle: OwnedHandle,
    pid: u32,
    created: u64,
}

impl Process {
    pub fn pid(&self) -> u32 { self.pid }

    pub fn open(pid: u32) -> windows::core::Result<Self> {
        let handle = unsafe {
            OpenProcess(
                PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                false,
                pid,
            )?
        };
        let handle = unsafe { OwnedHandle::from_raw_handle(handle.0) };
        let (mut created, mut exited, mut kernel, mut user) = (
            FILETIME::default(),
            FILETIME::default(),
            FILETIME::default(),
            FILETIME::default(),
        );
        unsafe {
            GetProcessTimes(
                HANDLE(handle.as_raw_handle()),
                &mut created,
                &mut exited,
                &mut kernel,
                &mut user,
            )?;
        }
        Ok(Self {
            handle,
            pid,
            created: ((created.dwHighDateTime as u64) << 32) | created.dwLowDateTime as u64,
        })
    }

    fn raw(&self) -> HANDLE {
        HANDLE(self.handle.as_raw_handle())
    }
}

/// Caller must retain the original child process handle, as PackagedEngine does,
/// so its PID cannot be recycled underneath the root lookup. Snapshot first,
/// acquire/validate descendant handles, then terminate children before parents.
/// Never select by executable name, port, or a process outside this lineage.
pub fn terminate_tree(pid: u32, wait_budget: Duration) -> windows::core::Result<()> {
    let started = Instant::now();
    let root = Process::open(pid)?;
    let captured = unsafe { windows::Win32::System::SystemInformation::GetSystemTimeAsFileTime() };
    let captured = ((captured.dwHighDateTime as u64) << 32) | captured.dwLowDateTime as u64;
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)? };
    let snapshot = unsafe { OwnedHandle::from_raw_handle(snapshot.0) };
    let mut entry = PROCESSENTRY32W {
        dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
        ..Default::default()
    };
    let mut entries = Vec::new();
    unsafe {
        Process32FirstW(HANDLE(snapshot.as_raw_handle()), &mut entry)?;
        loop {
            entries.push((entry.th32ProcessID, entry.th32ParentProcessID));
            if let Err(error) = Process32NextW(HANDLE(snapshot.as_raw_handle()), &mut entry) {
                if error.code() != windows::core::HRESULT::from_win32(18) {
                    return Err(error);
                }
                break;
            }
        }
    }
    let mut processes = vec![root];
    let mut seen = HashSet::from([pid]);
    let mut index = 0;
    while index < processes.len() {
        let parent = &processes[index];
        let (parent_pid, parent_created) = (parent.pid, parent.created);
        for &(child_pid, recorded_parent) in &entries {
            if recorded_parent == parent_pid && seen.insert(child_pid) {
                if let Ok(child) = Process::open(child_pid) {
                    // A stale parent PID in the snapshot must not claim an older,
                    // unrelated process. Retained handles prevent further reuse.
                    if child.created >= parent_created && child.created <= captured {
                        processes.push(child);
                    }
                }
            }
        }
        index += 1;
    }
    let mut first_error = None;
    for process in processes.iter().rev() {
        unsafe {
            if WaitForSingleObject(process.raw(), 0) == windows::Win32::Foundation::WAIT_OBJECT_0 {
                continue;
            }
            if let Err(error) = TerminateProcess(process.raw(), 1) {
                if WaitForSingleObject(process.raw(), 0)
                    != windows::Win32::Foundation::WAIT_OBJECT_0
                {
                    first_error.get_or_insert(error);
                }
            }
        }
    }
    // One total wait budget, not a fresh timeout for each descendant.
    for process in &processes {
        let remaining = wait_budget.saturating_sub(started.elapsed()).as_millis() as u32;
        unsafe {
            if WaitForSingleObject(process.raw(), remaining)
                != windows::Win32::Foundation::WAIT_OBJECT_0
            {
                first_error.get_or_insert_with(|| {
                    windows::core::Error::from(windows::core::HRESULT::from_win32(1460))
                });
            }
        }
    }
    first_error.map_or(Ok(()), Err)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::windows::process::CommandExt;
    use std::path::PathBuf;
    use std::process::{Child, Command, Stdio};
    use windows::core::w;
    static TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    use windows::Win32::UI::Shell::{DefSubclassProc, SetWindowSubclass};

    // Never broadcast shutdown messages. The simulated tao callback belongs to
    // this test thread and receives messages only on its own hidden HWNDs.
    #[test]
    fn session_observer_precedes_hidden_window_exit_and_preserves_cancellation() {
        unsafe extern "system" fn simulated_tao(
            hwnd: windows::Win32::Foundation::HWND,
            msg: u32,
            wp: WPARAM,
            lp: LPARAM,
            _: usize,
            reference: usize,
        ) -> LRESULT {
            if msg == WM_ENDSESSION {
                unsafe { &mut *(reference as *mut Vec<bool>) }
                    .push(SESSION_ENDING.load(Ordering::Acquire));
            }
            unsafe { DefSubclassProc(hwnd, msg, wp, lp) }
        }
        let _lock = TEST_LOCK.lock().unwrap();
        SESSION_ENDING.store(false, Ordering::Release);
        let hook = SessionEndHook::install().unwrap();
        let mut observed = Vec::<bool>::new();
        unsafe {
            let hidden = CreateWindowExW(
                WINDOW_EX_STYLE::default(),
                w!("STATIC"),
                w!(""),
                WS_POPUP,
                0,
                0,
                1,
                1,
                None,
                None,
                None,
                None,
            )
            .unwrap();
            let settings = CreateWindowExW(
                WINDOW_EX_STYLE::default(),
                w!("STATIC"),
                w!(""),
                WS_POPUP,
                0,
                0,
                1,
                1,
                None,
                None,
                None,
                None,
            )
            .unwrap();
            SetWindowSubclass(
                hidden,
                Some(simulated_tao),
                99,
                &mut observed as *mut _ as usize,
            )
            .ok()
            .unwrap();
            SendMessageW(hidden, WM_QUERYENDSESSION, Some(WPARAM(0)), Some(LPARAM(0)));
            assert!(!SESSION_ENDING.load(Ordering::Acquire));
            SendMessageW(hidden, WM_ENDSESSION, Some(WPARAM(0)), Some(LPARAM(0)));
            assert_eq!(observed, [false]);
            // Hidden target gets the confirmed notification BEFORE settings.
            for flags in [0, ENDSESSION_LOGOFF as isize, ENDSESSION_CLOSEAPP as isize] {
                SESSION_ENDING.store(false, Ordering::Release);
                SendMessageW(hidden, WM_ENDSESSION, Some(WPARAM(1)), Some(LPARAM(flags)));
                assert!(observed.last().copied().unwrap());
                assert!(session_ending());
                SendMessageW(
                    settings,
                    WM_ENDSESSION,
                    Some(WPARAM(1)),
                    Some(LPARAM(flags)),
                );
            }
            DestroyWindow(hidden).unwrap();
            DestroyWindow(settings).unwrap();
        }
        drop(hook);
        SESSION_ENDING.store(false, Ordering::Release);
    }

    struct OwnedFixture(Child);
    impl Drop for OwnedFixture {
        fn drop(&mut self) {
            let _ = terminate_tree(self.0.id(), Duration::from_millis(500));
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }

    fn fixture(directory: &std::path::Path, role: &str) -> OwnedFixture {
        OwnedFixture(
            Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "windows_shutdown::tests::process_fixture",
                    "--ignored",
                    "--nocapture",
                ])
                .env("WS_SHUTDOWN_FIXTURE", directory)
                .env("WS_SHUTDOWN_ROLE", role)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .creation_flags(CREATE_NO_WINDOW.0)
                .spawn()
                .unwrap(),
        )
    }

    #[test]
    #[ignore = "subprocess fixture, launched only by the owned-tree test"]
    fn process_fixture() {
        let directory = PathBuf::from(std::env::var_os("WS_SHUTDOWN_FIXTURE").unwrap());
        let role = std::env::var("WS_SHUTDOWN_ROLE").unwrap();
        let _child = match role.as_str() {
            "root" => Some(fixture(&directory, "middle")),
            "middle" => Some(fixture(&directory, "leaf")),
            _ => None,
        };
        std::fs::write(directory.join(&role), std::process::id().to_string()).unwrap();
        loop {
            std::thread::sleep(Duration::from_secs(1));
        }
    }

    #[test]
    fn cleanup_stops_descendants_preserves_unrelated_and_is_repeatable() {
        let _lock = TEST_LOCK.lock().unwrap();
        for (ending, root_exited) in [(false, false), (true, false), (true, true)] {
            let directory =
                std::env::temp_dir().join(format!("ws-shutdown-test-{}", std::process::id()));
            std::fs::create_dir_all(&directory).unwrap();
            let root = fixture(&directory, "root");
            let mut unrelated = fixture(&directory, "unrelated");
            let deadline = Instant::now() + Duration::from_secs(15);
            while !["root", "middle", "leaf", "unrelated"]
                .iter()
                .all(|name| directory.join(name).exists())
            {
                assert!(Instant::now() < deadline, "fixture did not start");
                std::thread::sleep(Duration::from_millis(20));
            }
            let processes: Vec<_> = ["root", "middle", "leaf"]
                .iter()
                .map(|name| {
                    let pid = std::fs::read_to_string(directory.join(name))
                        .unwrap()
                        .parse()
                        .unwrap();
                    Process::open(pid).unwrap()
                })
                .collect();
            if root_exited {
                unsafe {
                    TerminateProcess(processes[0].raw(), 1).unwrap();
                    assert_eq!(
                        WaitForSingleObject(processes[0].raw(), 2000),
                        windows::Win32::Foundation::WAIT_OBJECT_0
                    );
                }
            }
            let started = Instant::now();
            SESSION_ENDING.store(ending, Ordering::Release);
            stop_sidecar_tree(root.0.id());
            SESSION_ENDING.store(false, Ordering::Release);
            assert!(started.elapsed() < Duration::from_secs(3));
            for process in &processes {
                assert_eq!(
                    unsafe { WaitForSingleObject(process.raw(), 0) },
                    windows::Win32::Foundation::WAIT_OBJECT_0
                );
            }
            assert!(unrelated.0.try_wait().unwrap().is_none());
            terminate_tree(root.0.id(), Duration::from_millis(50)).unwrap();
            drop(root);
            drop(unrelated);
            std::fs::remove_dir_all(directory).unwrap();
        }
    }
}
