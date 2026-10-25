import { afterEach, expect, it, vi } from "vitest";
import { exportLogs } from "./api/diagnostics";

const mocks = vi.hoisted(() => ({ save: vi.fn(), writeFile: vi.fn() }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ save: mocks.save }));
vi.mock("@tauri-apps/plugin-fs", () => ({ writeFile: mocks.writeFile }));

afterEach(() => { vi.unstubAllGlobals(); vi.resetAllMocks(); });

it("keeps native log cancellation and disk failure distinct from success", async () => {
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  const blob = { arrayBuffer: async () => new Uint8Array([80, 75]).buffer } as Blob;
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, blob: async () => blob }));
  const browserDownload = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = browserDownload; });
  mocks.save.mockResolvedValueOnce(null);
  expect(await exportLogs()).toBe("cancelled");
  expect(mocks.writeFile).not.toHaveBeenCalled();
  mocks.save.mockResolvedValueOnce("C:/synthetic/logs.zip");
  mocks.writeFile.mockRejectedValueOnce(new Error("disk full"));
  await expect(exportLogs()).rejects.toThrow("disk full");
  expect(browserDownload).not.toHaveBeenCalled();
});
