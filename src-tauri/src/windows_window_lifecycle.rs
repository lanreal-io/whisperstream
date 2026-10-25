//! Windows owner for native window, tray, startup, and HWND behavior.

use tauri::image::Image;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::Manager;
use tauri_plugin_autostart::ManagerExt;

use crate::window_lifecycle_contract::{
    LaunchAtLoginCommandResult, LaunchAtLoginResult, LaunchAtLoginState, LaunchPresentation,
    LogicalGeometry, WindowAction, WindowFailure, WindowLifecycle, WindowLifecyclePort,
    WindowLifecycleRequest, WindowRole,
};

const MAIN_WINDOW_ICON_RESOURCE_ID: u16 = 32512;
const MAIN_WINDOW_ICON_SLOTS: [u32; 2] = [
    windows::Win32::UI::WindowsAndMessaging::ICON_SMALL,
    windows::Win32::UI::WindowsAndMessaging::ICON_BIG,
];

fn set_main_window_icon(window: &tauri::WebviewWindow) -> Result<(), String> {
    use windows::core::PCWSTR;
    use windows::Win32::{
        Foundation::{HINSTANCE, LPARAM, WPARAM},
        System::LibraryLoader::GetModuleHandleW,
        UI::WindowsAndMessaging::{LoadIconW, SendMessageW, WM_SETICON},
    };

    let hwnd = window.hwnd().map_err(|error| error.to_string())?;
    let module = unsafe { GetModuleHandleW(None) }
        .map_err(|error| format!("could not get executable module handle: {error}"))?;
    let icon_name = PCWSTR::from_raw(MAIN_WINDOW_ICON_RESOURCE_ID as usize as *const u16);
    let icon = unsafe { LoadIconW(Some(HINSTANCE(module.0)), icon_name) }
        .map_err(|error| format!("could not load bundled icon resource: {error}"))?;

    for slot in MAIN_WINDOW_ICON_SLOTS {
        unsafe {
            SendMessageW(
                hwnd,
                WM_SETICON,
                Some(WPARAM(slot as usize)),
                Some(LPARAM(icon.0 as isize)),
            );
        }
    }
    Ok(())
}

fn apply_main_window_icon(window: &tauri::WebviewWindow, phase: &str) {
    match set_main_window_icon(window) {
        Ok(()) => eprintln!("[tauri] main window icon applied phase={phase}"),
        Err(error) => {
            eprintln!("[tauri] main window icon apply failed phase={phase} error={error}")
        }
    }
}

struct WindowsWindowPort {
    app: tauri::AppHandle,
}

impl WindowsWindowPort {
    fn window(&self, role: WindowRole) -> Result<tauri::WebviewWindow, WindowFailure> {
        let label = match role {
            WindowRole::Main => "main",
            WindowRole::Hud => "hud",
            WindowRole::Snap => "snap-guide",
            WindowRole::Review => "popup-review",
            WindowRole::Update => "popup-update",
            WindowRole::Eula => "popup-eula",
            WindowRole::Error => "popup-error",
        };
        self.app
            .get_webview_window(label)
            .ok_or(WindowFailure::NotFound)
    }
}

impl WindowLifecyclePort for WindowsWindowPort {
    fn mutate(
        &mut self,
        role: WindowRole,
        action: WindowAction,
        geometry: Option<&LogicalGeometry>,
    ) -> Result<(bool, bool), WindowFailure> {
        if action == WindowAction::Quit {
            #[cfg(debug_assertions)]
            crate::signal_dev_backend_shutdown();
            self.app.exit(0);
            return Ok((false, false));
        }
        let window = self.window(role)?;
        if let Some(bounds) = geometry {
            use tauri::{LogicalPosition, LogicalSize};
            window
                .set_position(LogicalPosition::new(bounds.x, bounds.y))
                .map_err(|_| WindowFailure::DisplayUnavailable)?;
            window
                .set_size(LogicalSize::new(bounds.width, bounds.height))
                .map_err(|_| WindowFailure::DisplayUnavailable)?;
        }
        match action {
            WindowAction::Show => {
                window.show().map_err(|_| WindowFailure::NativeFailure)?;
                if role == WindowRole::Main {
                    apply_main_window_icon(&window, "after-show");
                }
            }
            WindowAction::Hide => window.hide().map_err(|_| WindowFailure::NativeFailure)?,
            WindowAction::Focus => window
                .set_focus()
                .map_err(|_| WindowFailure::NativeFailure)?,
            WindowAction::Minimize => window
                .minimize()
                .map_err(|_| WindowFailure::NativeFailure)?,
            WindowAction::Restore => {
                window
                    .unminimize()
                    .map_err(|_| WindowFailure::NativeFailure)?;
                window.show().map_err(|_| WindowFailure::NativeFailure)?;
            }
            WindowAction::CloseRequest => {
                window.close().map_err(|_| WindowFailure::NativeFailure)?
            }
            WindowAction::Quit => unreachable!(),
        }
        let visible = window.is_visible().unwrap_or(false);
        let focused = window.is_focused().unwrap_or(false);
        Ok((visible, focused))
    }

