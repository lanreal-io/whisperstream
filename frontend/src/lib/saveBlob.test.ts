import { afterEach, expect, it, vi } from "vitest";
import { saveBlob } from "./saveBlob";

const mocks = vi.hoisted(() => ({ save: vi.fn(), writeFile: vi.fn() }));
vi.mock("@tauri-apps/plugin-dialog", () => ({ save: mocks.save }));
vi.mock("@tauri-apps/plugin-fs", () => ({ writeFile: mocks.writeFile }));
afterEach(() => { vi.unstubAllGlobals(); vi.resetAllMocks(); });

it("cancels without writing and propagates real native write errors", async () => {
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  mocks.save.mockResolvedValueOnce(null);
  expect(await saveBlob(new Blob(), "Policy.pdf", "pdf")).toBe("cancelled");
  expect(mocks.writeFile).not.toHaveBeenCalled();
  mocks.save.mockResolvedValueOnce("C:/selected/Policy.pdf");
  const blob = { arrayBuffer: async () => new Uint8Array([37, 80, 68, 70]).buffer } as Blob;
  mocks.writeFile.mockRejectedValueOnce(new Error("disk full"));
  await expect(saveBlob(blob, "Policy.pdf", "pdf")).rejects.toThrow("disk full");
});
