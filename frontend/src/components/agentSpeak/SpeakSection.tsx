/**
 * The Agents settings' reserved status line and its status type, shared by
 * the Agents page and the setup steps.
 *
 * This file used to hold `SpeakSection`, the sentence-case section shell the
 * Agents page was built from. The 2026-10-25 owner direction replaced those
 * sections with a master switch panel and described rows in `HudConfigModal`,
 * so only the status line remains.
 */

export type CardStatusTone = "error" | "attention" | "normal";

export interface CardStatus {
  message: string;
  tone: CardStatusTone;
}

/**
 * The reserved status-line slot (design.md's "one reserved status line per
 * card"). Always renders at `min-h-[14px]`, even with no message, so a state
 * change recolors a line instead of reflowing the page — never conditionally
 * unmount this.
 */
export function CardStatusLine({ status, id }: { status?: CardStatus | null; id?: string }) {
  const color =
    status?.tone === "error"
      ? "var(--color-rose)"
      : status?.tone === "attention"
        ? "var(--color-frost-deep)"
        : "var(--color-steel)";
  return (
    <p
      id={id}
      className="min-h-[14px] text-[11px] leading-relaxed"
      style={{ color }}
      role={status?.tone === "error" ? "alert" : "status"}
    >
      {status?.message ?? ""}
    </p>
  );
}