    fn set_launch_at_login(&mut self, enabled: bool) -> LaunchAtLoginResult {
        let autostart = self.app.autolaunch();
        let outcome = if enabled {
            autostart.enable()
        } else {
            autostart.disable()
        };
        match outcome {
            Ok(()) => LaunchAtLoginResult {
                state: if enabled {
                    LaunchAtLoginState::Enabled
                } else {
                    LaunchAtLoginState::Disabled
                },
                failure: None,
            },
            Err(_) => LaunchAtLoginResult {
                state: LaunchAtLoginState::Unavailable,
                failure: Some(WindowFailure::NativeFailure),
            },
        }
    }

    fn launch_at_login_state(&self) -> LaunchAtLoginResult {
        match self.app.autolaunch().is_enabled() {
            Ok(enabled) => LaunchAtLoginResult {
                state: if enabled {
                    LaunchAtLoginState::Enabled
                } else {
                    LaunchAtLoginState::Disabled
                },
                failure: None,
            },
            Err(_) => LaunchAtLoginResult {
                state: LaunchAtLoginState::Unavailable,
                failure: Some(WindowFailure::NativeFailure),
            },
        }
    }
}

fn lifecycle(app: &tauri::AppHandle) -> WindowLifecycle<WindowsWindowPort> {
    WindowLifecycle::new(WindowsWindowPort { app: app.clone() })
}

fn request(app: &tauri::AppHandle, role: WindowRole, action: WindowAction) {
    let mut lifecycle = lifecycle(app);
    let result = lifecycle.apply(WindowLifecycleRequest {
        generation: lifecycle.generation(),
        role,
        action,
        geometry: None,
    });
    if let Some(failure) = result.failure {
        eprintln!(
            "[tauri] window lifecycle failed role={:?} action={:?} failure={:?}",
            role, action, failure
        );
    }
}

pub(crate) fn show_main_window(app: &tauri::AppHandle) {
    request(app, WindowRole::Main, WindowAction::Show);
    request(app, WindowRole::Main, WindowAction::Restore);
    request(app, WindowRole::Main, WindowAction::Focus);
}

// Called only by the official installer's private updater.
#[cfg_attr(not(official_updater), allow(dead_code))]
pub(crate) fn prepare_for_update(app: &tauri::AppHandle) {
    request(app, WindowRole::Hud, WindowAction::Hide);
    request(app, WindowRole::Update, WindowAction::Hide);
}

fn parse_role(value: &str) -> Option<WindowRole> {
    Some(match value {
        "main" => WindowRole::Main,
        "hud" => WindowRole::Hud,
        "snap" => WindowRole::Snap,
        "review" => WindowRole::Review,
        "update" => WindowRole::Update,
        "eula" => WindowRole::Eula,
        "error" => WindowRole::Error,
        _ => return None,
    })
}

fn parse_action(value: &str) -> Option<WindowAction> {
    Some(match value {
        "show" => WindowAction::Show,
        "hide" => WindowAction::Hide,
        "focus" => WindowAction::Focus,
        "minimize" => WindowAction::Minimize,
        "restore" => WindowAction::Restore,
        "close-request" => WindowAction::CloseRequest,
        "quit" => WindowAction::Quit,
        _ => return None,
    })
}

#[tauri::command]
pub(crate) fn apply_window_lifecycle(
    app: tauri::AppHandle,
    generation: u64,
    role: String,
    action: String,
) -> Result<(), String> {
    let role = parse_role(&role).ok_or_else(|| "window lifecycle role invalid".to_string())?;
    let action =
        parse_action(&action).ok_or_else(|| "window lifecycle action invalid".to_string())?;
    let mut lifecycle = lifecycle(&app);
    let result = lifecycle.apply(WindowLifecycleRequest {
        generation,
        role,
        action,
        geometry: None,
    });
    result.failure.map_or(Ok(()), |failure| {
        Err(format!("window lifecycle failed: {failure:?}"))
    })
}

#[tauri::command]
pub(crate) fn window_launch_at_login_state(app: tauri::AppHandle) -> LaunchAtLoginCommandResult {
    lifecycle(&app).launch_at_login_state().into()
}

#[tauri::command]
pub(crate) fn set_window_launch_at_login(
    app: tauri::AppHandle,
    enabled: bool,
) -> LaunchAtLoginCommandResult {
    let mut lifecycle = lifecycle(&app);
    lifecycle.set_launch_at_login(enabled).into()
}

