/**
 * Recovery-file helpers shared by Lockdown Mode and standard history recovery.
 *
 * Formats the approved recovery-file text for each variant and drives the two
 * explicit user actions that let a recovery key leave the app: a `.txt` save and
 * a print view. Text is copy-review.md sections 10 (history) and 11 (Lockdown,
 * the vetted body with "code" renamed to "key" and nothing else changed). No em
 * dashes; do not reword without owner legal review.
 *
 * SECURITY: NEVER log the recovery key. No `console.*` here, nothing is sent
 * over the network, and the print path keeps the key in-DOM only (a hidden
 * iframe, not a new-tab URL).
 */

import { saveBlob } from "./saveBlob";

export type RecoveryFileVariant = "history" | "lockdown";
export type RecoveryPlatform = "mac" | "windows";

export interface RecoveryFileOptions {
  variant: RecoveryFileVariant;
  /** Fills "this Mac" / "this PC" in the history text. Ignored for Lockdown. */
  platform?: RecoveryPlatform;
  date?: Date;
}

const FILE_NAMES: Record<RecoveryFileVariant, string> = {
  history: "whisperstream-history-recovery-key.txt",
  lockdown: "whisperstream-lockdown-recovery-key.txt",
};

const TITLES: Record<RecoveryFileVariant, string> = {
  history: "Whisperstream Transcript History - Recovery Key",
  lockdown: "Whisperstream Lockdown Mode - Recovery Key",
};

export function recoveryFileName(variant: RecoveryFileVariant): string {
  return FILE_NAMES[variant];
}

export function recoveryFileTitle(variant: RecoveryFileVariant): string {
  return TITLES[variant];
}

function formatGenerated(date: Date): string {
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function historyBody(key: string, platform: RecoveryPlatform): string[] {
  const place = platform === "windows" ? "this PC" : "this Mac";
  return [
    "RECOVERY KEY:",
    key,
    "",
    "Keep this key somewhere safe and private.",
    "",
    `- Use this key to restore your saved transcripts if ${place} loses its`,
    "  encryption key.",
    "- Anyone who has this key and a copy of your Whisperstream data can read your",
    "  transcripts, so keep it private.",
    "- Do not store it in a synced cloud folder (for example OneDrive, Google Drive, or",
    "  Dropbox) or anywhere other people can reach.",
    `- If you lose this key and your recovery password, and ${place} loses its`,
    "  encryption key, your saved transcripts can't be recovered.",
  ];
}

function lockdownBody(key: string): string[] {
  return [
    "RECOVERY KEY:",
    key,
    "",
    "Keep this key somewhere safe and private.",
    "",
    "- This one-time key is the only way back into your transcripts if you forget your",
    "  password.",
    "- Anyone who has this key can unlock your transcripts, so treat it like a key.",
    "- Do not store it in a synced cloud folder (for example OneDrive, Google Drive, or",
    "  Dropbox) or anywhere other people can reach.",
    "- If you lose both your password and recovery key, your history can't be restored. Reset deletes it instead.",
  ];
}

/** Build the approved recovery-file text with the real key and a human date. */
export function buildRecoveryFileText(
  recoveryKey: string,
  { variant, platform = "mac", date = new Date() }: RecoveryFileOptions,
): string {
  return [
    TITLES[variant],
    "Lanreal Technologies Inc.",
    "",
    ...(variant === "history"
      ? historyBody(recoveryKey, platform)
      : lockdownBody(recoveryKey)),
    "",
    "Lanreal Technologies never receives this key. It is generated and stored on your",
    "device only.",
    "",
    `Generated: ${formatGenerated(date)}`,
    "",
  ].join("\n");
}

/**
 * Save the recovery text as a `.txt` through the shared saveBlob path (native
 * save dialog in the app, an anchor download in the browser). Returns "saved"
 * or "cancelled"; native write failures reject so the caller can show
 * "Couldn't save the file. Try again." NEVER logs the key.
 */
export async function downloadRecoveryFile(
  recoveryKey: string,
  options: RecoveryFileOptions,
): Promise<"saved" | "cancelled"> {
  const text = buildRecoveryFileText(recoveryKey, options);
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  return saveBlob(blob, recoveryFileName(options.variant), "txt");
}

/**
 * Print the recovery text via a self-contained hidden iframe. Isolating the
 * print document keeps app chrome out of the printout and keeps the key
 * in-DOM only. Renders the SAME text as the download.
 */
export function printRecoveryFile(
  recoveryKey: string,
  options: RecoveryFileOptions,
): void {
  const text = buildRecoveryFileText(recoveryKey, options);
  const escaped = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  const iframe = document.createElement("iframe");
  iframe.setAttribute("aria-hidden", "true");
  iframe.style.position = "fixed";
  iframe.style.right = "0";
  iframe.style.bottom = "0";
  iframe.style.width = "0";
  iframe.style.height = "0";
  iframe.style.border = "0";
  document.body.appendChild(iframe);
  const doc = iframe.contentWindow?.document;
  if (!doc) {
    if (iframe.parentNode) iframe.parentNode.removeChild(iframe);
    return;
  }
  doc.open();
  doc.write(
    '<!doctype html><html><head><meta charset="utf-8">' +
      `<title>${TITLES[options.variant]}</title>` +
      "<style>body{margin:0;padding:32px;" +
      "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;" +
      "font-size:12px;line-height:1.5;white-space:pre-wrap;color:#000;}</style>" +
      `</head><body>${escaped}</body></html>`,
  );
  doc.close();
  const win = iframe.contentWindow;
  if (win) {
    win.focus();
    win.print();
  }
  // Remove after the print dialog has had a chance to capture the content.
  setTimeout(() => {
    if (iframe.parentNode) iframe.parentNode.removeChild(iframe);
  }, 1000);
}
