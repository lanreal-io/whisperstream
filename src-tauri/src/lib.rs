#[cfg(not(any(target_os = "windows", target_os = "macos")))]
use tauri::image::Image;
#[cfg(not(any(target_os = "windows", target_os = "macos")))]
use tauri::menu::{Menu, MenuItem};
#[cfg(not(any(target_os = "windows", target_os = "macos")))]
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::Manager;
// The official installer's updater compiles in only when the build supplies
// its provider (see build.rs). Builds without one have no updater.
#[cfg(official_updater)]
include!(concat!(env!("OUT_DIR"), "/official_updater.rs"));
#[cfg(official_updater)]
use official_updater::with_commands as with_private_commands;
#[cfg(not(official_updater))]
fn with_private_commands<H>(handler: H) -> H
where
    H: Fn(tauri::ipc::Invoke<tauri::Wry>) -> bool + Send + Sync + 'static,
{
    handler
}

#[cfg_attr(debug_assertions, allow(dead_code))]
mod engine_http;
pub mod engine_lifecycle_contract;
#[cfg(target_os = "macos")]
#[cfg_attr(debug_assertions, allow(dead_code))]
mod macos_engine_adapter;
#[cfg(target_os = "macos")]
mod macos_file_workflows;
#[cfg(all(target_os = "macos", feature = "macos-phase4-probe"))]
mod macos_phase4_probe;
#[cfg(target_os = "macos")]
#[cfg_attr(debug_assertions, allow(dead_code))]
mod macos_process_adapter;
#[cfg(target_os = "macos")]
mod macos_window_lifecycle;
pub mod window_lifecycle_contract;
#[cfg(target_os = "windows")]
#[cfg_attr(debug_assertions, allow(dead_code))]
mod windows_engine_adapter;
#[cfg(target_os = "windows")]
#[cfg_attr(debug_assertions, allow(dead_code))]
mod windows_process_adapter;
#[cfg(target_os = "windows")]
#[cfg_attr(debug_assertions, allow(dead_code))]
mod windows_window_lifecycle;
#[cfg(target_os = "macos")]
use macos_file_workflows::{
    choose_macos_import_audio, choose_macos_model_storage, release_macos_native_selections,
    save_macos_private_file,
};
#[cfg(target_os = "macos")]
use macos_window_lifecycle::{
    apply_window_lifecycle, set_window_launch_at_login, window_launch_at_login_state,
};

#[cfg(not(target_os = "macos"))]
#[tauri::command]
fn choose_macos_import_audio() -> Result<(), String> {
    Err("native Mac file workflow unsupported".to_string())
}

#[cfg(not(target_os = "macos"))]
#[tauri::command]
fn choose_macos_model_storage() -> Result<(), String> {
    Err("native Mac file workflow unsupported".to_string())
}

#[cfg(not(target_os = "macos"))]
#[tauri::command]
fn release_macos_native_selections(_tokens: Vec<String>) -> Result<(), String> {
    Err("native Mac file workflow unsupported".to_string())
}

#[cfg(not(target_os = "macos"))]
#[tauri::command]
fn save_macos_private_file(
    _kind: String,
    _default_name: String,
    _payload: Vec<u8>,
) -> Result<(), String> {
    Err("native Mac file workflow unsupported".to_string())
}

#[tauri::command]
const fn macos_private_file_workflows_supported() -> bool {
    cfg!(target_os = "macos")
}

