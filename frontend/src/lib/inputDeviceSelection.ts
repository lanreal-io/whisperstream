import type { AppConfig } from "./api/config";
import type { AudioDevice } from "./api/system";


export type InputDeviceInventoryStatus = "idle" | "loading" | "loaded" | "error";
export type InputDeviceAvailability = "available" | "unavailable" | "unknown";


export function usesOpaqueInputIdentity(
  selectionMode: AppConfig["audio_input_device_selection_mode"],
): boolean {
  return selectionMode === "opaque-id";
}


export function inputDeviceValue(
  device: AudioDevice,
  selectionMode: AppConfig["audio_input_device_selection_mode"],
): string {
  return usesOpaqueInputIdentity(selectionMode) ? device.id : device.name;
}


export function inputDevicePatch(
  device: AudioDevice | null,
  selectionMode: AppConfig["audio_input_device_selection_mode"],
): Partial<AppConfig> {
  if (!device) {
    return usesOpaqueInputIdentity(selectionMode)
      ? { audio_input_device: "", audio_input_device_label: "" }
      : { audio_input_device: "" };
  }
  return usesOpaqueInputIdentity(selectionMode)
    ? {
        audio_input_device: device.id,
        audio_input_device_label: device.name,
      }
    : { audio_input_device: device.name };
}


export function selectedInputDevice(
  value: string,
  devices: AudioDevice[] | null,
  selectionMode: AppConfig["audio_input_device_selection_mode"],
): AudioDevice | undefined {
  return devices?.find(
    (device) => inputDeviceValue(device, selectionMode) === value,
  );
}


export function savedInputDeviceLabel(
  config: Pick<AppConfig, "audio_input_device" | "audio_input_device_label">,
  current: AudioDevice | undefined,
  selectionMode: AppConfig["audio_input_device_selection_mode"],
): string {
  if (!config.audio_input_device) return "Default";
  if (current) return current.name;
  if (!usesOpaqueInputIdentity(selectionMode)) return config.audio_input_device;
  return config.audio_input_device_label?.trim() || "Saved microphone";
}


export function savedInputDeviceAvailability(
  value: string,
  current: AudioDevice | undefined,
  selectionMode: AppConfig["audio_input_device_selection_mode"],
  inventoryStatus: InputDeviceInventoryStatus,
  savedAvailable?: boolean,
): InputDeviceAvailability {
  if (!value || !usesOpaqueInputIdentity(selectionMode) || current) {
    return "available";
  }
  if (inventoryStatus === "error") return "unknown";
  if (inventoryStatus === "loaded") return "unavailable";
  if (savedAvailable === false) return "unavailable";
  if (savedAvailable === true) return "available";
  return "unknown";
}