#[tauri::command]
pub(crate) fn set_current_window_logical_position(
    window: tauri::WebviewWindow,
    x: f64,
    y: f64,
) -> Result<(), String> {
    use windows::Win32::UI::WindowsAndMessaging::{
        SetWindowPos, SWP_NOACTIVATE, SWP_NOSIZE, SWP_NOZORDER,
    };

    let scale = window.scale_factor().map_err(|error| error.to_string())?;
    let hwnd = window.hwnd().map_err(|error| error.to_string())?;
    let px = (x * scale).round() as i32;
    let py = (y * scale).round() as i32;
    unsafe {
        SetWindowPos(
            hwnd,
            None,
            px,
            py,
            0,
            0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
        .map_err(|error| error.to_string())?;
    }
    Ok(())
}

pub(crate) fn hud_borderless_styles(
    current_style: isize,
    current_ex_style: isize,
) -> (isize, isize) {
    use windows::Win32::UI::WindowsAndMessaging::{
        WS_CAPTION, WS_EX_APPWINDOW, WS_EX_CLIENTEDGE, WS_EX_CONTEXTHELP, WS_EX_DLGMODALFRAME,
        WS_EX_STATICEDGE, WS_EX_WINDOWEDGE, WS_MAXIMIZEBOX, WS_MINIMIZEBOX, WS_SYSMENU,
        WS_THICKFRAME,
    };

    let nonclient_style_mask =
        (WS_CAPTION.0 | WS_THICKFRAME.0 | WS_SYSMENU.0 | WS_MINIMIZEBOX.0 | WS_MAXIMIZEBOX.0)
            as isize;
    let nonclient_ex_style_mask = (WS_EX_APPWINDOW.0
        | WS_EX_CLIENTEDGE.0
        | WS_EX_CONTEXTHELP.0
        | WS_EX_DLGMODALFRAME.0
        | WS_EX_STATICEDGE.0
        | WS_EX_WINDOWEDGE.0) as isize;
    (
        current_style & !nonclient_style_mask,
        current_ex_style & !nonclient_ex_style_mask,
    )
}

#[tauri::command]
pub(crate) fn set_hud_window_borderless_visibility(
    window: tauri::WebviewWindow,
    show: bool,
    hide: bool,
) -> Result<crate::HudBorderlessWindowState, String> {
    use std::ffi::c_void;
    use windows::Win32::Graphics::Dwm::{
        DwmSetWindowAttribute, DWMNCRP_USEWINDOWSTYLE, DWMWA_NCRENDERING_POLICY,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        GetWindowLongPtrW, IsWindowVisible, SetWindowLongPtrW, SetWindowPos, ShowWindow,
        GWL_EXSTYLE, GWL_STYLE, SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE,
        SWP_NOZORDER, SW_HIDE, SW_SHOWNOACTIVATE,
    };

    if show == hide {
        return Err("HUD borderless visibility mutation requires exactly one action".to_string());
    }
    let hwnd = window.hwnd().map_err(|error| error.to_string())?;
    let restore_nonclient_policy = || {
        let policy = DWMNCRP_USEWINDOWSTYLE;
        unsafe {
            DwmSetWindowAttribute(
                hwnd,
                DWMWA_NCRENDERING_POLICY,
                &policy as *const _ as *const c_void,
                std::mem::size_of_val(&policy) as u32,
            )
        }
        .map_err(|error| format!("HUD borderless nonclient policy restore failed: {error}"))
    };
    if hide {
        unsafe {
            let _ = ShowWindow(hwnd, SW_HIDE);
        }
        restore_nonclient_policy()?;
        let hidden_style = unsafe { GetWindowLongPtrW(hwnd, GWL_STYLE) };
        let hidden_ex_style = unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) };
        let hidden_visible = unsafe { IsWindowVisible(hwnd).as_bool() };
        eprintln!(
            "[HUD] native borderless window hidden visible={} style=0x{:X} exstyle=0x{:X}",
            hidden_visible, hidden_style, hidden_ex_style
        );
        return Ok(crate::HudBorderlessWindowState {
            visible: hidden_visible,
            style: format!("0x{:X}", hidden_style),
            exstyle: format!("0x{:X}", hidden_ex_style),
            nonclient_policy: "use_window_style".to_string(),
        });
    }

    restore_nonclient_policy()?;
    let current_style = unsafe { GetWindowLongPtrW(hwnd, GWL_STYLE) };
    let current_ex_style = unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) };
    let (borderless_style, borderless_ex_style) =
        hud_borderless_styles(current_style, current_ex_style);
    eprintln!(
        "[HUD] native borderless show style_before=0x{:X} style_target=0x{:X} exstyle_before=0x{:X} exstyle_target=0x{:X}",
        current_style, borderless_style, current_ex_style, borderless_ex_style
    );
    if borderless_style != current_style || borderless_ex_style != current_ex_style {
        unsafe {
            SetWindowLongPtrW(hwnd, GWL_STYLE, borderless_style);
            SetWindowLongPtrW(hwnd, GWL_EXSTYLE, borderless_ex_style);
            SetWindowPos(
                hwnd,
                None,
                0,
                0,
                0,
                0,
                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
            .map_err(|error| error.to_string())?;
        }
    }
    unsafe {
        let _ = ShowWindow(hwnd, SW_SHOWNOACTIVATE);
    }
    let shown_style = unsafe { GetWindowLongPtrW(hwnd, GWL_STYLE) };
    let shown_ex_style = unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) };
    let shown_visible = unsafe { IsWindowVisible(hwnd).as_bool() };
    let (validated_style, validated_ex_style) = hud_borderless_styles(shown_style, shown_ex_style);
    if !shown_visible || shown_style != validated_style || shown_ex_style != validated_ex_style {
        unsafe {
            let _ = ShowWindow(hwnd, SW_HIDE);
        }
        return Err(format!(
            "HUD borderless invariant failed after native show visible={} style=0x{:X} expected=0x{:X} exstyle=0x{:X} expected_exstyle=0x{:X}",
            shown_visible, shown_style, validated_style, shown_ex_style, validated_ex_style
        ));
    }
    eprintln!(
        "[HUD] native borderless window shown visible=true style=0x{:X} exstyle=0x{:X} nonclient_policy=use_window_style",
        shown_style, shown_ex_style
    );
    Ok(crate::HudBorderlessWindowState {
        visible: shown_visible,
        style: format!("0x{:X}", shown_style),
        exstyle: format!("0x{:X}", shown_ex_style),
        nonclient_policy: "use_window_style".to_string(),
    })
}