// A main-window focus loss can mean either that the user switched apps or that
// another Whisperstream window took focus. The permission refresh owner uses
// this read-only distinction to ignore popup/HUD churn. It never queries or
// requests an OS permission.
#[tauri::command]
fn macos_application_is_active() -> bool {
    #[cfg(target_os = "macos")]
    {
        return objc2_app_kit::NSRunningApplication::currentApplication().isActive();
    }
    #[cfg(not(target_os = "macos"))]
    false
}
#[cfg(target_os = "windows")]
use windows_window_lifecycle::{
    apply_window_lifecycle, hide_and_clear_hud_window_region, hide_recording_control_window,
    hud_borderless_styles, redraw_current_window, set_current_window_logical_position,
    set_hud_bounds, set_hud_window_borderless_visibility, set_hud_window_focusable,
    hud_window_foreground_state,
    set_hud_window_region, set_window_launch_at_login, window_launch_at_login_state,
};

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
#[tauri::command]
fn apply_window_lifecycle(_generation: u64, _role: String, _action: String) -> Result<(), String> {
    Err("window lifecycle unsupported on this platform".to_string())
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
#[tauri::command]
fn window_launch_at_login_state() -> window_lifecycle_contract::LaunchAtLoginCommandResult {
    window_lifecycle_contract::LaunchAtLoginResult {
        state: window_lifecycle_contract::LaunchAtLoginState::Unsupported,
        failure: Some(window_lifecycle_contract::WindowFailure::Unsupported),
    }
    .into()
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
#[tauri::command]
fn set_window_launch_at_login(
    _enabled: bool,
) -> window_lifecycle_contract::LaunchAtLoginCommandResult {
    window_launch_at_login_state()
}

#[cfg(all(not(debug_assertions), any(target_os = "windows", target_os = "macos")))]
use engine_lifecycle_contract::{EngineLifecycle, EngineShutdownReason};
#[cfg(all(not(debug_assertions), target_os = "macos"))]
use macos_engine_adapter::{
    MacosEngineAdapter as PlatformEngineAdapter, MacosEngineSpawnMode, MACOS_ENGINE_SPAWN_MODE_ENV,
};
#[cfg(all(not(debug_assertions), any(target_os = "windows", target_os = "macos")))]
use std::sync::{Arc, Mutex};
#[cfg(all(not(debug_assertions), target_os = "windows"))]
use windows_engine_adapter::WindowsEngineAdapter as PlatformEngineAdapter;

#[cfg(all(not(debug_assertions), feature = "native-ui-test"))]
compile_error!(
    "the native-ui-test feature is debug-only and must never be enabled in release builds"
);

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn set_main_window_icon(window: &tauri::WebviewWindow) -> Result<(), String> {
    let icon =
        Image::from_bytes(include_bytes!("../icons/128x128.png")).map_err(|e| e.to_string())?;
    window.set_icon(icon).map_err(|e| e.to_string())
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn apply_main_window_icon(window: &tauri::WebviewWindow, phase: &str) {
    match set_main_window_icon(window) {
        Ok(()) => eprintln!("[tauri] main window icon applied phase={phase}"),
        Err(error) => {
            eprintln!("[tauri] main window icon apply failed phase={phase} error={error}")
        }
    }
}

#[cfg(target_os = "windows")]
mod webview_visibility;

#[cfg(target_os = "windows")]
mod windows_shutdown;

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn show_main_window(window: &tauri::WebviewWindow) {
    match window.show() {
        Ok(()) => apply_main_window_icon(window, "after-show"),
        Err(error) => eprintln!("[tauri] main window show failed error={error}"),
    }
}

fn starts_minimized<I, S>(args: I) -> bool
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    args.into_iter().any(|arg| arg.as_ref() == "--minimized")
}

// In dev builds, the Python backend is started manually (`npm run backend`),
// not spawned by Tauri, so the packaged lifecycle on RunEvent::Exit doesn't
// apply. Best-effort fire-and-forget POST so the
// manually-started backend exits with the desktop shell. Hardcoded to the
// default dev port (matches `npm run backend` and the production sidecar).
#[cfg(debug_assertions)]
pub(crate) fn signal_dev_backend_shutdown() {
    use std::io::Write;
    use std::net::TcpStream;
    use std::time::Duration;

    let Ok(addr) = "127.0.0.1:38420".parse() else {
        return;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, Duration::from_millis(200)) else {
        return;
    };
    let _ = stream.set_write_timeout(Some(Duration::from_millis(200)));
    let _ = stream.write_all(
        b"POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
    );
    eprintln!("[tauri] sent dev backend shutdown signal");
}

#[cfg(all(not(debug_assertions), any(target_os = "windows", target_os = "macos")))]
fn shutdown_packaged_engine(
    state: &PackagedEngine,
    reason: EngineShutdownReason,
) -> Result<(), String> {
    // Session-end cannot wait for readiness/update locks or launch a cleanup executable.
    // The independently retained process handle pins this exact engine PID.
    #[cfg(target_os = "windows")]
    if windows_shutdown::session_ending() {
        windows_shutdown::stop_sidecar_tree(state.1.pid());
        return Ok(());
    }
    let mut lifecycle = state.0
        .lock()
        .map_err(|_| "engine lifecycle unavailable".to_string())?;
    match lifecycle.shutdown(reason) {
        Ok(report) => {
            eprintln!(
                "[tauri] engine shutdown complete reason={:?} stage={:?} generation={} cleanup_verified={}",
                report.reason,
                report.stage,
                lifecycle
                    .identity()
                    .map(|identity| identity.generation)
                    .unwrap_or(0),
                report.cleanup_verified
            );
            Ok(())
        }
        Err(error) => {
            eprintln!(
                "[tauri] engine shutdown failed operation={:?} failure={:?}",
                error.operation, error.failure
            );
            Err("engine shutdown failed".to_string())
        }
    }
}

// Apply the HUD window's position AND size in one atomic native call.
//
// The frontend reconciler used to issue `setPosition` then `setSize` as two
// separate IPC round-trips. Between them the compositor can present a frame in
// which the window already sits at the new origin while still carrying the old
// size, which is what made the bottom-anchored recording seed visibly jump
// before expanding. One `SetWindowPos` moves and resizes together, so no
// intermediate geometry is ever presented.
//
// Coordinates are LOGICAL (what the JS side works in); this converts to physical
// with the window's own scale factor.
// Move only the current window in logical coordinates, synchronously.
//
// Tao's Windows `set_outer_position` adds SWP_ASYNCWINDOWPOS and invalidates
// the HWND after queueing the move. Its JavaScript promise can therefore
// resolve before Windows applies the final drag position. The dedicated
// recording control needs a completion boundary that means the move itself is
// done before its bounded redraw. SWP_NOCOPYBITS is deliberately omitted: the
// observed race is the queued move/invalidation, not stale saved client bits.
#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn set_current_window_logical_position(
    window: tauri::WebviewWindow,
    x: f64,
    y: f64,
) -> Result<(), String> {
    window
        .set_position(tauri::LogicalPosition::new(x, y))
        .map_err(|error| error.to_string())
}

// Non-Windows fallback so the command exists on every platform and the JS path
// stays uniform. Not atomic, but it is the same pair the frontend would have
// issued anyway.
#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn set_hud_bounds(
    window: tauri::WebviewWindow,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    use tauri::{LogicalPosition, LogicalSize};

    window
        .set_position(LogicalPosition::new(x, y))
        .map_err(|e| e.to_string())?;
    window
        .set_size(LogicalSize::new(width, height))
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[derive(serde::Serialize)]
struct HudBorderlessWindowState {
    visible: bool,
    style: String,
    exstyle: String,
    nonclient_policy: String,
}

// The fixed permanent recording control must not use tao's visibility flag.
// tao 0.34.5 rewrites GWL_STYLE from `to_window_styles()` for every non-empty
// WindowFlags diff. Those styles unconditionally contain caption, system-menu,
// window-edge, and app-window bits even when the window was created with
// decorations disabled and skipTaskbar enabled.
//
// This owner enforces a borderless frame before showing the fixed 84x40 HWND.
// It deliberately installs no HRGN: permanent-control animation stays inside
// one rectangular native envelope and changes only the DOM vessel.
// Force Windows to repaint the current top-level window and every child HWND.
// The dedicated recording control uses this only after pointer transition
// dispatch, where WebView2 can otherwise leave stale transparent backing
// pixels. This command deliberately changes no window state.
// The repaint seam is Windows-specific. Keep the command callable elsewhere
// so the frontend can use one event-order contract on every platform.
#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn redraw_current_window(_window: tauri::WebviewWindow) -> Result<(), String> {
    Ok(())
}

