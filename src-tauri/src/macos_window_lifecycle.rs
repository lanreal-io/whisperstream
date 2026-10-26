//! Darwin owner for AppKit/Tauri windows, menu-bar/Dock policy, displays, and
//! the main application's SMAppService login item.
//!
//! Native windows, monitor objects, login-item service objects, and status
//! details never leave this adapter. Commands expose only schema-v1 roles,
//! actions, and typed content-free status values.

use objc2::rc::Retained;
use objc2::runtime::{AnyClass, AnyObject, Bool, ClassBuilder, NSObject};
use objc2::{define_class, ffi, msg_send, AnyThread, ClassType, MainThreadMarker, MainThreadOnly};
use objc2_app_kit::{
    NSApplicationActivationOptions, NSPanel, NSRunningApplication, NSToolbar, NSTrackingArea,
    NSTrackingAreaOptions, NSView, NSWindow, NSWindowButton, NSWindowCollectionBehavior,
    NSWindowStyleMask, NSWindowTitleVisibility, NSWindowToolbarStyle, NSWorkspace,
    NSWorkspaceDidWakeNotification, NSWorkspaceScreensDidWakeNotification,
};
use objc2_foundation::{NSActivityOptions, NSProcessInfo, NSString};
use objc2_service_management::{SMAppService, SMAppServiceStatus};
use std::ffi::CStr;
use std::sync::{Mutex, OnceLock};
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{Emitter, Manager};

use crate::window_lifecycle_contract::{
    LaunchAtLoginCommandResult, LaunchAtLoginResult, LaunchAtLoginState, LaunchPresentation,
    LogicalGeometry, WindowAction, WindowFailure, WindowLifecycle, WindowLifecyclePort,
    WindowLifecycleRequest, WindowRole,
};

fn window_label(role: WindowRole) -> &'static str {
    match role {
        WindowRole::Main => "main",
        WindowRole::Hud => "hud",
        WindowRole::Snap => "snap-guide",
        WindowRole::Review => "popup-review",
        WindowRole::Update => "popup-update",
        WindowRole::Eula => "popup-eula",
        WindowRole::Error => "popup-error",
    }
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

static AGENT_HUD_PREVIOUS_APP: OnceLock<Mutex<Option<libc::pid_t>>> = OnceLock::new();

fn agent_hud_previous_app() -> &'static Mutex<Option<libc::pid_t>> {
    AGENT_HUD_PREVIOUS_APP.get_or_init(|| Mutex::new(None))
}

fn take_agent_hud_restore_target(
    previous_app: &mut Option<libc::pid_t>,
    hud_focused: Option<bool>,
) -> Option<libc::pid_t> {
    // Closing a focus lease always retires it. A user-selected destination or
    // unavailable focus observation must never reactivate its former owner.
    previous_app.take().filter(|_| hud_focused == Some(true))
}

/// Give the interactive Agent HUD exact native focus and return focus when its
/// question/permission generation closes. Only a PID is retained; no app name,
/// path, title, or native object crosses this adapter or survives release.
pub(crate) fn set_agent_hud_focusable(
    window: tauri::WebviewWindow,
    focusable: bool,
) -> Result<(), String> {
    if focusable {
        let workspace = NSWorkspace::sharedWorkspace();
        let previous_pid = workspace
            .frontmostApplication()
            .map(|application| application.processIdentifier())
            .filter(|pid| *pid > 0 && *pid != std::process::id() as libc::pid_t);
        *agent_hud_previous_app()
            .lock()
            .map_err(|_| "agent HUD focus owner unavailable".to_string())? = previous_pid;
        window
            .set_focusable(true)
            .map_err(|error| error.to_string())?;
        if let Err(error) = window.set_focus() {
            let _ = window.set_focusable(false);
            *agent_hud_previous_app()
                .lock()
                .map_err(|_| "agent HUD focus owner unavailable".to_string())? = None;
            return Err(error.to_string());
        }
        return Ok(());
    }

    // Observe exact window ownership before changing focusability. The user
    // may already have switched to another app or another Whisperstream window.
    let hud_focused = window.is_focused().ok();
    let previous_pid = take_agent_hud_restore_target(
        &mut *agent_hud_previous_app()
            .lock()
            .map_err(|_| "agent HUD focus owner unavailable".to_string())?,
        hud_focused,
    );
    window
        .set_focusable(false)
        .map_err(|error| error.to_string())?;
    if let Some(pid) = previous_pid {
        if let Some(application) =
            NSRunningApplication::runningApplicationWithProcessIdentifier(pid)
        {
            if !application.isTerminated() {
                application.activateWithOptions(NSApplicationActivationOptions::empty());
            }
        }
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq)]
struct DisplayBounds {
    token: String,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
}

fn bounded_geometry(
    requested: &LogicalGeometry,
    display: &DisplayBounds,
) -> Result<LogicalGeometry, WindowFailure> {
    requested.validate()?;
    if requested.display_token != display.token
        || !display.x.is_finite()
        || !display.y.is_finite()
        || !display.width.is_finite()
        || !display.height.is_finite()
        || display.width <= 0.0
        || display.height <= 0.0
    {
        return Err(WindowFailure::DisplayUnavailable);
    }
    let width = requested.width.min(display.width);
    let height = requested.height.min(display.height);
    let max_x = display.x + display.width - width;
    let max_y = display.y + display.height - height;
    Ok(LogicalGeometry {
        x: requested.x.clamp(display.x, max_x),
        y: requested.y.clamp(display.y, max_y),
        width,
        height,
        display_token: display.token.clone(),
    })
}

trait MacosNativeBridge {
    fn mutate_window(
        &mut self,
        role: WindowRole,
        action: WindowAction,
        geometry: Option<&LogicalGeometry>,
    ) -> Result<(bool, bool), WindowFailure>;
    fn set_foreground_presentation(&mut self, foreground: bool) -> Result<(), WindowFailure>;
    fn has_visible_foreground_window(&self) -> bool;
    fn quit(&mut self) -> Result<(), WindowFailure>;
    fn launch_at_login_state(&self) -> LaunchAtLoginResult;
    fn register_launch_at_login(&mut self) -> LaunchAtLoginResult;
    fn unregister_launch_at_login(&mut self) -> LaunchAtLoginResult;
}

