import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  tauri: true,
  save: vi.fn(),
  writeFile: vi.fn(),
  invoke: vi.fn(),
}));

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => mocks.tauri,
  invoke: mocks.invoke,
}));
vi.mock("@tauri-apps/plugin-dialog", () => ({ save: mocks.save }));
vi.mock("@tauri-apps/plugin-fs", () => ({ writeFile: mocks.writeFile }));

import { eulaTermsFilenameForTest, saveExactEulaTerms } from "./eulaTerms";

describe("exact EULA terms save", () => {
  beforeEach(() => {
    mocks.tauri = true;
    mocks.save.mockReset().mockResolvedValue("C:\\review\\terms.txt");
    mocks.writeFile.mockReset().mockResolvedValue(undefined);
    mocks.invoke.mockReset().mockRejectedValue("native Mac file workflow unsupported");
  });

  it("uses a version-identifying filename and writes the exact UTF-8 bytes", async () => {
    const text = "First line\r\nSecond line\r\n";
    await expect(saveExactEulaTerms(text, "2.0")).resolves.toBe("saved");
    expect(mocks.save).toHaveBeenCalledWith(expect.objectContaining({
      defaultPath: "Whisperstream-EULA-2.0.txt",
    }));
    const bytes = mocks.writeFile.mock.calls[0][1] as Uint8Array;
    expect(new TextDecoder().decode(bytes)).toBe(text);
  });

  it("does not write when the user cancels the native dialog", async () => {
    mocks.save.mockResolvedValueOnce(null);
    await expect(saveExactEulaTerms("terms", "2.0")).resolves.toBe("cancelled");
    expect(mocks.writeFile).not.toHaveBeenCalled();
  });

  it("keeps the Mac save destination inside the native command", async () => {
    mocks.invoke.mockResolvedValueOnce({ status: "saved" });
    await expect(saveExactEulaTerms("exact terms", "2.0")).resolves.toBe("saved");
    expect(mocks.invoke).toHaveBeenCalledWith("save_macos_private_file", {
      kind: "retain-eula",
      defaultName: "Whisperstream-EULA-2.0.txt",
      payload: Array.from(new TextEncoder().encode("exact terms")),
    });
    expect(mocks.save).not.toHaveBeenCalled();
    expect(mocks.writeFile).not.toHaveBeenCalled();
  });

  it("does not swallow native write failures", async () => {
    mocks.writeFile.mockRejectedValueOnce(new Error("disk full"));
    await expect(saveExactEulaTerms("terms", "2.0")).rejects.toThrow("disk full");
  });

  it("normalizes only the filename, never the agreement bytes", () => {
    expect(eulaTermsFilenameForTest("1.4")).toBe("Whisperstream-EULA-1.4.txt");
  });
});