#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn set_hud_window_borderless_visibility(
    window: tauri::WebviewWindow,
    show: bool,
    hide: bool,
) -> Result<HudBorderlessWindowState, String> {
    if show == hide {
        return Err("HUD borderless visibility mutation requires exactly one action".to_string());
    }
    if hide {
        window.hide().map_err(|error| error.to_string())?;
    } else {
        window.show().map_err(|error| error.to_string())?;
    }
    Ok(HudBorderlessWindowState {
        visible: show,
        style: "unsupported".to_string(),
        exstyle: "unsupported".to_string(),
        nonclient_policy: "unsupported".to_string(),
    })
}

// The main HUD calls this before preparing any recording, Preview, or Claude
// surface. The permanent control lives on a different HWND, so its raw hide
// cannot mutate Tao's cached visibility or style state for the main HUD.
#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn hide_recording_control_window(app: tauri::AppHandle) -> Result<(), String> {
    if let Some(window) = app.get_webview_window("recording-control") {
        window.hide().map_err(|error| error.to_string())?;
    }
    Ok(())
}

// Restrict the HUD's native paint and input surface without resizing its
// WebView. The compact recording seed and expanded control share one 132x52
// layout. Both use explicit rounded regions, so hover/collapse cannot present a
// stale WebView frame and a WebView2 repaint cannot expose the rectangular
// backing surface around the active pill.
//
// Coordinates are LOGICAL and window-local. On successful SetWindowRgn,
// Windows owns the HRGN; on failure we must release it ourselves.
// The product ships on Windows. Keep the command registered elsewhere so the
// frontend call remains uniform in browser/dev builds.
#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn set_hud_window_region(
    window: tauri::WebviewWindow,
    show: bool,
    hide: bool,
    _restricted: bool,
    _x: f64,
    _y: f64,
    _width: f64,
    _height: f64,
    _corner_radius: f64,
) -> Result<(), String> {
    if show && hide {
        return Err("HUD window cannot be shown and hidden in one mutation".to_string());
    }
    if hide {
        window.hide().map_err(|e| e.to_string())?;
    }
    if show {
        window.show().map_err(|e| e.to_string())?;
    }
    Ok(())
}