#[tauri::command]
pub(crate) fn redraw_current_window(window: tauri::WebviewWindow) -> Result<(), String> {
    use windows::Win32::Graphics::Gdi::{
        RedrawWindow, RDW_ALLCHILDREN, RDW_ERASE, RDW_FRAME, RDW_INVALIDATE, RDW_UPDATENOW,
    };

    let hwnd = window.hwnd().map_err(|error| error.to_string())?;
    let redrawn = unsafe {
        RedrawWindow(
            Some(hwnd),
            None,
            None,
            RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW | RDW_FRAME,
        )
    };
    if redrawn.as_bool() {
        Ok(())
    } else {
        Err(format!(
            "current window redraw failed: {}",
            windows::core::Error::from_win32()
        ))
    }
}

#[tauri::command]
pub(crate) fn hide_recording_control_window(app: tauri::AppHandle) -> Result<(), String> {
    use windows::Win32::UI::WindowsAndMessaging::{ShowWindow, SW_HIDE};

    let Some(window) = app.get_webview_window("recording-control") else {
        return Ok(());
    };
    let hwnd = window.hwnd().map_err(|error| error.to_string())?;
    unsafe {
        let _ = ShowWindow(hwnd, SW_HIDE);
    }
    eprintln!("[HUD] dedicated recording control hidden before main claim");
    Ok(())
}

