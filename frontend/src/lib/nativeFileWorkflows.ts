import { invoke, isTauri } from "@tauri-apps/api/core";

export interface NativeFileSelection {
  token: string;
  display_name: string;
}

export interface NativePickerResult {
  status: "selected" | "cancelled" | "failed";
  selections: NativeFileSelection[];
  destination_token: string | null;
}

export const MAX_NATIVE_SAVE_BYTES = 64 * 1024 * 1024;

export class NativeSavePayloadTooLargeError extends Error {
  constructor() {
    super("native save payload exceeds the bounded IPC limit");
    this.name = "NativeSavePayloadTooLargeError";
  }
}

export async function readBoundedNativeSaveResponse(
  response: Response,
): Promise<Uint8Array> {
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > MAX_NATIVE_SAVE_BYTES) {
    throw new NativeSavePayloadTooLargeError();
  }
  if (!response.body) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > MAX_NATIVE_SAVE_BYTES) {
      throw new NativeSavePayloadTooLargeError();
    }
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_NATIVE_SAVE_BYTES) {
        await reader.cancel();
        throw new NativeSavePayloadTooLargeError();
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

export async function supportsMacosPrivateFileWorkflows(): Promise<boolean> {
  if (!isTauri()) return false;
  return invoke<boolean>("macos_private_file_workflows_supported");
}

export async function routePrivateFileWorkflowResponse(
  response: Response,
): Promise<
  | { macosNative: true; bytes: Uint8Array }
  | { macosNative: false; response: Response }
> {
  const macosNative = await supportsMacosPrivateFileWorkflows();
  if (!macosNative) return { macosNative: false, response };
  return { macosNative: true, bytes: await readBoundedNativeSaveResponse(response) };
}

export async function chooseMacosImportAudio(): Promise<NativePickerResult | null> {
  if (!isTauri()) return null;
  try {
    return await invoke<NativePickerResult>("choose_macos_import_audio");
  } catch {
    return { status: "failed", selections: [], destination_token: null };
  }
}

export async function chooseMacosModelStorage(): Promise<NativePickerResult | null> {
  if (!isTauri()) return null;
  try {
    return await invoke<NativePickerResult>("choose_macos_model_storage");
  } catch {
    return { status: "failed", selections: [], destination_token: null };
  }
}

export async function releaseMacosNativeSelections(tokens: string[]): Promise<void> {
  if (!isTauri() || tokens.length === 0) return;
  await invoke("release_macos_native_selections", { tokens });
}

export async function saveMacosPrivateFile(
  kind: "export-transcript" | "retain-eula" | "export-logs",
  defaultName: string,
  payload: Uint8Array,
): Promise<"saved" | "cancelled" | "unsupported"> {
  if (!isTauri()) return "unsupported";
  if (payload.byteLength > MAX_NATIVE_SAVE_BYTES) {
    throw new NativeSavePayloadTooLargeError();
  }
  try {
    const result = await invoke<{ status: "saved" | "cancelled" | "failed" }>(
      "save_macos_private_file",
      { kind, defaultName, payload: Array.from(payload) },
    );
    if (result.status === "failed") throw new Error("native file save failed");
    return result.status;
  } catch (error) {
    if (String(error).includes("unsupported")) return "unsupported";
    throw error;
  }
}