// Keep the shaped/native HUD handoff deliberately smaller than the normal
// region command. The caller uses this only when returning presentation
// ownership to Tauri: the HWND must be physically hidden before Windows drops
// the HRGN, or the unrestricted backing window can be presented for a frame.
//
// The small sequencing seam also makes the load-bearing order testable without
// manufacturing an HWND in a unit test.
fn hide_then_clear_then_restore_hud_frame<Hide, Clear, Restore>(
    mut hide: Hide,
    mut clear: Clear,
    mut restore: Restore,
) -> Result<(), String>
where
    Hide: FnMut(),
    Clear: FnMut() -> Result<(), String>,
    Restore: FnMut() -> Result<(), String>,
{
    hide();
    clear()?;
    restore()
}

// There is no native region to clear outside Windows. Hide through the platform
// window API so the transition still starts from a non-presented surface.
#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn hide_and_clear_hud_window_region(window: tauri::WebviewWindow) -> Result<(), String> {
    hide_then_clear_then_restore_hud_frame(
        || {},
        || window.hide().map_err(|error| error.to_string()),
        || Ok(()),
    )
}

// Focusability for the HUD, owned natively for the same reason the region is.
//
// Tauri's `setFocusable` reaches tao's `WindowState::set_window_flags`, which
// diffs against tao's CACHED `WindowFlags` and then reapplies the whole set.
// Two consequences make it unusable here. `apply_diff` runs
// `if !new.contains(WindowFlags::VISIBLE) { ShowWindow(SW_HIDE) }`, and this
// HUD is shown by the raw `ShowWindow` in `set_hud_window_region` precisely so
// tao cannot restore caption state - so tao's cached VISIBLE is false forever
// and the first focusable toggle HID the live window. It then reapplies
// GWL_STYLE/GWL_EXSTYLE wholesale, undoing the frame stripping installed
// above. The window stayed painted-but-hidden for the rest of the session,
// because the reconciler had correctly recorded `visible: true` and therefore
// never owed another show.
//
// Toggling the one bit that actually governs activation keeps tao's cache out
// of the path entirely. Visibility is never touched here.
//
// Clearing the bit is necessary but NOT sufficient. Tauri's `setFocus` is also
// dead on this window: tao gates it on the same stale cached VISIBLE flag
// (`if is_visible && !is_minimized && !is_foreground`), so it silently returns
// without doing anything. That is why an expanded question card could never
// receive a keystroke - the option digits, Enter-to-submit, Escape-to-defer and
// arrow navigation were all implemented and all correct, they simply never
// reached a focused window. So this command owns activation too.
//
// The previous foreground window is remembered and restored when focusability
// is dropped, because this is a dictation app: leaving the HUD foreground after
// an answer would send the user's next keystrokes into a 380px overlay instead
// of the editor they were working in.
// The product ships on Windows. Keep the command registered elsewhere so the
// frontend call remains uniform in browser/dev builds.
#[cfg(not(any(target_os = "windows", target_os = "macos")))]
#[tauri::command]
fn set_hud_window_focusable(window: tauri::WebviewWindow, focusable: bool) -> Result<(), String> {
    window.set_focusable(focusable).map_err(|e| e.to_string())
}