#[tauri::command]
pub(crate) fn set_hud_bounds(
    window: tauri::WebviewWindow,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
) -> Result<(), String> {
    use windows::Win32::UI::WindowsAndMessaging::{SetWindowPos, SWP_NOACTIVATE, SWP_NOZORDER};

    let scale = window.scale_factor().map_err(|error| error.to_string())?;
    let hwnd = window.hwnd().map_err(|error| error.to_string())?;
    unsafe {
        SetWindowPos(
            hwnd,
            None,
            (x * scale).round() as i32,
            (y * scale).round() as i32,
            (width * scale).round() as i32,
            (height * scale).round() as i32,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        .map_err(|error| error.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub(crate) fn set_hud_window_region(
    window: tauri::WebviewWindow,
    show: bool,
    hide: bool,
    restricted: bool,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    corner_radius: f64,
) -> Result<(), String> {
    use std::ffi::c_void;
    use windows::Win32::Graphics::Dwm::{
        DWMNCRP_USEWINDOWSTYLE, DWMWA_NCRENDERING_POLICY, DwmSetWindowAttribute,
    };
    use windows::Win32::Graphics::Gdi::{
        CreateRectRgn, CreateRoundRectRgn, DeleteObject, HGDIOBJ, SetWindowRgn,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        GWL_STYLE, GetWindowLongPtrW, SW_HIDE, SW_SHOWNOACTIVATE, SWP_FRAMECHANGED,
        SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, SetWindowLongPtrW,
        SetWindowPos, ShowWindow, WS_CAPTION, WS_MAXIMIZEBOX, WS_MINIMIZEBOX,
        WS_SYSMENU, WS_THICKFRAME,
    };

    if show && hide {
        return Err("HUD window cannot be shown and hidden in one mutation".to_string());
    }
    if !x.is_finite()
        || !y.is_finite()
        || !width.is_finite()
        || !height.is_finite()
        || !corner_radius.is_finite()
        || width <= 0.0
        || height <= 0.0
        || corner_radius < 0.0
        || corner_radius > width.min(height) / 2.0
    {
        return Err("HUD window region must be finite, non-empty, and have a valid corner radius".to_string());
    }
    if !restricted && (x != 0.0 || y != 0.0 || corner_radius != 0.0) {
        return Err(
            "Full HUD window region must start at the window origin and be rectangular"
                .to_string(),
        );
    }

    install_hud_frame_style_guard(&window).map_err(|e| e.to_string())?;
    let hwnd = window.hwnd().map_err(|e| e.to_string())?;

    // Native show bypasses Tauri/tao so it cannot restore dormant frame state.
    // Hide must use the same ownership boundary: Tauri can otherwise retain a
    // stale "already hidden" cache and skip hiding an HWND shown directly.
    // Hide first so the following region/style work cannot present a frame.
    if hide {
        unsafe {
            let _ = ShowWindow(hwnd, SW_HIDE);
        }
        eprintln!("[HUD] native window hidden before region installation");
    }

    // The shared HWND must keep normal DWM frame rendering even while a shaped
    // agent panel owns it. Disabling it here used to recreate the classic-frame
    // condition from Incident 21: a clean, possibly visible Tauri window still
    // has caption bits until the style strip below, and Tauri reinstalls those
    // bits when recording takes over. Native show plus the explicit style strip
    // already protect shaped panels; disabling DWM is neither needed nor safe.
    let policy = DWMNCRP_USEWINDOWSTYLE;
    unsafe {
        DwmSetWindowAttribute(
            hwnd,
            DWMWA_NCRENDERING_POLICY,
            &policy as *const _ as *const c_void,
            std::mem::size_of_val(&policy) as u32,
        )
    }
    .map_err(|error| format!("HUD region nonclient policy restore failed: {error}"))?;

    // A transparent Tao window can retain the standard caption/style bits even
    // when Tauri's `decorations: false` makes its client rect cover the whole
    // window. SetWindowRgn switches Windows onto a GDI clipping path that can
    // make those dormant non-client pixels paint again as a classic
    // "Whisperstream  -  X" title bar. Strip the bits explicitly before the
    // first region is installed, then notify Windows that the non-client frame
    // changed. Subsequent region swaps are style-stable and cannot resurrect
    // native chrome.
    let caption_mask = (WS_CAPTION.0
        | WS_THICKFRAME.0
        | WS_SYSMENU.0
        | WS_MINIMIZEBOX.0
        | WS_MAXIMIZEBOX.0) as isize;
    let current_style = unsafe { GetWindowLongPtrW(hwnd, GWL_STYLE) };
    let borderless_style = current_style & !caption_mask;
    eprintln!(
        "[HUD] native region requested show={} hide={} restricted={} style_before=0x{:X} style_target=0x{:X}",
        show, hide, restricted, current_style, borderless_style
    );
    if borderless_style != current_style {
        unsafe {
            SetWindowLongPtrW(hwnd, GWL_STYLE, borderless_style);
            SetWindowPos(
                hwnd,
                None,
                0,
                0,
                0,
                0,
                SWP_FRAMECHANGED
                    | SWP_NOMOVE
                    | SWP_NOSIZE
                    | SWP_NOZORDER
                    | SWP_NOACTIVATE,
            )
            .map_err(|e| e.to_string())?;
        }
        let applied_style = unsafe { GetWindowLongPtrW(hwnd, GWL_STYLE) };
        eprintln!("[HUD] native frame stripped style_after=0x{:X}", applied_style);
    }

    let scale = window.scale_factor().map_err(|e| e.to_string())?;
    let left = (x * scale).round() as i32;
    let top = (y * scale).round() as i32;
    let right = ((x + width) * scale).round() as i32;
    let bottom = ((y + height) * scale).round() as i32;
    let corner_diameter = (corner_radius * 2.0 * scale).round() as i32;
    let region = if corner_diameter > 0 {
        unsafe {
            CreateRoundRectRgn(
                left,
                top,
                right,
                bottom,
                corner_diameter,
                corner_diameter,
            )
        }
    } else {
        unsafe { CreateRectRgn(left, top, right, bottom) }
    };
    if region.is_invalid() {
        return Err(windows::core::Error::from_win32().to_string());
    }

    // Never clear back to Windows' default region. That handoff can expose a
    // transient native frame (icon and caption buttons) even though Tauri
    // configured this window without decorations. Every presentation uses an
    // explicit region instead, rounded to the persistent pill where possible.
    // Redraw is required when the region grows. Without it, Windows exposes
    // whatever pixels were last stored outside the compact clip, including
    // dormant non-client pixels painted during the initial Tauri show.
    let result = unsafe { SetWindowRgn(hwnd, Some(region), true) };
    if result == 0 {
        unsafe {
            let _ = DeleteObject(HGDIOBJ::from(region));
        }
        return Err(windows::core::Error::from_win32().to_string());
    }

    // Never call Tauri/tao's show path for this HUD. It can restore caption
    // styles and clear the region while making the window visible, which lets
    // Windows present one classic mini-window before the following mutation.
    // The HWND is already borderless and clipped here, so native show is
    // the final operation and cannot expose an unrestricted state.
    if show {
        unsafe {
            let _ = ShowWindow(hwnd, SW_SHOWNOACTIVATE);
        }
        eprintln!("[HUD] native window shown after region installation");
    }

    Ok(())
}

#[tauri::command]
pub(crate) fn hide_and_clear_hud_window_region(window: tauri::WebviewWindow) -> Result<(), String> {
    use std::ffi::c_void;
    use windows::Win32::Graphics::Dwm::{
        DwmSetWindowAttribute, DWMNCRP_USEWINDOWSTYLE, DWMWA_NCRENDERING_POLICY,
    };
    use windows::Win32::Graphics::Gdi::SetWindowRgn;
    use windows::Win32::UI::WindowsAndMessaging::{ShowWindow, SW_HIDE};

    let hwnd = window.hwnd().map_err(|error| error.to_string())?;
    crate::hide_then_clear_then_restore_hud_frame(
        || unsafe {
            let _ = ShowWindow(hwnd, SW_HIDE);
        },
        || {
            let result = unsafe { SetWindowRgn(hwnd, None, true) };
            if result == 0 {
                Err(windows::core::Error::from_win32().to_string())
            } else {
                Ok(())
            }
        },
        || {
            let policy = DWMNCRP_USEWINDOWSTYLE;
            unsafe {
                DwmSetWindowAttribute(
                    hwnd,
                    DWMWA_NCRENDERING_POLICY,
                    &policy as *const _ as *const c_void,
                    std::mem::size_of_val(&policy) as u32,
                )
            }
            .map_err(|error| error.to_string())
        },
    )?;
    eprintln!(
        "[HUD] native window hidden region cleared nonclient_policy=use_window_style for Tauri presentation"
    );
    Ok(())
}

static HUD_PREVIOUS_FOREGROUND: std::sync::Mutex<Option<isize>> = std::sync::Mutex::new(None);

/// The focusability the HUD last asked for. The frame-style guard reads it so
/// a later style rewrite (tao reapplies its cached flags on every Tauri show)
/// cannot put `WS_EX_NOACTIVATE` back while the HUD wants keyboard focus.
static HUD_WANTS_FOCUSABLE: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);

/// The extended style the HUD may hold: no activation bit while it is meant
/// to take keyboard focus, whatever a style rewrite proposes.
pub(crate) fn hud_ex_style_for_focus(ex_style: isize, focusable: bool) -> isize {
    use windows::Win32::UI::WindowsAndMessaging::WS_EX_NOACTIVATE;
    if focusable {
        ex_style & !(WS_EX_NOACTIVATE.0 as isize)
    } else {
        ex_style
    }
}

/// Whether the HUD is the real foreground window. The HUD reports `focused`
/// to the backend from this, never from WebView focus events alone: WebView2
/// can focus its document on a click while another window keeps the keyboard.
#[tauri::command]
pub(crate) fn hud_window_foreground_state(window: tauri::WebviewWindow) -> Result<bool, String> {
    use windows::Win32::UI::WindowsAndMessaging::GetForegroundWindow;
    let hwnd = window.hwnd().map_err(|error| error.to_string())?;
    Ok(unsafe { GetForegroundWindow() } == hwnd)
}

#[tauri::command]
pub(crate) fn set_hud_window_focusable(
    window: tauri::WebviewWindow,
    focusable: bool,
) -> Result<(), String> {
    use windows::Win32::Foundation::HWND;
    use windows::Win32::System::Threading::{AttachThreadInput, GetCurrentThreadId};
    use windows::Win32::UI::Input::KeyboardAndMouse::{SetActiveWindow, SetFocus};
    use windows::Win32::UI::WindowsAndMessaging::{
        BringWindowToTop, GetForegroundWindow, GetWindowLongPtrW, GetWindowThreadProcessId,
        IsWindow, IsWindowVisible, SetForegroundWindow, SetWindowLongPtrW, SetWindowPos, GWL_EXSTYLE,
        SWP_FRAMECHANGED, SWP_NOACTIVATE, SWP_NOMOVE, SWP_NOSIZE, SWP_NOZORDER, WS_EX_NOACTIVATE,
    };

    unsafe fn activate(hwnd: HWND) {
        let foreground = unsafe { GetForegroundWindow() };
        if foreground == hwnd {
            return;
        }
        let this_thread = unsafe { GetCurrentThreadId() };
        let foreground_thread = if foreground.0.is_null() {
            0
        } else {
            unsafe { GetWindowThreadProcessId(foreground, None) }
        };
        let attached = foreground_thread != 0
            && foreground_thread != this_thread
            && unsafe { AttachThreadInput(this_thread, foreground_thread, true) }.as_bool();
        let _ = unsafe { BringWindowToTop(hwnd) };
        let _ = unsafe { SetForegroundWindow(hwnd) };
        let _ = unsafe { SetActiveWindow(hwnd) };
        let _ = unsafe { SetFocus(Some(hwnd)) };
        if attached {
            let _ = unsafe { AttachThreadInput(this_thread, foreground_thread, false) };
        }
    }

    let hwnd = window.hwnd().map_err(|error| error.to_string())?;
    HUD_WANTS_FOCUSABLE.store(focusable, std::sync::atomic::Ordering::SeqCst);
    let current = unsafe { GetWindowLongPtrW(hwnd, GWL_EXSTYLE) };
    let target = if focusable {
        current & !(WS_EX_NOACTIVATE.0 as isize)
    } else {
        current | WS_EX_NOACTIVATE.0 as isize
    };
    if focusable {
        let previous = unsafe { GetForegroundWindow() };
        if !previous.0.is_null() && previous != hwnd {
            if let Ok(mut slot) = HUD_PREVIOUS_FOREGROUND.lock() {
                *slot = Some(previous.0 as isize);
            }
        }
    }
    if target != current {
        unsafe {
            SetWindowLongPtrW(hwnd, GWL_EXSTYLE, target);
            SetWindowPos(
                hwnd,
                None,
                0,
                0,
                0,
                0,
                SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
            .map_err(|error| error.to_string())?;
        }
    }
    if focusable {
        unsafe { activate(hwnd) };
        // Content-free: whether activation actually reached the foreground.
        let foreground = unsafe { GetForegroundWindow() } == hwnd;
        eprintln!("[HUD] native activation foreground={foreground}");
    } else if let Ok(mut slot) = HUD_PREVIOUS_FOREGROUND.lock() {
        if let Some(previous) = slot.take() {
            let previous = HWND(previous as *mut _);
            unsafe {
                if IsWindow(Some(previous)).as_bool()
                    && IsWindowVisible(previous).as_bool()
                    && GetForegroundWindow() == hwnd
                {
                    activate(previous);
                }
            }
        }
    }
    eprintln!("[HUD] native focusable applied focusable={focusable}");
    Ok(())
}

pub(crate) fn install(app: &mut tauri::App, presentation: LaunchPresentation) -> tauri::Result<()> {
    #[cfg(not(debug_assertions))]
    {
        let mut lifecycle = lifecycle(app.handle());
        if lifecycle.launch_at_login_state().state == LaunchAtLoginState::Enabled {
            let result = lifecycle.set_launch_at_login(true);
            if result.failure.is_none() {
                eprintln!("[tauri] autostart reconciled to current_exe");
            } else {
                eprintln!("[tauri] autostart reconcile failed failure=native-failure");
            }
        }
    }

    let settings_item = MenuItem::with_id(app, "settings", "Settings", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&settings_item, &quit_item])?;
    let icon = Image::from_bytes(include_bytes!("../icons/32x32.png"))?;
    let _tray = TrayIconBuilder::new()
        .icon(icon)
        .menu(&menu)
        .show_menu_on_left_click(false)
        .tooltip("Whisperstream")
        .on_menu_event(|app, event| match event.id.as_ref() {
            "settings" => show_main_window(app),
            "quit" => request(app, WindowRole::Main, WindowAction::Quit),
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
                show_main_window(tray.app_handle());
            }
        })
        .build(app)?;

    let main_window = app
        .get_webview_window("main")
        .ok_or_else(|| tauri::Error::WindowNotFound)?;
    apply_main_window_icon(&main_window, "before-show");
    let mut lifecycle = lifecycle(app.handle());
    let launch = lifecycle.launch(presentation);
    if launch.failure.is_some() {
        return Err(tauri::Error::WindowNotFound);
    }
    let app_handle = app.handle().clone();
    main_window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            request(&app_handle, WindowRole::Main, WindowAction::CloseRequest);
        }
    });
    Ok(())
}

