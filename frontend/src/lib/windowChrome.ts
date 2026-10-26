// macOS draws native traffic lights over the main window's top-left corner;
// Windows uses the app's own minimize/close buttons.
export function usesNativeWindowControls(
  navigatorPlatform = typeof navigator === "undefined" ? "" : navigator.platform,
): boolean {
  return /mac/i.test(navigatorPlatform);
}