#[cfg(target_os = "macos")]
#[tauri::command]
fn set_hud_window_focusable(window: tauri::WebviewWindow, focusable: bool) -> Result<(), String> {
    macos_window_lifecycle::set_agent_hud_focusable(window, focusable)
}

// Whether the HUD really holds keyboard focus. Windows answers from the
// foreground window (see windows_window_lifecycle); elsewhere the platform's
// key-window state is already truthful.
#[cfg(not(target_os = "windows"))]
#[tauri::command]
fn hud_window_foreground_state(window: tauri::WebviewWindow) -> Result<bool, String> {
    window.is_focused().map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    #[cfg(all(target_os = "macos", feature = "macos-phase4-probe"))]
    if let Some(code) = macos_phase4_probe::maybe_run_from_args() {
        std::process::exit(code);
    }

    #[cfg(target_os = "windows")]
    let _session_end_hook = windows_shutdown::SessionEndHook::install()
        .expect("failed to install session-end observer");
    let builder = tauri::Builder::default();
    #[allow(unused_mut)]
    let mut context = tauri::generate_context!();
    // The updater provider supplies its plugin configuration when the build's
    // Tauri config has none (a development run without the build overlay).
    #[cfg(official_updater)]
    official_updater::configure(&mut context);

    #[cfg(all(debug_assertions, feature = "native-ui-test"))]
    let builder = {
        println!("[NativeUiTest] rust-log-marker plugins=wdio,wdio-webdriver");
        builder
            .plugin(tauri_plugin_wdio::init())
            .plugin(tauri_plugin_wdio_webdriver::init())
    };

    #[cfg(not(all(debug_assertions, feature = "native-ui-test")))]
    let builder = builder.plugin(tauri_plugin_single_instance::init(
        |app, argv: Vec<String>, _cwd: String| {
            if argv.contains(&"--quit".to_string()) {
                eprintln!("[tauri] single-instance fired argv_quit=true");
                app.exit(0);
            } else {
                eprintln!("[tauri] single-instance fired argv_quit=false");
                #[cfg(target_os = "windows")]
                windows_window_lifecycle::show_main_window(app);
                #[cfg(target_os = "macos")]
                macos_window_lifecycle::show_main_window(app);
                #[cfg(not(any(target_os = "windows", target_os = "macos")))]
                if let Some(window) = app.get_webview_window("main") {
                    show_main_window(&window);
                    let _ = window.set_focus();
                }
            }
        },
    ));

    #[cfg(target_os = "windows")]
    let builder = builder.plugin(tauri_plugin_autostart::init(
        tauri_plugin_autostart::MacosLauncher::LaunchAgent,
        Some(vec!["--minimized"]),
    ));

    #[cfg(official_updater)]
    let builder = official_updater::register(builder);

    builder
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_opener::init())
        // Autostart writes an HKCU\...\Run value pointing at Tauri's own
        // current_exe() (the installed Whisperstream.exe), sidestepping the
        // old Python `startup.py` path that registered the Nuitka engine
        // sidecar from its transient onefile extraction cache. The `--minimized`
        // arg is appended to the Run value so a login launch starts hidden in
        // the tray (detected in setup() below).
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .invoke_handler(with_private_commands(tauri::generate_handler![
            apply_window_lifecycle,
            window_launch_at_login_state,
            set_window_launch_at_login,
            set_hud_bounds,
            set_current_window_logical_position,
            set_hud_window_borderless_visibility,
            redraw_current_window,
            hide_recording_control_window,
            set_hud_window_region,
            hide_and_clear_hud_window_region,
            set_hud_window_focusable,
            hud_window_foreground_state,
            choose_macos_import_audio,
            choose_macos_model_storage,
            release_macos_native_selections,
            save_macos_private_file,
            macos_private_file_workflows_supported,
            macos_application_is_active
        ]))
        .setup(|app| {
            // Launched via autostart? tauri-plugin-autostart appends
            // `--minimized` to the Run value, so a login launch starts hidden
            // in the tray instead of popping the settings window.
            let started_minimized = starts_minimized(std::env::args());

            #[cfg(target_os = "windows")]
            windows_window_lifecycle::install(
                app,
                if started_minimized {
                    window_lifecycle_contract::LaunchPresentation::Minimized
                } else {
                    window_lifecycle_contract::LaunchPresentation::Normal
                },
            )?;

            #[cfg(target_os = "windows")]
            webview_visibility::install(&app.get_webview_window("main").unwrap())?;

            #[cfg(target_os = "macos")]
            macos_file_workflows::initialize_native_selection_registry()
                .map_err(std::io::Error::other)?;

            #[cfg(target_os = "macos")]
            macos_window_lifecycle::install(
                app,
                if started_minimized {
                    window_lifecycle_contract::LaunchPresentation::Minimized
                } else {
                    window_lifecycle_contract::LaunchPresentation::Normal
                },
            )?;

            #[cfg(not(any(target_os = "windows", target_os = "macos")))]
            {
                // --- System tray ---
                let settings_item =
                    MenuItem::with_id(app, "settings", "Settings", true, None::<&str>)?;
                let quit_item = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
                let menu = Menu::with_items(app, &[&settings_item, &quit_item])?;

                let icon = Image::from_bytes(include_bytes!("../icons/32x32.png"))?;

                let _tray = TrayIconBuilder::new()
                    .icon(icon)
                    .menu(&menu)
                    .show_menu_on_left_click(false)
                    .tooltip("Whisperstream")
                    .on_menu_event(|app, event| match event.id.as_ref() {
                        "settings" => {
                            if let Some(window) = app.get_webview_window("main") {
                                show_main_window(&window);
                                let _ = window.set_focus();
                            }
                        }
                        "quit" => {
                            #[cfg(debug_assertions)]
                            signal_dev_backend_shutdown();
                            app.exit(0);
                        }
                        _ => {}
                    })
                    .on_tray_icon_event(|tray, event| {
                        let should_show = matches!(
                            event,
                            TrayIconEvent::Click {
                                button: MouseButton::Left,
                                button_state: MouseButtonState::Up,
                                ..
                            } | TrayIconEvent::DoubleClick {
                                button: MouseButton::Left,
                                ..
                            }
                        );
                        if should_show {
                            if let Some(window) = tray.app_handle().get_webview_window("main") {
                                show_main_window(&window);
                                let _ = window.unminimize();
                                let _ = window.set_focus();
                            }
                        }
                    })
                    .build(app)?;

                // Hide to tray on close instead of quitting
                let main_window = app.get_webview_window("main").unwrap();

                apply_main_window_icon(&main_window, "before-show");

                // The main window is created hidden (visible:false in tauri.conf.json) so
                // an autostart `--minimized` login launch never flashes it on screen. Show
                // it for a normal launch; an autostart launch stays hidden in the tray
                // until the user opens it from the tray icon.
                if !started_minimized {
                    show_main_window(&main_window);
                }
                let win_clone = main_window.clone();
                main_window.on_window_event(move |event| {
                    if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                        api.prevent_close();
                        let _ = win_clone.hide();
                    }
                });
            }

            // --- Packaged engine (native adapter owns only this child) ---
            #[cfg(all(not(debug_assertions), any(target_os = "windows", target_os = "macos")))]
            {
                #[cfg(target_os = "windows")]
                let adapter = PlatformEngineAdapter::new(app.handle().clone());
                #[cfg(target_os = "macos")]
                let adapter = {
                    let configured_mode = std::env::var_os(MACOS_ENGINE_SPAWN_MODE_ENV);
                    let spawn_mode =
                        MacosEngineSpawnMode::from_environment_value(configured_mode.as_deref())
                            .map_err(|_| {
                                std::io::Error::other("invalid macOS engine spawn mode")
                            })?;
                    PlatformEngineAdapter::new(app.handle().clone(), spawn_mode)
                };
                let mut lifecycle = EngineLifecycle::new(adapter);
                let identity = lifecycle.spawn().map_err(|error| {
                    std::io::Error::other(format!(
                        "packaged engine spawn failed operation={:?} failure={:?}",
                        error.operation, error.failure
                    ))
                })?;
                eprintln!(
                    "[tauri] engine spawned pid={} parent_pid={} generation={}",
                    identity.pid,
                    identity.parent_pid.unwrap_or(0),
                    identity.generation
                );

                let lifecycle = Arc::new(Mutex::new(lifecycle));
                #[cfg(target_os = "windows")]
                let retained_process = windows_shutdown::Process::open(identity.pid)
                    .map_err(std::io::Error::other)?;
                app.manage(PackagedEngine(
                    Arc::clone(&lifecycle),
                    #[cfg(target_os = "windows")]
                    retained_process,
                ));
                tauri::async_runtime::spawn_blocking(move || {
                    let Ok(mut lifecycle) = lifecycle.lock() else {
                        eprintln!("[tauri] engine readiness failed failure=lifecycle-unavailable");
                        return;
                    };
                    match lifecycle.await_ready() {
                        Ok(()) => {
                            eprintln!("[tauri] engine ready generation={}", identity.generation)
                        }
                        Err(error) => {
                            eprintln!(
                                "[tauri] engine readiness failed operation={:?} failure={:?}",
                                error.operation, error.failure
                            );
                            let _ = lifecycle.shutdown(EngineShutdownReason::AppExit);
                        }
                    }
                });
            }

            Ok(())
        })
        .build(context)
        .expect("error while building tauri application")
        .run(|_app_handle, _event| {
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Reopen { .. } = _event {
                macos_window_lifecycle::show_main_window(_app_handle);
            }
            #[cfg(target_os = "macos")]
            if let tauri::RunEvent::Exit = _event {
                macos_file_workflows::cleanup_native_selection_registry();
            }
            #[cfg(all(not(debug_assertions), any(target_os = "windows", target_os = "macos")))]
            if let tauri::RunEvent::Exit = _event {
                if let Some(state) = _app_handle.try_state::<PackagedEngine>() {
                    let _ = shutdown_packaged_engine(&state, EngineShutdownReason::AppExit);
                }
                #[cfg(target_os = "windows")]
                windows_shutdown::exit_if_session_ended();
            }
        });
}

