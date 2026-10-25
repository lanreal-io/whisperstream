import { useEffect, useMemo, useState } from "react";
import { motion } from "framer-motion";
import { LanguagePopup } from "./LanguagePopup";
import { ALL_LANGUAGES } from "../../lib/languages";
import logoUrl from "../../assets/logo.png";

interface StepWelcomeProps {
  language: string | null;
  onLanguageChange: (code: string) => void;
  isLanguageAvailable?: (code: string) => boolean;
  unavailableReason?: string;
}

// Step 1 — minimal welcome.
//
// The earlier version of this step rendered a hero pill + an 8-chip curated
// grid + a "More languages" link. The pattern was optimizing for a
// "showcase how many languages we support" message that the actual user
// (single person, first launch, overwhelmingly likely English) doesn't care
// about. Their goal is to confirm English and move on.
//
// New layout:
//   1. Heading
//   2. Tagline
//   3. A single inline element: "Set up in [English ▾]". The chip portion
//      opens LanguagePopup. The chevron telegraphs that the chip is the
//      interaction surface. Defaults to English; updates to whatever the
//      user picks from the popup.
//   4. Continue (lives in the OnboardingOverlay footer, not here)
export function StepWelcome({
  language,
  onLanguageChange,
  isLanguageAvailable,
  unavailableReason,
}: StepWelcomeProps) {
  const [popupOpen, setPopupOpen] = useState(false);

  // Default-select English on mount if no language is set yet. Scope.md
  // calls English the recommended/default choice.
  useEffect(() => {
    if (language == null) {
      onLanguageChange("en");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Resolve the active language label. Defaults to English if nothing is
  // set yet (the effect above will sync state on the next render).
  const activeLabel = useMemo(() => {
    const code = language ?? "en";
    const match = ALL_LANGUAGES.find((l) => l.code === code);
    return match?.label ?? "English";
  }, [language]);

  return (
    <div className="flex flex-col items-center text-center">
      {/* Brand logo — the flowing-X mark used in the sidebar lockup, scaled
          up to anchor the welcome moment. */}
      <motion.img
        src={logoUrl}
        alt=""
        aria-hidden="true"
        draggable={false}
        initial={{ opacity: 0, y: 8, scale: 0.95 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.4, ease: [0.2, 0, 0, 1] }}
        className="h-14 w-auto select-none pointer-events-none mb-6"
        style={{
          filter: "drop-shadow(0 6px 16px rgba(120, 160, 200, 0.22))",
        }}
      />

      <motion.h1
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.32, delay: 0.06, ease: [0.2, 0, 0, 1] }}
        className="font-display text-[34px] font-light tracking-tight text-slate leading-tight"
      >
        Welcome to Whisperstream
      </motion.h1>

      <motion.p
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.32, delay: 0.12, ease: [0.2, 0, 0, 1] }}
        className="mt-4 text-[14px] text-steel max-w-[440px] leading-relaxed"
      >
        Your voice, transcribed locally on your machine.
      </motion.p>

      {/* The whole interaction surface for step 1: prose + clickable chip
          + chevron. Centered below the tagline with generous breathing
          room. The chip uses the white-pearl-pill style that matches the
          sidebar active tab and the ShortcutCapture key tokens — the
          status idiom in this app is white glass with a frost glow, NOT
          a frost gradient (which is reserved for primary CTAs). */}
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.32, delay: 0.18, ease: [0.2, 0, 0, 1] }}
        className="mt-9 inline-flex items-center gap-2.5 text-[14px] text-steel"
      >
        <span>Set up in</span>
        <button
          onClick={() => setPopupOpen(true)}
          className="inline-flex items-center gap-1.5 rounded-full px-4 py-[7px] text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
          style={{
            background: "var(--surface-hover)",
            backdropFilter: "blur(8px)",
            border: "1px solid rgba(255, 255, 255, 0.85)",
            boxShadow:
              "inset 0 1px 0 rgb(255 255 255 / calc(0.95 * var(--rim-scale))), var(--shadow-frost-glow), 0 4px 12px -4px color-mix(in srgb, var(--color-frost) 25%, transparent)",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = "rgba(255, 255, 255, 0.78)";
            e.currentTarget.style.transform = "translateY(-1px)";
            e.currentTarget.style.boxShadow =
              "inset 0 1px 0 rgb(255 255 255 / calc(1 * var(--rim-scale))), var(--shadow-frost-glow), 0 6px 16px -4px color-mix(in srgb, var(--color-frost) 30%, transparent)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "var(--surface-hover)";
            e.currentTarget.style.transform = "translateY(0)";
            e.currentTarget.style.boxShadow =
              "inset 0 1px 0 rgb(255 255 255 / calc(0.95 * var(--rim-scale))), var(--shadow-frost-glow), 0 4px 12px -4px color-mix(in srgb, var(--color-frost) 25%, transparent)";
          }}
          onMouseDown={(e) => {
            e.currentTarget.style.transform = "translateY(0.5px)";
          }}
          onMouseUp={(e) => {
            e.currentTarget.style.transform = "translateY(-1px)";
          }}
        >
          {activeLabel}
          <svg
            width="10"
            height="10"
            viewBox="0 0 12 12"
            fill="none"
            className="text-steel/60 shrink-0"
          >
            <path
              d="M3 4.5L6 7.5L9 4.5"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
      </motion.div>

      <LanguagePopup
        open={popupOpen}
        selectedCode={language}
        onSelect={onLanguageChange}
        onClose={() => setPopupOpen(false)}
        isLanguageAvailable={isLanguageAvailable}
        unavailableReason={unavailableReason}
      />
    </div>
  );
}