struct MacosWindowPort<B> {
    bridge: B,
}

impl<B: MacosNativeBridge> WindowLifecyclePort for MacosWindowPort<B> {
    fn mutate(
        &mut self,
        role: WindowRole,
        action: WindowAction,
        geometry: Option<&LogicalGeometry>,
    ) -> Result<(bool, bool), WindowFailure> {
        if action == WindowAction::Quit {
            self.bridge.quit()?;
            return Ok((false, false));
        }

        let foreground_action = matches!(
            action,
            WindowAction::Show | WindowAction::Focus | WindowAction::Restore
        ) && !matches!(role, WindowRole::Hud | WindowRole::Snap);
        if foreground_action {
            self.bridge.set_foreground_presentation(true)?;
        }
        let result = self.bridge.mutate_window(role, action, geometry)?;
        if matches!(action, WindowAction::Hide | WindowAction::CloseRequest)
            && !self.bridge.has_visible_foreground_window()
        {
            self.bridge.set_foreground_presentation(false)?;
        }
        Ok(result)
    }

    fn set_launch_at_login(&mut self, enabled: bool) -> LaunchAtLoginResult {
        if enabled {
            self.bridge.register_launch_at_login()
        } else {
            self.bridge.unregister_launch_at_login()
        }
    }

    fn launch_at_login_state(&self) -> LaunchAtLoginResult {
        self.bridge.launch_at_login_state()
    }
}

#[derive(Clone)]
struct TauriMacosBridge {
    app: tauri::AppHandle,
}

impl TauriMacosBridge {
    fn window(&self, role: WindowRole) -> Result<tauri::WebviewWindow, WindowFailure> {
        self.app
            .get_webview_window(window_label(role))
            .ok_or(WindowFailure::NotFound)
    }

    fn display_bounds(
        &self,
        window: &tauri::WebviewWindow,
        display_token: &str,
    ) -> Result<DisplayBounds, WindowFailure> {
        // Logical display tokens are adapter-owned. The shared/frontend layers
        // never receive monitor names, IDs, coordinates, or native objects.
        let monitors = window
            .available_monitors()
            .map_err(|_| WindowFailure::DisplayUnavailable)?;
        let (index, monitor) = monitors
            .into_iter()
            .enumerate()
            .find(|(index, _)| display_token == format!("display-{index}"))
            .ok_or(WindowFailure::DisplayUnavailable)?;
        let scale = monitor.scale_factor();
        let position: tauri::LogicalPosition<f64> = monitor.position().to_logical(scale);
        let size: tauri::LogicalSize<f64> = monitor.size().to_logical(scale);
        Ok(DisplayBounds {
            token: format!("display-{index}"),
            x: position.x,
            y: position.y,
            width: size.width,
            height: size.height,
        })
    }
}

impl MacosNativeBridge for TauriMacosBridge {
    fn mutate_window(
        &mut self,
        role: WindowRole,
        action: WindowAction,
        geometry: Option<&LogicalGeometry>,
    ) -> Result<(bool, bool), WindowFailure> {
        let window = self.window(role)?;
        if let Some(requested) = geometry {
            let display = self.display_bounds(&window, &requested.display_token)?;
            let bounded = bounded_geometry(requested, &display)?;
            window
                .set_position(tauri::LogicalPosition::new(bounded.x, bounded.y))
                .map_err(|_| WindowFailure::DisplayUnavailable)?;
            window
                .set_size(tauri::LogicalSize::new(bounded.width, bounded.height))
                .map_err(|_| WindowFailure::DisplayUnavailable)?;
        }
        match action {
            WindowAction::Show => window.show().map_err(|_| WindowFailure::NativeFailure)?,
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
        Ok((
            window.is_visible().unwrap_or(false),
            window.is_focused().unwrap_or(false),
        ))
    }

    fn set_foreground_presentation(&mut self, foreground: bool) -> Result<(), WindowFailure> {
        self.app
            .set_activation_policy(if foreground {
                tauri::ActivationPolicy::Regular
            } else {
                tauri::ActivationPolicy::Accessory
            })
            .map_err(|_| WindowFailure::NativeFailure)?;
        self.app
            .set_dock_visibility(foreground)
            .map_err(|_| WindowFailure::NativeFailure)
    }

    fn has_visible_foreground_window(&self) -> bool {
        [
            WindowRole::Main,
            WindowRole::Review,
            WindowRole::Update,
            WindowRole::Eula,
            WindowRole::Error,
        ]
        .into_iter()
        .filter_map(|role| self.app.get_webview_window(window_label(role)))
        .any(|window| window.is_visible().unwrap_or(false))
    }

    fn quit(&mut self) -> Result<(), WindowFailure> {
        #[cfg(debug_assertions)]
        crate::signal_dev_backend_shutdown();
        self.app.exit(0);
        Ok(())
    }

    fn launch_at_login_state(&self) -> LaunchAtLoginResult {
        SmAppServiceBridge::status()
    }

    fn register_launch_at_login(&mut self) -> LaunchAtLoginResult {
        SmAppServiceBridge::register()
    }

    fn unregister_launch_at_login(&mut self) -> LaunchAtLoginResult {
        SmAppServiceBridge::unregister()
    }
}

struct SmAppServiceBridge;

impl SmAppServiceBridge {
    fn service() -> objc2::rc::Retained<SMAppService> {
        // SAFETY: mainAppService is a process-global Objective-C factory with
        // no caller-provided pointer. The retained object stays inside this
        // native adapter and is dropped before returning typed state.
        unsafe { SMAppService::mainAppService() }
    }

    fn status() -> LaunchAtLoginResult {
        let service = Self::service();
        // SAFETY: the retained SMAppService is valid for this synchronous,
        // passive query. No prompt, registration, or settings mutation occurs.
        Self::map_status(unsafe { service.status() })
    }

