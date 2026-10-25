import { afterEach, describe, expect, it, vi } from "vitest";
import {
  buildRecoveryFileText,
  downloadRecoveryFile,
  recoveryFileName,
  recoveryFileTitle,
} from "./lockdownRecovery";

const saveBlob = vi.hoisted(() => vi.fn());
vi.mock("./saveBlob", () => ({ saveBlob }));

const KEY = "WSRK-1111-2222-3333-4444";
const DATE = new Date("2026-08-05T12:00:00Z");

afterEach(() => {
  vi.clearAllMocks();
});

describe("buildRecoveryFileText, lockdown", () => {
  const text = buildRecoveryFileText(KEY, { variant: "lockdown", date: DATE });

  it("keeps the vetted body with code renamed to key", () => {
    expect(text.startsWith("Whisperstream Lockdown Mode - Recovery Key\n")).toBe(true);
    expect(text).toContain(`RECOVERY KEY:\n${KEY}\n`);
    expect(text).toContain("Keep this key somewhere safe and private.");
    expect(text).toContain(
      "- This one-time key is the only way back into your transcripts if you forget your",
    );
    expect(text).toContain(
      "- Anyone who has this key can unlock your transcripts, so treat it like a key.",
    );
    expect(text).toContain(
      "If you lose both your password and recovery key, your history can't be restored. Reset deletes it instead.",
    );
    expect(text).toContain("Lanreal Technologies never receives this key.");
    expect(text).not.toMatch(/recovery code|this code|RECOVERY CODE/i);
    expect(text).not.toContain("permanently");
    expect(text).not.toContain("—");
  });
});

describe("buildRecoveryFileText, history", () => {
  it("fills this Mac on Mac", () => {
    const text = buildRecoveryFileText(KEY, { variant: "history", platform: "mac", date: DATE });
    expect(text.startsWith("Whisperstream Transcript History - Recovery Key\n")).toBe(true);
    expect(text).toContain(
      "- Use this key to restore your saved transcripts if this Mac loses its\n  encryption key.",
    );
    expect(text).toContain(
      "- If you lose this key and your recovery password, and this Mac loses its\n  encryption key, your saved transcripts can't be recovered.",
    );
    expect(text).toContain(
      "- Anyone who has this key and a copy of your Whisperstream data can read your\n  transcripts, so keep it private.",
    );
    expect(text).not.toContain("this PC");
    expect(text).not.toContain("Reset deletes");
    expect(text).not.toContain("—");
  });

  it("fills this PC on Windows", () => {
    const text = buildRecoveryFileText(KEY, { variant: "history", platform: "windows", date: DATE });
    expect(text).toContain("if this PC loses its");
    expect(text).toContain("and this PC loses its");
    expect(text).not.toContain("this Mac");
  });
});

describe("recovery file names", () => {
  it("uses the locked file names and titles", () => {
    expect(recoveryFileName("history")).toBe("whisperstream-history-recovery-key.txt");
    expect(recoveryFileName("lockdown")).toBe("whisperstream-lockdown-recovery-key.txt");
    expect(recoveryFileTitle("history")).toBe("Whisperstream Transcript History - Recovery Key");
    expect(recoveryFileTitle("lockdown")).toBe("Whisperstream Lockdown Mode - Recovery Key");
  });

  it("saves through saveBlob with the variant file name", async () => {
    saveBlob.mockResolvedValue("saved");
    await expect(downloadRecoveryFile(KEY, { variant: "history" })).resolves.toBe("saved");
    expect(saveBlob).toHaveBeenCalledWith(
      expect.any(Blob),
      "whisperstream-history-recovery-key.txt",
      "txt",
    );
  });

  it("propagates native save failures", async () => {
    saveBlob.mockRejectedValue(new Error("write failed"));
    await expect(downloadRecoveryFile(KEY, { variant: "lockdown" })).rejects.toThrow();
  });
});