#[cfg(test)]
mod tests {
    #[cfg(target_os = "windows")]
    use super::hud_borderless_styles;
    use super::{
        hide_then_clear_then_restore_hud_frame, macos_private_file_workflows_supported,
        starts_minimized,
    };
    use std::cell::RefCell;

    #[test]
    fn minimized_startup_stays_hidden_without_changing_normal_start() {
        assert!(starts_minimized(["Whisperstream.exe", "--minimized"]));
        assert!(!starts_minimized(["Whisperstream.exe"]));
    }

    #[test]
    fn private_file_workflow_capability_is_compile_target_authoritative() {
        assert_eq!(
            macos_private_file_workflows_supported(),
            cfg!(target_os = "macos")
        );
    }

    #[test]
    fn hud_handoff_hides_then_clears_then_restores_frame_policy() {
        let calls = RefCell::new(Vec::new());

        hide_then_clear_then_restore_hud_frame(
            || calls.borrow_mut().push("hide"),
            || {
                calls.borrow_mut().push("clear");
                Ok(())
            },
            || {
                calls.borrow_mut().push("restore");
                Ok(())
            },
        )
        .expect("HUD handoff should succeed");

        assert_eq!(*calls.borrow(), ["hide", "clear", "restore"]);
    }

    #[test]
    fn hud_handoff_reports_clear_failure_after_hiding() {
        let calls = RefCell::new(Vec::new());

        let result = hide_then_clear_then_restore_hud_frame(
            || calls.borrow_mut().push("hide"),
            || {
                calls.borrow_mut().push("clear");
                Err("clear failed".to_string())
            },
            || {
                calls.borrow_mut().push("restore");
                Ok(())
            },
        );

        assert_eq!(result, Err("clear failed".to_string()));
        assert_eq!(*calls.borrow(), ["hide", "clear"]);
    }