    fn register() -> LaunchAtLoginResult {
        let service = Self::service();
        // SAFETY: this is called only from the explicit user mutation command.
        let outcome = unsafe { service.registerAndReturnError() };
        let status = Self::map_status(unsafe { service.status() });
        if outcome.is_ok() || status.state == LaunchAtLoginState::RequiresApproval {
            status
        } else {
            LaunchAtLoginResult {
                state: LaunchAtLoginState::Unavailable,
                failure: Some(WindowFailure::NativeFailure),
            }
        }
    }

    fn unregister() -> LaunchAtLoginResult {
        let service = Self::service();
        // SAFETY: this is called only from the explicit user mutation command.
        let outcome = unsafe { service.unregisterAndReturnError() };
        let status = Self::map_status(unsafe { service.status() });
        if outcome.is_ok() || status.state == LaunchAtLoginState::Disabled {
            status
        } else {
            LaunchAtLoginResult {
                state: LaunchAtLoginState::Unavailable,
                failure: Some(WindowFailure::NativeFailure),
            }
        }
    }

    fn map_status(status: SMAppServiceStatus) -> LaunchAtLoginResult {
        if status == SMAppServiceStatus::NotRegistered {
            LaunchAtLoginResult {
                state: LaunchAtLoginState::Disabled,
                failure: None,
            }
        } else if status == SMAppServiceStatus::Enabled {
            LaunchAtLoginResult {
                state: LaunchAtLoginState::Enabled,
                failure: None,
            }
        } else if status == SMAppServiceStatus::RequiresApproval {
            LaunchAtLoginResult {
                state: LaunchAtLoginState::RequiresApproval,
                failure: None,
            }
        } else if status == SMAppServiceStatus::NotFound {
            LaunchAtLoginResult {
                state: LaunchAtLoginState::NotFound,
                failure: Some(WindowFailure::NotFound),
            }
        } else {
            LaunchAtLoginResult {
                state: LaunchAtLoginState::Unavailable,
                failure: Some(WindowFailure::NativeFailure),
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg(feature = "macos-phase4-probe")]
pub(crate) struct PackagedLoginItemProbe {
    pub(crate) initial: LaunchAtLoginState,
    pub(crate) registered: LaunchAtLoginState,
    pub(crate) requeried: LaunchAtLoginState,
    pub(crate) final_state: LaunchAtLoginState,
    pub(crate) stale_internal_registration_cleaned: bool,
}

#[cfg(feature = "macos-phase4-probe")]
fn cleanup_packaged_login_item_registration() -> bool {
    // This helper is reached only after this exact fresh internal bundle saw
    // Disabled and attempted registration. It must never clean an ambiguous
    // registration discovered at preflight. Re-query after each bounded
    // unregister attempt so every error path leaves the test identity absent.
    for _ in 0..3 {
        let unregistered = SmAppServiceBridge::unregister();
        let final_state = SmAppServiceBridge::status().state;
        if unregistered.state == LaunchAtLoginState::Disabled
            && final_state == LaunchAtLoginState::Disabled
        {
            return true;
        }
    }
    false
}

#[cfg(any(feature = "macos-phase4-probe", test))]
fn clean_packaged_login_item_preflight(state: LaunchAtLoginState) -> bool {
    // A fresh main-app identity can be absent from Background Task Management
    // until its first real registration. macOS reports that clean no-record
    // state as NotFound rather than NotRegistered. Both states are safe to
    // proceed from; any registered, approval, or unavailable state remains
    // ambiguous and must fail before mutation.
    matches!(
        state,
        LaunchAtLoginState::Disabled | LaunchAtLoginState::NotFound
    )
}

#[cfg(feature = "macos-phase4-probe")]
pub(crate) fn run_packaged_login_item_probe() -> Result<PackagedLoginItemProbe, String> {
    let initial = SmAppServiceBridge::status().state;
    if !clean_packaged_login_item_preflight(initial) {
        return Err("internal login-item preflight found ambiguous existing state".to_string());
    }

    let registered = SmAppServiceBridge::register().state;
    if !matches!(
        registered,
        LaunchAtLoginState::Enabled | LaunchAtLoginState::RequiresApproval
    ) {
        if !cleanup_packaged_login_item_registration() {
            return Err("internal login-item registration cleanup failed".to_string());
        }
        return Err("internal login-item registration failed".to_string());
    }
    let requeried = SmAppServiceBridge::status().state;
    if requeried != registered {
        if !cleanup_packaged_login_item_registration() {
            return Err("internal login-item re-query cleanup failed".to_string());
        }
        return Err("internal login-item status re-query changed".to_string());
    }
    if !cleanup_packaged_login_item_registration() {
        return Err("internal login-item unregister cleanup failed".to_string());
    }
    let final_state = SmAppServiceBridge::status().state;
    Ok(PackagedLoginItemProbe {
        initial,
        registered,
        requeried,
        final_state,
        stale_internal_registration_cleaned: false,
    })
}

fn lifecycle(app: &tauri::AppHandle) -> WindowLifecycle<MacosWindowPort<TauriMacosBridge>> {
    WindowLifecycle::new(MacosWindowPort {
        bridge: TauriMacosBridge { app: app.clone() },
    })
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
            "[tauri] macOS window lifecycle failed role={role:?} action={action:?} failure={failure:?}"
        );
    }
}

pub(crate) fn show_main_window(app: &tauri::AppHandle) {
    request(app, WindowRole::Main, WindowAction::Show);
    request(app, WindowRole::Main, WindowAction::Restore);
    request(app, WindowRole::Main, WindowAction::Focus);
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

fn overlay_collection_behavior(
    mut behavior: NSWindowCollectionBehavior,
) -> NSWindowCollectionBehavior {
    behavior.remove(NSWindowCollectionBehavior::MoveToActiveSpace);
    behavior.remove(NSWindowCollectionBehavior::FullScreenPrimary);
    behavior.remove(NSWindowCollectionBehavior::FullScreenNone);
    // These macOS 13+ membership roles are mutually exclusive. Only overlays
    // may join another application's fullscreen/Stage Manager collection.
    behavior.remove(NSWindowCollectionBehavior::Primary);
    behavior.remove(NSWindowCollectionBehavior::Auxiliary);
    behavior.insert(NSWindowCollectionBehavior::CanJoinAllApplications);
    behavior.insert(NSWindowCollectionBehavior::CanJoinAllSpaces);
    behavior.insert(NSWindowCollectionBehavior::FullScreenAuxiliary);
    behavior
}

const OVERLAY_PANEL_CLASS: &CStr = c"WhisperstreamOverlayPanel";
const TAO_FOCUSABLE_IVAR: &CStr = c"focusable";

/// Content-free reason an overlay stayed an ordinary window.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum OverlayPanelRefusal {
    UnexpectedWindowClass,
    LayoutMismatch,
    ClassUnavailable,
    MethodCopyFailed,
}

/// Build an NSPanel subclass that can stand in for tao's NSWindow subclass.
///
/// AppKit shows another app's fullscreen Space only to non-activating panels
/// while this app uses the Regular activation policy (main window open). tao
/// creates every Tauri window as `TaoWindow`, an NSWindow subclass with one
/// Bool `focusable` ivar and overrides such as `canBecomeKeyWindow`. The panel
/// class redeclares that ivar at the identical offset and copies tao's own
/// method implementations, so tao's focusability, key and event handling keep
/// working after the swap. Any layout difference refuses the swap.
fn build_overlay_panel_class(
    source: &AnyClass,
    name: &CStr,
) -> Result<&'static AnyClass, OverlayPanelRefusal> {
    let window = NSWindow::class();
    let panel = NSPanel::class();
    if source.superclass() != Some(window) {
        return Err(OverlayPanelRefusal::UnexpectedWindowClass);
    }
    let source_ivars = source.instance_variables();
    let [source_ivar] = &source_ivars[..] else {
        return Err(OverlayPanelRefusal::LayoutMismatch);
    };
    if source_ivar.name() != TAO_FOCUSABLE_IVAR
        || panel.instance_size() != window.instance_size()
        || !panel.instance_variables().is_empty()
    {
        return Err(OverlayPanelRefusal::LayoutMismatch);
    }

