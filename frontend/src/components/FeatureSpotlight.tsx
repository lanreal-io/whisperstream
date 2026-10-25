import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { motion, useReducedMotion } from "framer-motion";
import type { SpotlightOffer } from "../lib/spotlightOffers";

/* ---------------------------------------------------------------------------
   FeatureSpotlight — the reusable feature-spotlight / upgrade-offer overlay.

   A family-A frost-glass dialog (shell tokens lifted verbatim from
   LocalNudgeDialog / EnableLocalConfirmDialog per the "no shared base dialog
   component" decision) with a hero band + breathing frost bloom, an
   announcement eyebrow, a display headline, and one primary CTA with an optional
   "Maybe later". It renders whatever `offer` it is handed and owns no app state.

   When `benchmarkSlot` is provided, the card's BODY is swapped for that slot
   under the SAME backdrop (never a second stacked z-50 backdrop) — used for the
   Cohere benchmark hand-off.
--------------------------------------------------------------------------- */

const dialogShellStyle = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
};

const backdropStyle = {
  background: "var(--backdrop-strong)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
};

const glassPillStyle = {
  background: "var(--surface-modal)",
  border: "1px solid var(--border-hairline-steel)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
};

const gradientPillStyle = {
  background: "var(--accent-fill)",
  boxShadow: "var(--shadow-pearl)",
};

function accentColor(accent: SpotlightOffer["accent"]): string {
  return accent === "gold" ? "var(--color-gold-deep)" : "var(--color-frost-deep)";
}

/** Breathing bloom behind the hero in the offer's accent. Static under reduced
 *  motion. */
function FeatureBloom({ reduce, accent }: { reduce: boolean; accent: SpotlightOffer["accent"] }) {
  return (
    <motion.div
      aria-hidden="true"
      className="absolute inset-x-[-10%] top-[-20%] h-[200%] pointer-events-none"
      style={{
        background:
          accent === "gold"
            ? "radial-gradient(60% 70% at 50% 40%, color-mix(in srgb, var(--color-gold) 26%, transparent), transparent 72%)"
            : "radial-gradient(60% 70% at 50% 40%, color-mix(in srgb, var(--color-frost) 42%, transparent), transparent 72%)",
        filter: "blur(4px)",
      }}
      initial={false}
      animate={reduce ? { opacity: 0.7 } : { opacity: [0.62, 1, 0.62], scale: [0.97, 1.03, 0.97] }}
      transition={reduce ? undefined : { duration: 4.2, repeat: Infinity, ease: "easeInOut" }}
    />
  );
}

export interface FeatureSpotlightProps {
  offer: SpotlightOffer;
  /** Fired when the primary CTA is pressed. */
  onAct: (offer: SpotlightOffer) => void;
  /** Fired on "Maybe later", backdrop click, or Esc. */
  onDismiss: (offer: SpotlightOffer) => void;
  /** When set, replaces the card body (hero + content) with this slot under the
   *  same backdrop — the Cohere benchmark hand-off. */
  benchmarkSlot?: ReactNode;
}

export function FeatureSpotlight({ offer, onAct, onDismiss, benchmarkSlot }: FeatureSpotlightProps) {
  const laterRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const reduce = useReducedMotion() ?? false;
  const inBenchmark = benchmarkSlot != null;
  const required = offer.required === true;
  const ctaRef = useRef<HTMLButtonElement>(null);

  // Esc dismisses; Enter is intentionally NOT bound (read-first, like the other
  // family-A dialogs). Only active in offer mode — the slot owns its own keys.
  useEffect(() => {
    if (inBenchmark) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        // A required offer has no dismissal.
        if (!required) onDismiss(offer);
      } else if (e.key === "Tab") {
        const buttons = dialogRef.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)");
        if (!buttons?.length) return;
        const first = buttons[0];
        const last = buttons[buttons.length - 1];
        if (e.shiftKey && (document.activeElement === first || !dialogRef.current?.contains(document.activeElement))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && (document.activeElement === last || !dialogRef.current?.contains(document.activeElement))) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [inBenchmark, offer, onDismiss, required]);

  // Mount-focus on the safe "Maybe later" button (offer mode only); a required
  // offer has only its CTA.
  useEffect(() => {
    if (inBenchmark) return;
    if (required) ctaRef.current?.focus();
    else laterRef.current?.focus();
  }, [inBenchmark, required]);

  // Capture before mount focus and return to the invoking surface on dismissal.
  const previousFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null);
  useEffect(() => () => {
    if (previousFocus.current?.isConnected) previousFocus.current.focus();
  }, []);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={() => !inBenchmark && !required && onDismiss(offer)}
      />

      <motion.div
        ref={dialogRef}
        initial={reduce ? { opacity: 0 } : { scale: 0.92, opacity: 0, y: 12 }}
        animate={reduce ? { opacity: 1 } : { scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[460px] max-w-[calc(100vw-40px)] overflow-hidden rounded-[var(--radius-card)]"
        style={dialogShellStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby={inBenchmark ? undefined : "spotlight-heading"}
        aria-describedby={inBenchmark ? undefined : "spotlight-body"}
        aria-label={inBenchmark ? "Hardware check" : undefined}
      >
        {inBenchmark ? (
          benchmarkSlot
        ) : (
          <>
            {/* Hero band */}
            <div className="relative flex h-[150px] items-center justify-center overflow-hidden rounded-t-[var(--radius-card)]">
              <div
                aria-hidden="true"
                className="absolute inset-0"
                style={{
                  background:
                    "radial-gradient(120% 120% at 50% 12%, color-mix(in srgb, var(--color-frost-light) 45%, transparent), transparent 70%)",
                }}
              />
              <FeatureBloom reduce={reduce} accent={offer.accent} />
              <div className="relative z-10">{offer.hero}</div>
              <div
                aria-hidden="true"
                className="absolute inset-x-0 bottom-0 h-px"
                style={{
                  background:
                    "linear-gradient(90deg, transparent, var(--border-emphasis) 20%, var(--border-emphasis) 80%, transparent)",
                }}
              />
            </div>

            {/* Content */}
            <div className="px-7 pb-7 pt-6">
              <span
                className="mb-2 inline-block font-display text-[10px] font-semibold uppercase tracking-[0.12em]"
                style={{ color: accentColor(offer.accent) }}
              >
                {offer.eyebrow}
              </span>
              <h2
                id="spotlight-heading"
                className="mb-2 font-display text-[23px] font-light leading-tight text-slate"
                style={{ textWrap: "balance" } as React.CSSProperties}
              >
                {offer.headline}
              </h2>
              <p id="spotlight-body" className={`${offer.note ? "mb-2" : "mb-6"} max-w-[40ch] text-[13.5px] leading-relaxed text-steel`}>{offer.body}</p>
              {offer.note && (
                <p className="mb-6 text-[12px] leading-relaxed text-steel">{offer.note}</p>
              )}

              {/* primary pill LAST in DOM -> right side; row right-aligned (app convention) */}
              <div className="flex items-center justify-end gap-2.5">
                {!required && (
                  <button
                    ref={laterRef}
                    onClick={() => onDismiss(offer)}
                    className="cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium text-slate transition-all duration-200"
                    style={glassPillStyle}
                  >
                    Maybe later
                  </button>
                )}
                <button
                  ref={ctaRef}
                  onClick={() => onAct(offer)}
                  className="cursor-pointer rounded-full px-5 py-2 text-[13px] font-medium text-white transition-all duration-200"
                  style={gradientPillStyle}
                >
                  {offer.cta.label}
                </button>
              </div>
            </div>
          </>
        )}
      </motion.div>
    </div>
  );
}
