//! Native-free user-facing window and application lifecycle contract.

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WindowRole {
    Main,
    Hud,
    Snap,
    Review,
    Update,
    Eula,
    Error,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WindowAction {
    Show,
    Hide,
    Focus,
    Minimize,
    Restore,
    CloseRequest,
    Quit,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WindowCloseDisposition {
    Hide,
    Close,
    Quit,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LaunchPresentation {
    Normal,
    Minimized,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LaunchAtLoginState {
    Enabled,
    Disabled,
    RequiresApproval,
    NotFound,
    Unsupported,
    Unavailable,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum WindowFailure {
    Unsupported,
    NotFound,
    InvalidState,
    DisplayUnavailable,
    StaleGeneration,
    NativeFailure,
}

#[derive(Clone, Debug, PartialEq)]
pub struct LogicalGeometry {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
    pub display_token: String,
}

impl LogicalGeometry {
    pub fn validate(&self) -> Result<(), WindowFailure> {
        if !self.x.is_finite()
            || !self.y.is_finite()
            || !self.width.is_finite()
            || !self.height.is_finite()
            || self.width <= 0.0
            || self.height <= 0.0
            || self.display_token.trim().is_empty()
        {
            return Err(WindowFailure::InvalidState);
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct WindowPolicy {
    pub role: WindowRole,
    pub close_disposition: WindowCloseDisposition,
    pub focusable: bool,
    pub resizable: bool,
    pub visible_on_normal_launch: bool,
    pub visible_on_minimized_launch: bool,
}

pub const fn policy(role: WindowRole) -> WindowPolicy {
    match role {
        WindowRole::Main => WindowPolicy {
            role,
            close_disposition: WindowCloseDisposition::Hide,
            focusable: true,
            resizable: false,
            visible_on_normal_launch: true,
            visible_on_minimized_launch: false,
        },
        WindowRole::Hud | WindowRole::Snap => WindowPolicy {
            role,
            close_disposition: WindowCloseDisposition::Hide,
            focusable: false,
            resizable: false,
            visible_on_normal_launch: false,
            visible_on_minimized_launch: false,
        },
        WindowRole::Review
        | WindowRole::Update
        | WindowRole::Eula
        | WindowRole::Error => WindowPolicy {
            role,
            close_disposition: WindowCloseDisposition::Hide,
            focusable: true,
            resizable: false,
            visible_on_normal_launch: false,
            visible_on_minimized_launch: false,
        },
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct WindowLifecycleRequest {
    pub generation: u64,
    pub role: WindowRole,
    pub action: WindowAction,
    pub geometry: Option<LogicalGeometry>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct WindowLifecycleResult {
    pub generation: u64,
    pub role: WindowRole,
    pub action: WindowAction,
    pub visible: bool,
    pub focused: bool,
    pub failure: Option<WindowFailure>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LaunchAtLoginResult {
    pub state: LaunchAtLoginState,
    pub failure: Option<WindowFailure>,
}

#[derive(Clone, Debug, Eq, PartialEq, serde::Serialize)]
pub struct LaunchAtLoginCommandResult {
    pub state: &'static str,
    pub failure: Option<&'static str>,
}

impl From<LaunchAtLoginResult> for LaunchAtLoginCommandResult {
    fn from(result: LaunchAtLoginResult) -> Self {
        Self {
            state: match result.state {
                LaunchAtLoginState::Enabled => "enabled",
                LaunchAtLoginState::Disabled => "disabled",
                LaunchAtLoginState::RequiresApproval => "requires-approval",
                LaunchAtLoginState::NotFound => "not-found",
                LaunchAtLoginState::Unsupported => "unsupported",
                LaunchAtLoginState::Unavailable => "unavailable",
            },
            failure: result.failure.map(|failure| match failure {
                WindowFailure::Unsupported => "unsupported",
                WindowFailure::NotFound => "not-found",
                WindowFailure::InvalidState => "invalid-state",
                WindowFailure::DisplayUnavailable => "display-unavailable",
                WindowFailure::StaleGeneration => "stale-generation",
                WindowFailure::NativeFailure => "native-failure",
            }),
        }
    }
}

pub trait WindowLifecyclePort {
    fn mutate(
        &mut self,
        role: WindowRole,
        action: WindowAction,
        geometry: Option<&LogicalGeometry>,
    ) -> Result<(bool, bool), WindowFailure>;

    fn set_launch_at_login(&mut self, enabled: bool) -> LaunchAtLoginResult;

    fn launch_at_login_state(&self) -> LaunchAtLoginResult;
}

pub struct WindowLifecycle<P> {
    port: P,
    generation: u64,
}

impl<P: WindowLifecyclePort> WindowLifecycle<P> {
    pub fn new(port: P) -> Self {
        Self {
            port,
            generation: 1,
        }
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn launch(&mut self, presentation: LaunchPresentation) -> WindowLifecycleResult {
        let action = match presentation {
            LaunchPresentation::Normal => WindowAction::Show,
            LaunchPresentation::Minimized => WindowAction::Hide,
        };
        self.apply(WindowLifecycleRequest {
            generation: self.generation,
            role: WindowRole::Main,
            action,
            geometry: None,
        })
    }

    pub fn apply(&mut self, request: WindowLifecycleRequest) -> WindowLifecycleResult {
        if request.generation != self.generation {
            return self.failed(&request, WindowFailure::StaleGeneration);
        }
        if let Some(geometry) = request.geometry.as_ref() {
            if let Err(failure) = geometry.validate() {
                return self.failed(&request, failure);
            }
        }
        let action = if request.action == WindowAction::CloseRequest {
            match policy(request.role).close_disposition {
                WindowCloseDisposition::Hide => WindowAction::Hide,
                WindowCloseDisposition::Close => WindowAction::CloseRequest,
                WindowCloseDisposition::Quit => WindowAction::Quit,
            }
        } else {
            request.action
        };
        match self
            .port
            .mutate(request.role, action, request.geometry.as_ref())
        {
            Ok((visible, focused)) => WindowLifecycleResult {
                generation: request.generation,
                role: request.role,
                action: request.action,
                visible,
                focused,
                failure: None,
            },
            Err(failure) => self.failed(&request, failure),
        }
    }

    pub fn set_launch_at_login(&mut self, enabled: bool) -> LaunchAtLoginResult {
        self.port.set_launch_at_login(enabled)
    }

    pub fn launch_at_login_state(&self) -> LaunchAtLoginResult {
        self.port.launch_at_login_state()
    }

    fn failed(
        &self,
        request: &WindowLifecycleRequest,
        failure: WindowFailure,
    ) -> WindowLifecycleResult {
        WindowLifecycleResult {
            generation: request.generation,
            role: request.role,
            action: request.action,
            visible: false,
            focused: false,
            failure: Some(failure),
        }
    }
}

pub const WINDOW_LIFECYCLE_SCHEMA_VERSION: u32 = 1;

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct FakePort {
        calls: Vec<(WindowRole, WindowAction)>,
        login: bool,
        fail: Option<WindowFailure>,
    }

    impl WindowLifecyclePort for FakePort {
        fn mutate(
            &mut self,
            role: WindowRole,
            action: WindowAction,
            _geometry: Option<&LogicalGeometry>,
        ) -> Result<(bool, bool), WindowFailure> {
            self.calls.push((role, action));
            if let Some(failure) = self.fail {
                return Err(failure);
            }
            Ok((action != WindowAction::Hide, action == WindowAction::Focus))
        }

        fn set_launch_at_login(&mut self, enabled: bool) -> LaunchAtLoginResult {
            self.login = enabled;
            self.launch_at_login_state()
        }

        fn launch_at_login_state(&self) -> LaunchAtLoginResult {
            LaunchAtLoginResult {
                state: if self.login {
                    LaunchAtLoginState::Enabled
                } else {
                    LaunchAtLoginState::Disabled
                },
                failure: None,
            }
        }
    }

    #[test]
    fn normal_and_minimized_launch_are_explicit() {
        let mut lifecycle = WindowLifecycle::new(FakePort::default());
        assert!(lifecycle.launch(LaunchPresentation::Normal).visible);
        assert!(!lifecycle.launch(LaunchPresentation::Minimized).visible);
    }

    #[test]
    fn main_close_request_hides_and_stale_generation_cannot_mutate() {
        let mut lifecycle = WindowLifecycle::new(FakePort::default());
        let closed = lifecycle.apply(WindowLifecycleRequest {
            generation: 1,
            role: WindowRole::Main,
            action: WindowAction::CloseRequest,
            geometry: None,
        });
        assert_eq!(closed.failure, None);
        assert!(!closed.visible);
        let stale = lifecycle.apply(WindowLifecycleRequest {
            generation: 2,
            role: WindowRole::Hud,
            action: WindowAction::Show,
            geometry: None,
        });
        assert_eq!(stale.failure, Some(WindowFailure::StaleGeneration));
    }

    #[test]
    fn login_and_policy_states_are_typed() {
        let mut lifecycle = WindowLifecycle::new(FakePort::default());
        assert_eq!(
            lifecycle.set_launch_at_login(true).state,
            LaunchAtLoginState::Enabled
        );
        assert_eq!(policy(WindowRole::Hud).focusable, false);
        assert_eq!(
            policy(WindowRole::Main).close_disposition,
            WindowCloseDisposition::Hide
        );
        assert_eq!(WINDOW_LIFECYCLE_SCHEMA_VERSION, 1);
    }
}
