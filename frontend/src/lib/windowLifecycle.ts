import { invoke, isTauri } from "@tauri-apps/api/core";
import { emit } from "@tauri-apps/api/event";

const POPUP_LABELS: Partial<Record<WindowLifecycleRole, string>> = {
  review: "popup-review",
  update: "popup-update", eula: "popup-eula", error: "popup-error",
};

export async function publishPopupVisibility(label: string, visible: boolean): Promise<void> {
  await emit(`${label}:${visible ? "shown" : "hidden"}`).catch(() => {});
}

export type WindowLifecycleAction = "show" | "hide" | "focus" | "minimize" | "restore" | "close-request" | "quit";

export type WindowLifecycleRole =
  | "main"
  | "hud"
  | "snap"
  | "review"
  | "update"
  | "eula"
  | "error";

export async function applyWindowLifecycle(
  role: WindowLifecycleRole,
  action: WindowLifecycleAction,
): Promise<boolean> {
  if (!isTauri()) return false;
  await invoke("apply_window_lifecycle", { generation: 1, role, action });
  const label = POPUP_LABELS[role];
  if (label && (action === "show" || action === "hide")) await publishPopupVisibility(label, action === "show");
  return true;
}
