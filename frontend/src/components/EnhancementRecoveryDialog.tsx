import { useEffect, useRef } from "react";
import { PrimaryButton } from "./PrimaryButton";
import { SecondaryButton } from "./SecondaryButton";
import type { MissingEnhancementModel } from "../lib/api/enhancement";

export function EnhancementRecoveryDialog({ model, onDownload, onDismiss }: {
  model: MissingEnhancementModel;
  onDownload: () => void;
  onDismiss: () => void;
}) {
  const card = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    card.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => previous?.focus();
  }, []);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0" onClick={onDismiss}
        style={{ background: "var(--backdrop-strong)", backdropFilter: "blur(40px)" }} />
      <div ref={card} role="dialog" aria-modal="true" aria-labelledby="enhancement-recovery-title"
        aria-describedby="enhancement-recovery-description"
        className="relative w-[420px] max-w-[calc(100vw-32px)] rounded-[var(--radius-card)] p-7"
        style={{ background: "var(--surface-modal)", border: "1px solid var(--border-emphasis)", boxShadow: "var(--shadow-glass)" }}
        onKeyDown={(event) => {
          if (event.key === "Escape") { event.preventDefault(); onDismiss(); }
          if (event.key === "Tab") {
            const buttons = card.current?.querySelectorAll<HTMLButtonElement>("button");
            if (!buttons?.length) return;
            const first = buttons[0], last = buttons[buttons.length - 1];
            if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
            if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
          }
        }}>
        <h2 id="enhancement-recovery-title" className="font-display text-[18px] text-slate text-center mb-3">
          Enhancement model is missing
        </h2>
        <p id="enhancement-recovery-description" className="text-[13px] text-steel text-center leading-relaxed mb-6">
          {model.label} could not be found on this device. Download it again to
          restore local enhancement. You can keep dictating without cleanup.
        </p>
        <div className="flex justify-center gap-2.5">
          <SecondaryButton onClick={onDismiss} className="px-5 py-2 rounded-full text-[13px] text-slate">Not now</SecondaryButton>
          <PrimaryButton onClick={onDownload} className="px-5 py-2 rounded-full text-[13px]">Download again</PrimaryButton>
        </div>
      </div>
    </div>
  );
}
