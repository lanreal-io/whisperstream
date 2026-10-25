//! Owned Tauri session-end experiment. Never sends a system-wide broadcast.
#[cfg(windows)]
#[path = "../src/windows_shutdown.rs"]
mod windows_shutdown;

#[cfg(windows)]
mod probe {
    use super::windows_shutdown;
    use tauri::{Manager, RunEvent};
    use windows::core::BOOL;
    use windows::Win32::Foundation::{HWND, LPARAM, WPARAM};
    use windows::Win32::System::Threading::GetCurrentThreadId;
    use windows::Win32::UI::WindowsAndMessaging::*;

    unsafe extern "system" fn find_target(hwnd: HWND, parameter: LPARAM) -> BOOL {
        let mut class = [0u16; 256];
        let len = unsafe { GetClassNameW(hwnd, &mut class) };
        if String::from_utf16_lossy(&class[..len as usize]) == "Tao Thread Event Target" {
            unsafe { *(parameter.0 as *mut isize) = hwnd.0 as isize };
        }
        BOOL(1)
    }

    pub fn run() {
        let mode = std::env::args().nth(1).unwrap_or_else(|| "shutdown".into());
        assert!([
            "shutdown",
            "logoff",
            "restart-manager",
            "cancel",
            "ordinary"
        ]
        .contains(&mode.as_str()));
        let _hook = windows_shutdown::SessionEndHook::install().unwrap();
        let thread = unsafe { GetCurrentThreadId() };
        let app = tauri::Builder::default()
            .setup(|app| {
                tauri::WebviewWindowBuilder::new(
                    app,
                    "probe",
                    tauri::WebviewUrl::External("about:blank".parse().unwrap()),
                )
                .data_directory(
                    std::env::temp_dir()
                        .join(format!("ws-shutdown-webview-{}", std::process::id())),
                )
                .visible(false)
                .build()?;
                Ok(())
            })
            .build(tauri::test::mock_context(tauri::test::noop_assets()))
            .unwrap();
        app.run(move |app, event| match event {
            RunEvent::Ready => {
                std::thread::spawn(|| {
                    std::thread::sleep(std::time::Duration::from_secs(5));
                    eprintln!("probe timed out with shell still alive");
                    std::process::exit(77);
                });
                let mut target = 0isize;
                unsafe {
                    EnumThreadWindows(
                        thread,
                        Some(find_target),
                        LPARAM(&mut target as *mut _ as isize),
                    )
                    .unwrap();
                }
                assert_ne!(target, 0);
                let app = app.clone();
                let mode = mode.clone();
                std::thread::spawn(move || {
                    let hwnd = HWND(target as *mut _);
                    let started = std::time::Instant::now();
                    if mode == "ordinary" {
                        app.exit(0);
                        return;
                    }
                    for (message, confirmed) in [(WM_QUERYENDSESSION, 0), (WM_ENDSESSION, 0)] {
                        let mut result = 0usize;
                        let sent = unsafe {
                            SendMessageTimeoutW(
                                hwnd,
                                message,
                                WPARAM(confirmed),
                                LPARAM(0),
                                SMTO_ABORTIFHUNG,
                                2000,
                                Some(&mut result),
                            )
                        };
                        assert_ne!(sent.0, 0);
                        assert!(!windows_shutdown::session_ending());
                        if message == WM_QUERYENDSESSION {
                            assert_eq!(result, 1);
                        }
                    }
                    eprintln!("query/cancellation preserved shell");
                    if mode == "cancel" {
                        // Show that it can still process an ordinary exit afterward.
                        app.exit(0);
                        return;
                    }
                    let flags = match mode.as_str() {
                        "logoff" => ENDSESSION_LOGOFF as isize,
                        "restart-manager" => ENDSESSION_CLOSEAPP as isize,
                        _ => 0,
                    };
                    eprintln!("sending confirmed session end");
                    unsafe {
                        SendMessageTimeoutW(
                            hwnd,
                            WM_ENDSESSION,
                            WPARAM(1),
                            LPARAM(flags),
                            SMTO_ABORTIFHUNG,
                            2000,
                            None,
                        );
                    }
                    eprintln!("confirmed message returned after {:?}", started.elapsed());
                });
            }
            RunEvent::Exit => {
                eprintln!(
                    "Exit callback ending={} windows={}",
                    windows_shutdown::session_ending(),
                    app.webview_windows().len()
                );
                if !std::env::args().any(|arg| arg == "--baseline") {
                    windows_shutdown::exit_if_session_ended();
                }
                eprintln!("Exit callback returning");
            }
            _ => {}
        });
        eprintln!("app.run returned");
    }
}

fn main() {
    #[cfg(windows)]
    probe::run();
    #[cfg(not(windows))]
    eprintln!("This probe requires Windows.");
}