// Tao reapplies caption/edge styles on visibility changes, after ShowWindow.
// Reject those bits before Windows installs them; stripping them afterward
// leaves a compositor frame in which a real title bar can be presented.
#[cfg(target_os = "windows")]
unsafe extern "system" fn hud_frame_style_guard(
    hwnd: windows::Win32::Foundation::HWND,
    message: u32,
    wparam: windows::Win32::Foundation::WPARAM,
    lparam: windows::Win32::Foundation::LPARAM,
    subclass_id: usize,
    _reference: usize,
) -> windows::Win32::Foundation::LRESULT {
    use windows::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass};
    use windows::Win32::UI::WindowsAndMessaging::{
        GWL_EXSTYLE, GWL_STYLE, STYLESTRUCT, WM_NCACTIVATE, WM_NCDESTROY, WM_STYLECHANGING,
    };

    if message == WM_NCACTIVATE {
        // The first Tauri show after a shaped Agent Speak surface can ask
        // Windows to paint an inactive caption even with borderless styles.
        // Keep activation processing, but suppress only its nonclient paint.
        // https://learn.microsoft.com/en-us/windows/win32/winmsg/wm-ncactivate
        return unsafe {
            DefSubclassProc(hwnd, message, wparam, windows::Win32::Foundation::LPARAM(-1))
        };
    }

    if message == WM_STYLECHANGING && lparam.0 != 0 {
        // Windows owns this structure for the synchronous message dispatch.
        let styles = unsafe { &mut *(lparam.0 as *mut STYLESTRUCT) };
        let proposed = styles.styleNew as isize;
        let filtered = match wparam.0 as i32 {
            value if value == GWL_STYLE.0 => hud_borderless_styles(proposed, 0).0,
            value if value == GWL_EXSTYLE.0 => hud_ex_style_for_focus(
                hud_borderless_styles(0, proposed).1,
                HUD_WANTS_FOCUSABLE.load(std::sync::atomic::Ordering::SeqCst),
            ),
            _ => proposed,
        };
        if proposed != filtered {
            eprintln!("[HUD] frame style rejected index={} proposed={proposed:#x} applied={filtered:#x}", wparam.0 as i32);
        }
        styles.styleNew = filtered as u32;
    }
    if message == WM_NCDESTROY {
        unsafe { let _ = RemoveWindowSubclass(hwnd, Some(hud_frame_style_guard), subclass_id); }
    }
    unsafe { DefSubclassProc(hwnd, message, wparam, lparam) }
}