    let mut builder =
        ClassBuilder::new(name, panel).ok_or(OverlayPanelRefusal::ClassUnavailable)?;
    builder.add_ivar::<Bool>(TAO_FOCUSABLE_IVAR);
    let class = builder.register();
    let ivar = class
        .instance_variable(TAO_FOCUSABLE_IVAR)
        .ok_or(OverlayPanelRefusal::LayoutMismatch)?;
    if ivar.offset() != source_ivar.offset()
        || ivar.type_encoding() != source_ivar.type_encoding()
        || class.instance_size() != source.instance_size()
    {
        return Err(OverlayPanelRefusal::LayoutMismatch);
    }
    for method in source.instance_methods().iter() {
        // SAFETY: both classes share the receiver layout verified above; the
        // copied IMP keeps its original selector and type encoding. tao's
        // overrides resolve `super` through the receiver's runtime superclass.
        let added = unsafe {
            ffi::class_addMethod(
                class as *const AnyClass as *mut AnyClass,
                method.name(),
                method.implementation(),
                ffi::method_getTypeEncoding(*method),
            )
        };
        if !added.as_bool() {
            return Err(OverlayPanelRefusal::MethodCopyFailed);
        }
    }
    Ok(class)
}

/// One process-wide panel class, built from the first overlay's tao class.
fn overlay_panel_class(source: &'static AnyClass) -> Result<&'static AnyClass, OverlayPanelRefusal> {
    static PANEL: OnceLock<(&'static AnyClass, Result<&'static AnyClass, OverlayPanelRefusal>)> =
        OnceLock::new();
    let (built_from, result) =
        PANEL.get_or_init(|| (source, build_overlay_panel_class(source, OVERLAY_PANEL_CLASS)));
    if *built_from != source {
        return Err(OverlayPanelRefusal::UnexpectedWindowClass);
    }
    *result
}

/// Keys whose KVO notifications a Foundation `NSKVONotifying_` class carries,
/// derived from its overridden one-argument setters. Any other override shape
/// is unknown, so the overlay is not converted.
fn kvo_observed_keys<'a>(methods: impl IntoIterator<Item = &'a str>) -> Option<Vec<String>> {
    let mut keys = Vec::new();
    for method in methods {
        if matches!(method, "class" | "dealloc" | "_isKVOA") {
            continue;
        }
        let property = method.strip_prefix("set")?.strip_suffix(':')?;
        let mut chars = property.chars();
        let first = chars.next().filter(char::is_ascii_uppercase)?;
        if property.contains(':') {
            return None;
        }
        keys.push(first.to_ascii_lowercase().to_string() + chars.as_str());
    }
    Some(keys)
}

/// Turn one hidden overlay into a non-activating panel in place. Level,
/// focusability, mouse handling, geometry and the WebView are unchanged.
fn convert_overlay_to_nonactivating_panel(native: &NSWindow) -> Result<(), OverlayPanelRefusal> {
    let object: &AnyObject = native;
    let runtime = object.class();
    // KVO hides its generated subclass from `-class`. WebKit observes the
    // overlay windows, so the runtime class is normally NSKVONotifying_TaoWindow.
    // SAFETY: `-class` takes no arguments and returns the receiver's class.
    let logical: &'static AnyClass = unsafe { objc2::msg_send![object, class] };
    let observed_keys = if runtime == logical {
        Vec::new()
    } else {
        if runtime.superclass() != Some(logical)
            || runtime.instance_size() != logical.instance_size()
            || !runtime.instance_variables().is_empty()
        {
            return Err(OverlayPanelRefusal::UnexpectedWindowClass);
        }
        let methods = runtime.instance_methods();
        let names: Vec<String> = methods
            .iter()
            .map(|method| method.name().name().to_string_lossy().into_owned())
            .collect();
        kvo_observed_keys(names.iter().map(String::as_str))
            .ok_or(OverlayPanelRefusal::UnexpectedWindowClass)?
    };
    let panel = overlay_panel_class(logical)?;
    // SAFETY: called on the main thread during setup, before the overlay is
    // shown. The panel class has the receiver's exact instance layout.
    unsafe { AnyObject::set_class(object, panel) };
    native.setStyleMask(native.styleMask() | NSWindowStyleMask::NonactivatingPanel);
    // NSPanel hides on deactivation by default; overlays must stay presented.
    native.setHidesOnDeactivate(false);
    rearm_kvo(object, panel, &observed_keys);
    Ok(())
}

/// Restore KVO notifications after the class swap. Adding an observer makes
/// Foundation generate the panel's notifying subclass and override each
/// observed setter; existing observers (WebKit's) then receive notifications
/// again. The transient observer is removed before any change can occur.
fn rearm_kvo(object: &AnyObject, panel: &AnyClass, keys: &[String]) {
    if keys.is_empty() {
        return;
    }
    let observer = objc2::runtime::NSObject::new();
    for key in keys {
        let key = objc2_foundation::NSString::from_str(key);
        // SAFETY: main-thread KVO registration with a live observer that is
        // removed synchronously; no value changes between the two calls.
        unsafe {
            let _: () = objc2::msg_send![
                object,
                addObserver: &*observer,
                forKeyPath: &*key,
                options: 0usize,
                context: std::ptr::null_mut::<std::ffi::c_void>()
            ];
            let _: () = objc2::msg_send![object, removeObserver: &*observer, forKeyPath: &*key];
        }
    }
    let rearmed = object.class();
    if rearmed.superclass() != Some(panel) {
        eprintln!("[tauri] macOS overlay panel KVO re-arm unverified");
    }
}

/// Apply the approved Spaces and panel policy only to the three dictation
/// overlays. Neither changes activation, focusability or window level.
fn install_overlay_spaces_policy(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    for label in ["hud", "recording-control", "snap-guide"] {
        if let Some(window) = app.get_webview_window(label) {
            let pointer = window.ns_window()?;
            // setup runs on AppKit's main thread; Tauri owns the live NSWindow.
            let native = unsafe { &*(pointer as *const NSWindow) };
            if let Err(refusal) = convert_overlay_to_nonactivating_panel(native) {
                // Fail safe: keep the ordinary window, which still appears
                // over fullscreen apps while the app is an Accessory.
                eprintln!("[tauri] macOS overlay panel unavailable window={label} reason={refusal:?}");
            }
            native.setCollectionBehavior(overlay_collection_behavior(native.collectionBehavior()));
        }
    }
    Ok(())
}

/// Give the shared undecorated main window native macOS chrome: traffic
/// lights, rounded corners and the system shadow. The title bar is a
/// transparent overlay over full-size content, so the web layout is unchanged
/// and its own drag regions still move the window. Windows keeps its custom
/// controls.
fn install_main_window_chrome(app: &tauri::AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let window = app
        .get_webview_window(window_label(WindowRole::Main))
        .ok_or("missing main window")?;
    let pointer = window.ns_window()?;
    // setup runs on AppKit's main thread, before the main window is first
    // shown.
    let native = unsafe { &*(pointer as *const NSWindow) };
    native.setStyleMask(
        native.styleMask()
            | NSWindowStyleMask::Titled
            | NSWindowStyleMask::Closable
            | NSWindowStyleMask::Miniaturizable
            | NSWindowStyleMask::FullSizeContentView,
    );
    native.setTitlebarAppearsTransparent(true);
    native.setTitleVisibility(NSWindowTitleVisibility::Hidden);
    // An empty unified toolbar gives the title bar its standard height, so
    // AppKit lays the traffic lights out where it tracks their hover. Moving
    // them by hand leaves the hover area at their original position.
    let toolbar = NSToolbar::new(MainThreadMarker::new().ok_or("not on the main thread")?);
    native.setToolbar(Some(&toolbar));
    native.setToolbarStyle(NSWindowToolbarStyle::Unified);
    // The window is fixed-size, so zoom would only show as a blank disabled
    // button.
    if let Some(zoom) = native.standardWindowButton(NSWindowButton::ZoomButton) {
        zoom.setHidden(true);
    }
    // A style-mask change can drop the first responder; keep key handling.
    let content = native.contentView();
    native.makeFirstResponder(content.as_deref().map(AsRef::as_ref));
    Ok(())
}

/// Native hover signal for the recording control's webview.
pub(crate) const RECORDING_CONTROL_HOVER_EVENT: &str = "recording-control-hover";

static RECORDING_CONTROL_HOVER_APP: OnceLock<tauri::AppHandle> = OnceLock::new();

fn emit_recording_control_hover(entered: bool) {
    let Some(app) = RECORDING_CONTROL_HOVER_APP.get() else {
        return;
    };
    eprintln!("[HUD] dedicated control native hover entered={entered}");
    if let Err(error) = app.emit_to("recording-control", RECORDING_CONTROL_HOVER_EVENT, entered) {
        eprintln!("[HUD] dedicated control native hover emit failed reason={error}");
    }
}

define_class!(
    /// Tracking-area owner that reports pointer enter/exit for the control.
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "WhisperstreamRecordingControlHover"]
    struct RecordingControlHover;

