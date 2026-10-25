import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { webcrypto } from "node:crypto";
import { saveDocumentation, revealDocumentation } from "./offlineDocumentation";
import manifest from "../assets/documentation/manifest.json";

const mocks = vi.hoisted(() => ({open:vi.fn(), exists:vi.fn(), mkdir:vi.fn(), writeFile:vi.fn(), reveal:vi.fn()}));
vi.mock("@tauri-apps/plugin-dialog", () => ({open:mocks.open}));
vi.mock("@tauri-apps/plugin-fs", () => ({exists:mocks.exists,mkdir:mocks.mkdir,writeFile:mocks.writeFile}));
vi.mock("@tauri-apps/api/path", () => ({join: (...parts:string[]) => Promise.resolve(parts.join("/"))}));
vi.mock("@tauri-apps/plugin-opener", () => ({revealItemInDir:mocks.reveal}));
beforeEach(() => {
  mocks.open.mockResolvedValue("C:/chosen");
  mocks.exists.mockResolvedValue(false);
  mocks.mkdir.mockResolvedValue(undefined);
  mocks.writeFile.mockResolvedValue(undefined);
  vi.stubGlobal("crypto",webcrypto);
  vi.stubGlobal("fetch",vi.fn(async (url:string) => {
    const filename = new URL(url,"http://localhost").pathname.replace(/^\//,"");
    const bytes = readFileSync(path.join(process.cwd(),filename));
    return new Response(bytes);
  }));
});
afterEach(() => {vi.resetAllMocks();vi.unstubAllGlobals();});

describe("bundled documentation save", () => {
  it("saves the verified PDF and every Markdown file inside the picked folder",async () => {
    const folder = await saveDocumentation();
    expect(folder).toBe("C:/chosen/Whisperstream documentation");
    expect(mocks.open).toHaveBeenCalledWith(expect.objectContaining({directory:true,recursive:true}));
    expect(mocks.writeFile).toHaveBeenCalledTimes(Object.keys(manifest.files).length);
    expect(mocks.mkdir).toHaveBeenCalledWith(folder);
    for (const [file,bytes] of mocks.writeFile.mock.calls) {
      expect(file.startsWith(`${folder}/`)).toBe(true);
      expect(bytes.byteLength).toBeGreaterThan(0);
    }
    await revealDocumentation(folder!);
    expect(mocks.reveal).toHaveBeenCalledWith(`${folder}/Whisperstream-Documentation.pdf`);
  });
  it("cancels before reading or writing any files",async () => {
    mocks.open.mockResolvedValue(null);
    expect(await saveDocumentation()).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
    expect(mocks.mkdir).not.toHaveBeenCalled();
    expect(mocks.writeFile).not.toHaveBeenCalled();
  });
  it("preserves an earlier export by creating a fresh sibling folder",async () => {
    mocks.exists.mockImplementation(async (name:string) => !name.endsWith("(2)"));
    expect(await saveDocumentation()).toBe("C:/chosen/Whisperstream documentation (2)");
    expect(mocks.mkdir).not.toHaveBeenCalledWith("C:/chosen/Whisperstream documentation");
  });
  it("rejects damaged bundled files before creating a partial export",async () => {
    vi.mocked(fetch).mockImplementation(async () => new Response("damaged"));
    await expect(saveDocumentation()).rejects.toThrow("checksum mismatch");
    expect(mocks.mkdir).not.toHaveBeenCalled();
  });
  it("reports a native write failure instead of falling back to a browser download",async () => {
    mocks.writeFile.mockRejectedValue(new Error("disk full"));
    await expect(saveDocumentation()).rejects.toThrow("disk full");
  });
});