#[cfg(target_os = "windows")]
fn install_hud_frame_style_guard(window: &tauri::WebviewWindow) -> Result<(), Box<dyn std::error::Error>> {
    use windows::Win32::UI::Shell::SetWindowSubclass;
    use windows::Win32::UI::WindowsAndMessaging::{
        GetWindowLongPtrW, SetWindowLongPtrW, GWL_EXSTYLE, GWL_STYLE,
    };
    let hwnd = window.hwnd()?;
    // Install when the shaped presentation first takes ownership, after
    // WebView creation has finished installing its native subclasses.
    unsafe {
        SetWindowSubclass(hwnd, Some(hud_frame_style_guard), 1, 0).ok()?;
        let (style, ex_style) = hud_borderless_styles(
            GetWindowLongPtrW(hwnd, GWL_STYLE),
            GetWindowLongPtrW(hwnd, GWL_EXSTYLE),
        );
        SetWindowLongPtrW(hwnd, GWL_STYLE, style);
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style);
    }
    eprintln!("[HUD] frame style guard installed");
    Ok(())
}

#[cfg(test)]
mod tests {
    #[cfg(target_os = "windows")]
    #[test]
    fn hud_guard_preserves_activation_dispatch_without_nonclient_repaint() {
        use windows::core::w;
        use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
        use windows::Win32::UI::Shell::{DefSubclassProc, SetWindowSubclass};
        use windows::Win32::UI::WindowsAndMessaging::*;

        unsafe extern "system" fn observe(
            hwnd: HWND, message: u32, wparam: WPARAM, lparam: LPARAM,
            _id: usize, reference: usize,
        ) -> LRESULT {
            if message == WM_NCACTIVATE || message == WM_APP {
                // Synchronous SendMessage dispatch; storage outlives this HWND.
                unsafe { &mut *(reference as *mut Vec<(u32, usize, isize)>) }
                    .push((message, wparam.0, lparam.0));
            }
            unsafe { DefSubclassProc(hwnd, message, wparam, lparam) }
        }

        let mut received: Vec<(u32, usize, isize)> = Vec::new();
        unsafe {
            let hwnd = CreateWindowExW(
                WINDOW_EX_STYLE::default(), w!("STATIC"), w!(""),
                WS_POPUP, 0, 0, 160, 80, None, None, None, None,
            ).unwrap();
            struct OwnedWindow(HWND);
            impl Drop for OwnedWindow {
                fn drop(&mut self) { unsafe { let _ = DestroyWindow(self.0); } }
            }
            let _owned = OwnedWindow(hwnd);
            SetWindowSubclass(hwnd, Some(observe), 2, &mut received as *mut _ as usize)
                .ok().unwrap();
            // Control: an unguarded nonclient activation requests normal paint.
            SendMessageW(hwnd, WM_NCACTIVATE, Some(WPARAM(0)), Some(LPARAM(0)));
            assert_eq!(received.pop(), Some((WM_NCACTIVATE, 0, 0)));

            SetWindowSubclass(hwnd, Some(super::hud_frame_style_guard), 1, 0)
                .ok().unwrap();
            for active in [0, 1, 0] {
                let result = SendMessageW(
                    hwnd, WM_NCACTIVATE, Some(WPARAM(active)), Some(LPARAM(0)),
                );
                assert_eq!(received.pop(), Some((WM_NCACTIVATE, active, -1)));
                if active == 0 {
                    assert_ne!(result.0, 0, "deactivation must not be vetoed");
                }
            }
            SendMessageW(hwnd, WM_APP, Some(WPARAM(7)), Some(LPARAM(42)));
            assert_eq!(received.pop(), Some((WM_APP, 7, 42)));
        }
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn hud_style_guard_rejects_native_frame_before_style_installation() {
        use windows::core::w;
        use windows::Win32::UI::Shell::SetWindowSubclass;
        use windows::Win32::UI::WindowsAndMessaging::*;
        unsafe {
            let hwnd = CreateWindowExW(
                WINDOW_EX_STYLE::default(), w!("STATIC"), w!(""),
                WS_POPUP, 0, 0, 160, 80, None, None, None, None,
            ).unwrap();
            struct OwnedWindow(windows::Win32::Foundation::HWND);
            impl Drop for OwnedWindow {
                fn drop(&mut self) { unsafe { let _ = DestroyWindow(self.0); } }
            }
            let _owned = OwnedWindow(hwnd);
            // Control: without the guard Windows accepts the captured frame.
            SetWindowLongPtrW(hwnd, GWL_STYLE, 0x04CB0000);
            assert_ne!(GetWindowLongPtrW(hwnd, GWL_STYLE) & WS_CAPTION.0 as isize, 0);
            SetWindowSubclass(hwnd, Some(super::hud_frame_style_guard), 1, 0).ok().unwrap();
            // These are Tao's captured hidden-HUD style words. Exercise the
            // real synchronous WM_STYLECHANGING callback repeatedly, as in
            // show/hide and Agent Speak handoffs, without presenting test UI.
            for _ in 0..3 {
                SetWindowLongPtrW(hwnd, GWL_STYLE, 0x04CB0000);
                SetWindowLongPtrW(hwnd, GWL_EXSTYLE, 0x08040118);
                assert_eq!(GetWindowLongPtrW(hwnd, GWL_STYLE), 0x04000000);
                // WS_EX_TOPMOST is controlled by SetWindowPos, not SetWindowLong.
                assert_eq!(GetWindowLongPtrW(hwnd, GWL_EXSTYLE), 0x08000010);
            }
        }
    }
    #[cfg(target_os = "windows")]
    #[test]
    fn hud_ex_style_keeps_activation_off_only_while_focusable() {
        use windows::Win32::UI::WindowsAndMessaging::WS_EX_NOACTIVATE;
        let proposed = 0x08000018_isize;
        assert_eq!(
            super::hud_ex_style_for_focus(proposed, true) & WS_EX_NOACTIVATE.0 as isize,
            0,
            "a style rewrite must not restore WS_EX_NOACTIVATE while the HUD takes focus",
        );
        assert_eq!(super::hud_ex_style_for_focus(proposed, false), proposed);
        assert_eq!(super::hud_ex_style_for_focus(0x18, true), 0x18);
    }

    use super::MAIN_WINDOW_ICON_SLOTS;
    use windows::Win32::UI::WindowsAndMessaging::{ICON_BIG, ICON_SMALL};

    #[test]
    fn main_window_icon_targets_small_and_large_native_slots() {
        assert_eq!(MAIN_WINDOW_ICON_SLOTS, [ICON_SMALL, ICON_BIG]);
    }
}