    impl RecordingControlHover {
        #[unsafe(method(mouseEntered:))]
        fn mouse_entered(&self, _event: &AnyObject) {
            emit_recording_control_hover(true);
        }

        #[unsafe(method(mouseExited:))]
        fn mouse_exited(&self, _event: &AnyObject) {
            emit_recording_control_hover(false);
        }
    }
);

/// WebKit delivers DOM hover only while its window is key, and the recording
/// control is a non-focusable panel that never becomes key: hover never
/// expanded it, and a click's enter was never matched by a leave. AppKit still
/// reports an always-active tracking area in any window, so a native owner
/// forwards enter/exit to the control's own hover handlers as an event.
fn install_recording_control_hover_tracking(app: &tauri::AppHandle) -> tauri::Result<()> {
    let Some(window) = app.get_webview_window("recording-control") else {
        return Ok(());
    };
    let _ = RECORDING_CONTROL_HOVER_APP.set(app.clone());
    window.with_webview(|webview| {
        let Some(mtm) = MainThreadMarker::new() else {
            eprintln!("[HUD] dedicated control native hover unavailable reason=off_main_thread");
            return;
        };
        let owner: Retained<RecordingControlHover> =
            unsafe { msg_send![RecordingControlHover::alloc(mtm), init] };
        // SAFETY: with_webview runs on AppKit's main thread; inner() is the
        // live WKWebView, an NSView that retains the added tracking area.
        unsafe {
            let view = &*(webview.inner() as *const NSView);
            let area = NSTrackingArea::initWithRect_options_owner_userInfo(
                NSTrackingArea::alloc(),
                objc2_foundation::NSRect::ZERO,
                NSTrackingAreaOptions::MouseEnteredAndExited
                    | NSTrackingAreaOptions::ActiveAlways
                    | NSTrackingAreaOptions::InVisibleRect,
                Some(&owner),
                None,
            );
            view.addTrackingArea(&area);
        }
        // Tracking areas do not retain their owner; the control lives for the
        // whole process, so its owner does too.
        std::mem::forget(owner);
        eprintln!("[HUD] dedicated control native hover installed");
    })
}

