import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  tauri: true,
  invoke: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => mocks.tauri,
  invoke: mocks.invoke,
}));

import {
  MAX_NATIVE_SAVE_BYTES,
  NativeSavePayloadTooLargeError,
  chooseMacosImportAudio,
  chooseMacosModelStorage,
  routePrivateFileWorkflowResponse,
  releaseMacosNativeSelections,
  saveMacosPrivateFile,
  supportsMacosPrivateFileWorkflows,
} from "./nativeFileWorkflows";

describe("Mac native file workflow bridge", () => {
  beforeEach(() => {
    mocks.tauri = true;
    mocks.invoke.mockReset();
  });

  it("returns only opaque import selections and cancellation", async () => {
    mocks.invoke.mockResolvedValueOnce({
      status: "selected",
      selections: [{ token: "macos-native:synthetic", display_name: "reviewed.wav" }],
      destination_token: null,
    });
    await expect(chooseMacosImportAudio()).resolves.toEqual({
      status: "selected",
      selections: [{ token: "macos-native:synthetic", display_name: "reviewed.wav" }],
      destination_token: null,
    });
    mocks.invoke.mockResolvedValueOnce({
      status: "cancelled",
      selections: [],
      destination_token: null,
    });
    await expect(chooseMacosModelStorage()).resolves.toMatchObject({
      status: "cancelled",
      destination_token: null,
    });
  });

  it("distinguishes a native picker failure from explicit cancellation", async () => {
    mocks.invoke.mockRejectedValueOnce(new Error("native panel unavailable"));
    await expect(chooseMacosImportAudio()).resolves.toEqual({
      status: "failed",
      selections: [],
      destination_token: null,
    });
  });

  it("releases exact tokens and sends no destination path", async () => {
    mocks.invoke.mockResolvedValue(undefined);
    await releaseMacosNativeSelections(["macos-native:one"]);
    expect(mocks.invoke).toHaveBeenCalledWith("release_macos_native_selections", {
      tokens: ["macos-native:one"],
    });
  });

  it("passes bounded export metadata and bytes to the native save session", async () => {
    mocks.invoke.mockResolvedValue({ status: "saved" });
    await expect(
      saveMacosPrivateFile("export-logs", "whisperstream-logs.zip", new Uint8Array([1, 2])),
    ).resolves.toBe("saved");
    expect(mocks.invoke).toHaveBeenCalledWith("save_macos_private_file", {
      kind: "export-logs",
      defaultName: "whisperstream-logs.zip",
      payload: [1, 2],
    });
  });

  it("rejects oversized native IPC payloads before invoking Rust", async () => {
    const oversized = {
      byteLength: MAX_NATIVE_SAVE_BYTES + 1,
    } as Uint8Array;
    await expect(
      saveMacosPrivateFile("export-transcript", "recording.wav", oversized),
    ).rejects.toBeInstanceOf(NativeSavePayloadTooLargeError);
    expect(mocks.invoke).not.toHaveBeenCalled();
  });

  it("uses the authoritative target capability before applying the Mac response cap", async () => {
    const legacyArrayBuffer = vi.fn().mockResolvedValue(
      new Uint8Array([7, 8]).buffer,
    );
    const response = {
      headers: new Headers({
        "content-length": String(MAX_NATIVE_SAVE_BYTES + 1),
      }),
      arrayBuffer: legacyArrayBuffer,
    } as unknown as Response;

    mocks.invoke.mockResolvedValueOnce(false);
    await expect(routePrivateFileWorkflowResponse(response)).resolves.toEqual({
      macosNative: false,
      response,
    });
    expect(mocks.invoke).toHaveBeenCalledWith(
      "macos_private_file_workflows_supported",
    );
    expect(legacyArrayBuffer).not.toHaveBeenCalled();

    legacyArrayBuffer.mockClear();
    mocks.invoke.mockResolvedValueOnce(true);
    await expect(routePrivateFileWorkflowResponse(response)).rejects.toBeInstanceOf(
      NativeSavePayloadTooLargeError,
    );
    expect(legacyArrayBuffer).not.toHaveBeenCalled();
  });

  it("is inert outside Tauri", async () => {
    mocks.tauri = false;
    await expect(supportsMacosPrivateFileWorkflows()).resolves.toBe(false);
    await expect(chooseMacosImportAudio()).resolves.toBeNull();
    await expect(
      saveMacosPrivateFile("retain-eula", "terms.txt", new Uint8Array()),
    ).resolves.toBe("unsupported");
    expect(mocks.invoke).not.toHaveBeenCalled();
  });
});