    #[test]
    fn hud_handoff_reports_frame_policy_restore_failure_last() {
        let calls = RefCell::new(Vec::new());

        let result = hide_then_clear_then_restore_hud_frame(
            || calls.borrow_mut().push("hide"),
            || {
                calls.borrow_mut().push("clear");
                Ok(())
            },
            || {
                calls.borrow_mut().push("restore");
                Err("restore failed".to_string())
            },
        );

        assert_eq!(result, Err("restore failed".to_string()));
        assert_eq!(*calls.borrow(), ["hide", "clear", "restore"]);
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn hud_borderless_styles_strip_the_persisted_native_frame() {
        // Captured from the actual persisted classic HUD window:
        // style=0x14CB0000 exstyle=0x08040118.
        assert_eq!(
            hud_borderless_styles(0x14CB0000, 0x08040118),
            (0x14000000, 0x08000018)
        );
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn hud_borderless_styles_preserve_visibility_and_nonclient_free_flags() {
        assert_eq!(
            hud_borderless_styles(0x04000000, 0x08000018),
            (0x04000000, 0x08000018)
        );
    }
}

#[cfg(all(not(debug_assertions), any(target_os = "windows", target_os = "macos")))]
struct PackagedEngine(
    Arc<Mutex<EngineLifecycle<PlatformEngineAdapter>>>,
    #[cfg(target_os = "windows")] windows_shutdown::Process,
);

// The library test harness does not receive the app binary's Tauri manifest.
// Its dialog imports need Common Controls v6 before the Windows loader runs.
#[cfg(all(test, target_os = "windows", target_env = "msvc"))]
#[used]
#[link_section = ".drectve"]
static TEST_COMMON_CONTROLS_MANIFEST: [u8; 184] = *br#" /MANIFEST:EMBED /MANIFESTDEPENDENCY:"type='win32' name='Microsoft.Windows.Common-Controls' version='6.0.0.0' processorArchitecture='*' publicKeyToken='6595b64144ccf1df' language='*'" "#;