/// Native wake signal for every webview's backend socket.
pub(crate) const SYSTEM_WAKE_EVENT: &str = "system-wake";

static SYSTEM_WAKE_APP: OnceLock<tauri::AppHandle> = OnceLock::new();

fn emit_system_wake(source: &str) {
    let Some(app) = SYSTEM_WAKE_APP.get() else {
        return;
    };
    eprintln!("[tauri] system wake source={source}");
    if let Err(error) = app.emit(SYSTEM_WAKE_EVENT, ()) {
        eprintln!("[tauri] system wake emit failed reason={error}");
    }
}

define_class!(
    /// NSWorkspace observer that reports system and display wake.
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[name = "WhisperstreamSystemWakeObserver"]
    struct SystemWakeObserver;

    impl SystemWakeObserver {
        #[unsafe(method(systemDidWake:))]
        fn system_did_wake(&self, _notification: &AnyObject) {
            emit_system_wake("system");
        }

        #[unsafe(method(screensDidWake:))]
        fn screens_did_wake(&self, _notification: &AnyObject) {
            emit_system_wake("screens");
        }
    }
);

/// A webview socket dropped across display sleep was not retried until the
/// app was next activated: its backoff timer stayed suspended while the app
/// had no visible window. Wake is reported natively so every webview
/// reconnects at once, including the hidden HUD.
fn install_system_wake_observer(app: &tauri::AppHandle) {
    let Some(mtm) = MainThreadMarker::new() else {
        eprintln!("[tauri] system wake observer unavailable reason=off_main_thread");
        return;
    };
    let _ = SYSTEM_WAKE_APP.set(app.clone());
    let observer: Retained<SystemWakeObserver> =
        unsafe { msg_send![SystemWakeObserver::alloc(mtm), init] };
    let center = NSWorkspace::sharedWorkspace().notificationCenter();
    // SAFETY: the observer implements both selectors with the notification
    // argument, and the AppKit names are immutable framework statics.
    unsafe {
        center.addObserver_selector_name_object(
            &observer,
            objc2::sel!(systemDidWake:),
            Some(NSWorkspaceDidWakeNotification),
            None,
        );
        center.addObserver_selector_name_object(
            &observer,
            objc2::sel!(screensDidWake:),
            Some(NSWorkspaceScreensDidWakeNotification),
            None,
        );
    }
    // The center does not retain observers; this one lives for the process.
    std::mem::forget(observer);
}

/// Keep App Nap from coalescing the shell's and webviews' timers while the
/// app sits in the background. A push-to-talk app must present its HUD
/// promptly after long idle; idle system sleep remains allowed.
fn hold_app_nap_exemption() {
    let activity = NSProcessInfo::processInfo().beginActivityWithOptions_reason(
        NSActivityOptions::UserInitiatedAllowingIdleSystemSleep,
        &NSString::from_str("Respond to dictation shortcuts"),
    );
    // Ending the activity would restore App Nap; it lasts for the process.
    std::mem::forget(activity);
}

/// Send a window's close request through the lifecycle instead of closing it.
/// Windows created after setup, such as the official installer's update popup,
/// call this themselves.
pub(crate) fn route_close_requests(
    app: &tauri::AppHandle,
    role: WindowRole,
    window: &tauri::WebviewWindow,
) {
    let app_handle = app.clone();
    window.on_window_event(move |event| {
        if let tauri::WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            request(&app_handle, role, WindowAction::CloseRequest);
        }
    });
}

