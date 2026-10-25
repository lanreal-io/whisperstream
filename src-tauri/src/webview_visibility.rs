//! Keep the settings WebView2 controller in step with its native window.
//! This hides rendering, not JavaScript execution or backend subscriptions.
//! Do not install on measured HUD/popup surfaces: their hidden preparation
//! currently depends on animation frames before their native show handshake.

use std::cell::Cell;
use windows::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass};
use windows::Win32::UI::WindowsAndMessaging::{
    IsIconic, IsWindowVisible, WM_NCDESTROY, WM_SHOWWINDOW, WM_SIZE, WM_WINDOWPOSCHANGED,
};

const SUBCLASS_ID: usize = 3;

struct VisibilityState {
    visible: Cell<Option<bool>>,
    apply: Box<dyn Fn(bool) -> bool>,
}

impl VisibilityState {
    fn sync(&self, hwnd: HWND) {
        let visible = unsafe { IsWindowVisible(hwnd).as_bool() && !IsIconic(hwnd).as_bool() };
        if self.visible.get() != Some(visible) && (self.apply)(visible) {
            self.visible.set(Some(visible));
        }
    }
}

unsafe extern "system" fn visibility_guard(
    hwnd: HWND,
    message: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    subclass_id: usize,
    reference: usize,
) -> LRESULT {
    if message == WM_NCDESTROY {
        // Installed and removed on the owning UI thread. Release the COM
        // controller before the native window finishes destruction.
        unsafe {
            let _ = RemoveWindowSubclass(hwnd, Some(visibility_guard), subclass_id);
            drop(Box::from_raw(reference as *mut VisibilityState));
        }
        return unsafe { DefSubclassProc(hwnd, message, wparam, lparam) };
    }
    let result = unsafe { DefSubclassProc(hwnd, message, wparam, lparam) };
    if matches!(message, WM_SHOWWINDOW | WM_SIZE | WM_WINDOWPOSCHANGED) {
        // Read settled native state rather than Tauri's cached window flags.
        unsafe { &*(reference as *const VisibilityState) }.sync(hwnd);
    }
    result
}

pub fn install(window: &tauri::WebviewWindow) -> tauri::Result<()> {
    // WebView2's ParentWindow can be Wry's child container. Observe the
    // top-level Tauri HWND, whose show/hide/minimize state owns presentation.
    let native_hwnd = window.hwnd()?.0 as usize;
    // with_webview runs on the UI thread; the controller never crosses threads.
    window.with_webview(move |webview| {
        let controller = webview.controller();
        let hwnd = HWND(native_hwnd as *mut _);
        let state = Box::new(VisibilityState {
            visible: Cell::new(None),
            apply: Box::new(move |visible| {
                let result = unsafe { controller.SetIsVisible(visible) };
                if result.is_err() {
                    eprintln!("[tauri] settings rendering visibility sync failed");
                }
                result.is_ok()
            }),
        });
        let reference = Box::into_raw(state);
        if !unsafe { SetWindowSubclass(hwnd, Some(visibility_guard), SUBCLASS_ID, reference as usize) }.as_bool() {
            unsafe { drop(Box::from_raw(reference)); }
            eprintln!("[tauri] settings rendering visibility install failed stage=subclass");
            return;
        }
        unsafe { &*reference }.sync(hwnd);
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::rc::Rc;
    use windows::core::w;
    use windows::Win32::UI::WindowsAndMessaging::*;

    #[test]
    fn native_hide_minimize_restore_and_destroy_update_visibility() {
        unsafe {
            let hwnd = CreateWindowExW(
                WINDOW_EX_STYLE::default(), w!("STATIC"), w!(""),
                WS_POPUP, 0, 0, 16, 16, None, None, None, None,
            ).unwrap();
            struct OwnedWindow(HWND);
            impl Drop for OwnedWindow {
                fn drop(&mut self) { unsafe { let _ = DestroyWindow(self.0); } }
            }
            let owned = OwnedWindow(hwnd);
            let visible = Rc::new(Cell::new(None));
            let observed = visible.clone();
            let reference = Box::into_raw(Box::new(VisibilityState {
                visible: Cell::new(None),
                apply: Box::new(move |value| { observed.set(Some(value)); true }),
            }));
            SetWindowSubclass(hwnd, Some(visibility_guard), SUBCLASS_ID, reference as usize)
                .ok().unwrap();
            (&*reference).sync(hwnd);
            assert_eq!(visible.get(), Some(false));
            for (command, expected) in [
                (SW_SHOWNOACTIVATE, true), (SW_HIDE, false),
                (SW_SHOWNOACTIVATE, true), (SW_SHOWMINNOACTIVE, false),
                (SW_SHOWNOACTIVATE, true), (SW_HIDE, false),
            ] {
                let _ = ShowWindow(hwnd, command);
                assert_eq!(visible.get(), Some(expected));
            }
            assert_eq!(Rc::strong_count(&visible), 2);
            drop(owned);
            assert_eq!(Rc::strong_count(&visible), 1, "destroy releases controller callback");
        }
    }
}