pub(crate) fn install(
    app: &mut tauri::App,
    presentation: LaunchPresentation,
) -> Result<(), Box<dyn std::error::Error>> {
    install_overlay_spaces_policy(app.handle())?;
    install_main_window_chrome(app.handle())?;
    install_system_wake_observer(app.handle());
    hold_app_nap_exemption();
    if let Err(error) = install_recording_control_hover_tracking(app.handle()) {
        eprintln!("[tauri] macOS recording control hover tracking unavailable reason={error}");
    }
    let settings_item = MenuItem::with_id(app, "settings", "Settings", true, None::<&str>)?;
    let quit_item = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&settings_item, &quit_item])?;
    let icon = Image::from_bytes(include_bytes!("../icons/macos-menu-bar-template.png"))?;
    let _menu_bar_item = TrayIconBuilder::new()
        .icon(icon)
        .icon_as_template(true)
        .menu(&menu)
        .show_menu_on_left_click(false)
        .tooltip("Whisperstream")
        .on_menu_event(|app, event| match event.id.as_ref() {
            "settings" => show_main_window(app),
            "quit" => request(app, WindowRole::Main, WindowAction::Quit),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if matches!(
                event,
                TrayIconEvent::Click {
                    button: MouseButton::Left,
                    button_state: MouseButtonState::Up,
                    ..
                } | TrayIconEvent::DoubleClick {
                    button: MouseButton::Left,
                    ..
                }
            ) {
                show_main_window(tray.app_handle());
            }
        })
        .build(app)?;

    let mut lifecycle = lifecycle(app.handle());
    let launched = lifecycle.launch(presentation);
    if let Some(failure) = launched.failure {
        return Err(format!("macOS launch presentation failed: {failure:?}").into());
    }

    for role in [
        WindowRole::Main,
        WindowRole::Review,
        WindowRole::Eula,
        WindowRole::Error,
    ] {
        let window = app
            .get_webview_window(window_label(role))
            .ok_or_else(|| format!("missing startup-static window role={role:?}"))?;
        route_close_requests(app.handle(), role, &window);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn overlay_collection_policy_joins_other_apps_and_preserves_unrelated_behavior() {
        let conflicts = NSWindowCollectionBehavior::Primary
            | NSWindowCollectionBehavior::Auxiliary
            | NSWindowCollectionBehavior::FullScreenPrimary
            | NSWindowCollectionBehavior::FullScreenNone
            | NSWindowCollectionBehavior::MoveToActiveSpace;
        let unrelated = NSWindowCollectionBehavior::Stationary
            | NSWindowCollectionBehavior::IgnoresCycle;
        let required = NSWindowCollectionBehavior::CanJoinAllApplications
            | NSWindowCollectionBehavior::CanJoinAllSpaces
            | NSWindowCollectionBehavior::FullScreenAuxiliary;
        for initial in [NSWindowCollectionBehavior::empty(), conflicts | unrelated] {
            let result = overlay_collection_behavior(initial);
            assert!(result.contains(required));
            assert!(!result.intersects(conflicts));
            assert_eq!(result & unrelated, initial & unrelated);
            assert_eq!(overlay_collection_behavior(result), result);
        }
    }

    extern "C-unwind" fn test_cannot_become_key(_this: &AnyObject, _cmd: objc2::runtime::Sel) -> Bool {
        Bool::NO
    }

    fn tao_like_window_class(name: &CStr, with_ivar: bool) -> &'static AnyClass {
        let mut builder = ClassBuilder::new(name, NSWindow::class()).expect("unique test class");
        if with_ivar {
            builder.add_ivar::<Bool>(TAO_FOCUSABLE_IVAR);
        }
        // SAFETY: the signature matches `canBecomeKeyWindow`.
        unsafe {
            builder.add_method(
                objc2::sel!(canBecomeKeyWindow),
                test_cannot_become_key as extern "C-unwind" fn(_, _) -> _,
            );
        }
        builder.register()
    }

    #[test]
    fn overlay_panel_class_preserves_tao_layout_and_overrides() {
        let source = tao_like_window_class(c"WsTestTaoWindowCompatible", true);
        let panel = build_overlay_panel_class(source, c"WsTestOverlayPanelCompatible")
            .expect("tao-shaped window converts");
        assert_eq!(panel.superclass(), Some(NSPanel::class()));
        assert_eq!(panel.instance_size(), source.instance_size());
        let source_ivar = source.instance_variable(TAO_FOCUSABLE_IVAR).unwrap();
        let panel_ivar = panel.instance_variable(TAO_FOCUSABLE_IVAR).unwrap();
        assert_eq!(panel_ivar.offset(), source_ivar.offset());
        assert_eq!(panel_ivar.type_encoding(), source_ivar.type_encoding());
        let selector = objc2::sel!(canBecomeKeyWindow);
        assert_eq!(
            panel.instance_method(selector).unwrap().implementation() as usize,
            source.instance_method(selector).unwrap().implementation() as usize,
        );
    }

    #[test]
    fn overlay_panel_class_refuses_unknown_window_layouts() {
        let without_ivar = tao_like_window_class(c"WsTestTaoWindowNoIvar", false);
        assert_eq!(
            build_overlay_panel_class(without_ivar, c"WsTestOverlayPanelNoIvar"),
            Err(OverlayPanelRefusal::LayoutMismatch)
        );
        let mut builder =
            ClassBuilder::new(c"WsTestPanelSubclass", NSPanel::class()).expect("unique test class");
        builder.add_ivar::<Bool>(TAO_FOCUSABLE_IVAR);
        let not_a_window_subclass = builder.register();
        assert_eq!(
            build_overlay_panel_class(not_a_window_subclass, c"WsTestOverlayPanelWrongBase"),
            Err(OverlayPanelRefusal::UnexpectedWindowClass)
        );
    }

    #[test]
    fn kvo_observed_keys_accepts_only_simple_setter_overrides() {
        assert_eq!(
            kvo_observed_keys(["setContentView:", "class", "dealloc", "_isKVOA"]),
            Some(vec!["contentView".to_string()])
        );
        assert_eq!(kvo_observed_keys(["class", "dealloc"]), Some(vec![]));
        for unknown in ["setFrame:display:", "frame", "setlower:", "_setFoo:"] {
            assert_eq!(kvo_observed_keys([unknown]), None, "{unknown}");
        }
    }

    #[test]
    fn agent_hud_focus_release_preserves_user_destination_and_consumes_lease() {
        for focus in [Some(false), None] {
            let mut previous = Some(123);
            assert_eq!(take_agent_hud_restore_target(&mut previous, focus), None);
            assert_eq!(previous, None);
            assert_eq!(take_agent_hud_restore_target(&mut previous, Some(true)), None);
        }
        let mut previous = Some(123);
        assert_eq!(take_agent_hud_restore_target(&mut previous, Some(true)), Some(123));
        assert_eq!(previous, None);
        assert_eq!(take_agent_hud_restore_target(&mut previous, Some(true)), None);
    }
    use std::cell::RefCell;
    use std::rc::Rc;

    struct FakeState {
        calls: Vec<String>,
        foreground_visible: bool,
        login: LaunchAtLoginState,
    }

    impl Default for FakeState {
        fn default() -> Self {
            Self {
                calls: Vec::new(),
                foreground_visible: false,
                login: LaunchAtLoginState::Disabled,
            }
        }
    }

    #[derive(Clone)]
    struct FakeBridge(Rc<RefCell<FakeState>>);

    impl MacosNativeBridge for FakeBridge {
        fn mutate_window(
            &mut self,
            role: WindowRole,
            action: WindowAction,
            _geometry: Option<&LogicalGeometry>,
        ) -> Result<(bool, bool), WindowFailure> {
            self.0
                .borrow_mut()
                .calls
                .push(format!("window:{role:?}:{action:?}"));
            if !matches!(role, WindowRole::Hud | WindowRole::Snap) {
                self.0.borrow_mut().foreground_visible =
                    !matches!(action, WindowAction::Hide | WindowAction::CloseRequest);
            }
            Ok((
                !matches!(action, WindowAction::Hide | WindowAction::CloseRequest),
                action == WindowAction::Focus,
            ))
        }

        fn set_foreground_presentation(&mut self, foreground: bool) -> Result<(), WindowFailure> {
            self.0
                .borrow_mut()
                .calls
                .push(format!("presentation:{foreground}"));
            Ok(())
        }

        fn has_visible_foreground_window(&self) -> bool {
            self.0.borrow().foreground_visible
        }

        fn quit(&mut self) -> Result<(), WindowFailure> {
            self.0.borrow_mut().calls.push("quit".to_string());
            Ok(())
        }

        fn launch_at_login_state(&self) -> LaunchAtLoginResult {
            let state = self.0.borrow().login;
            LaunchAtLoginResult {
                state,
                failure: matches!(
                    state,
                    LaunchAtLoginState::NotFound
                        | LaunchAtLoginState::Unsupported
                        | LaunchAtLoginState::Unavailable
                )
                .then_some(WindowFailure::NativeFailure),
            }
        }

        fn register_launch_at_login(&mut self) -> LaunchAtLoginResult {
            self.0.borrow_mut().login = LaunchAtLoginState::RequiresApproval;
            self.launch_at_login_state()
        }

        fn unregister_launch_at_login(&mut self) -> LaunchAtLoginResult {
            self.0.borrow_mut().login = LaunchAtLoginState::Disabled;
            self.launch_at_login_state()
        }
    }

    #[test]
    fn macos_window_lifecycle_normal_minimized_close_reopen_and_quit() {
        let state = Rc::new(RefCell::new(FakeState {
            login: LaunchAtLoginState::Disabled,
            ..FakeState::default()
        }));
        let mut lifecycle = WindowLifecycle::new(MacosWindowPort {
            bridge: FakeBridge(Rc::clone(&state)),
        });

        assert!(lifecycle.launch(LaunchPresentation::Normal).visible);
        assert!(!lifecycle.launch(LaunchPresentation::Minimized).visible);
        let closed = lifecycle.apply(WindowLifecycleRequest {
            generation: 1,
            role: WindowRole::Main,
            action: WindowAction::CloseRequest,
            geometry: None,
        });
        assert!(!closed.visible);
        assert!(
            lifecycle
                .apply(WindowLifecycleRequest {
                    generation: 1,
                    role: WindowRole::Main,
                    action: WindowAction::Restore,
                    geometry: None,
                })
                .visible
        );
        lifecycle.apply(WindowLifecycleRequest {
            generation: 1,
            role: WindowRole::Main,
            action: WindowAction::Quit,
            geometry: None,
        });

        let calls = &state.borrow().calls;
        assert!(calls.starts_with(&[
            "presentation:true".to_string(),
            "window:Main:Show".to_string(),
        ]));
        assert!(calls.contains(&"presentation:false".to_string()));
        assert_eq!(calls.last().map(String::as_str), Some("quit"));
    }

    #[test]
    fn macos_window_lifecycle_login_requires_approval_is_not_booleanized() {
        let state = Rc::new(RefCell::new(FakeState {
            login: LaunchAtLoginState::Disabled,
            ..FakeState::default()
        }));
        let mut lifecycle = WindowLifecycle::new(MacosWindowPort {
            bridge: FakeBridge(Rc::clone(&state)),
        });
        assert_eq!(
            lifecycle.set_launch_at_login(true).state,
            LaunchAtLoginState::RequiresApproval
        );
        assert_eq!(
            lifecycle.set_launch_at_login(false).state,
            LaunchAtLoginState::Disabled
        );
    }

    #[test]
    fn macos_window_lifecycle_bounds_geometry_to_one_logical_display() {
        let display = DisplayBounds {
            token: "display-test".to_string(),
            x: -100.0,
            y: 20.0,
            width: 800.0,
            height: 600.0,
        };
        let result = bounded_geometry(
            &LogicalGeometry {
                x: -500.0,
                y: 900.0,
                width: 1200.0,
                height: 400.0,
                display_token: "display-test".to_string(),
            },
            &display,
        )
        .expect("geometry should clamp");
        assert_eq!((result.x, result.y), (-100.0, 220.0));
        assert_eq!((result.width, result.height), (800.0, 400.0));
    }

    #[test]
    fn macos_window_lifecycle_passive_status() {
        let status = SmAppServiceBridge::status();
        assert!(matches!(
            status.state,
            LaunchAtLoginState::Enabled
                | LaunchAtLoginState::Disabled
                | LaunchAtLoginState::RequiresApproval
                | LaunchAtLoginState::NotFound
                | LaunchAtLoginState::Unavailable
        ));
    }

    #[test]
    fn packaged_login_item_preflight_accepts_only_clean_absent_states() {
        assert!(clean_packaged_login_item_preflight(
            LaunchAtLoginState::Disabled
        ));
        assert!(clean_packaged_login_item_preflight(
            LaunchAtLoginState::NotFound
        ));
        for ambiguous in [
            LaunchAtLoginState::Enabled,
            LaunchAtLoginState::RequiresApproval,
            LaunchAtLoginState::Unsupported,
            LaunchAtLoginState::Unavailable,
        ] {
            assert!(!clean_packaged_login_item_preflight(ambiguous));
        }
    }
}
