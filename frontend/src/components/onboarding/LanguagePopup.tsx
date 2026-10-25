import { useEffect, useMemo, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  ALL_LANGUAGES,
  CURATED_LANGUAGES,
  EXTENDED_LANGUAGES,
  type Language,
} from "../../lib/languages";

interface LanguagePopupProps {
  open: boolean;
  selectedCode: string | null;
  onSelect: (code: string) => void;
  onClose: () => void;
  isLanguageAvailable?: (code: string) => boolean;
  unavailableReason?: string;
}

// Centered glass modal that sits on top of the onboarding overlay. Used as
// the only language selection surface in the new step 1 flow.
//
// Behavior:
//   - When the search field is empty, the list is segmented:
//       (no header) → 6 CURATED_LANGUAGES in their canonical order
//       "All languages" header → EXTENDED_LANGUAGES alphabetically
//     The English-default user opens the popup, sees English at the top,
//     and immediately knows where to look. We deliberately do not label the
//     top group ("Common", "Popular", etc.) because that implies the rest
//     are second-tier; "All languages" below is descriptive, not a quality
//     judgment.
//   - When the user types in the search field, the segmentation collapses
//     into a single flat alphabetical list of matches across both groups.
//   - The popup opens with the highlight on the currently-selected language
//     so the user's eye lands on their actual pick. (If their selection is
//     in EXTENDED_LANGUAGES, the popup auto-scrolls to bring it into view.)
//   - Click a row → fires onSelect(code) and closes.
//   - Esc or click on the backdrop → closes without changes.
//   - Up/Down arrows move highlight; Enter picks the highlighted row.
//   - Body scroll is preserved (this lives inside an already-fullscreen
//     overlay).

interface PopupRow {
  type: "header" | "lang";
  /** For header rows: section label. For lang rows: undefined. */
  label?: string;
  /** For lang rows: the underlying language. For header rows: undefined. */
  lang?: Language;
  /** For lang rows: index into the navigable (lang-only) list. */
  navIndex?: number;
}

const ALL_LANGUAGES_AVAILABLE = () => true;

export function LanguagePopup({
  open,
  selectedCode,
  onSelect,
  onClose,
  isLanguageAvailable = ALL_LANGUAGES_AVAILABLE,
  unavailableReason = "Not available on this platform.",
}: LanguagePopupProps) {
  const [query, setQuery] = useState("");
  const [highlightIdx, setHighlightIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // Build the rows we'll render. Two modes:
  //   - empty query: segmented (6 curated with no header, then "All
  //     languages" header + 19 extended alphabetical)
  //   - non-empty query: flat alphabetical list of all matches
  // We always also produce a parallel `navRows` array of just the language
  // rows so arrow-key navigation skips headers cleanly.
  const { rows, navRows } = useMemo(() => {
    const q = query.trim().toLowerCase();
    const built: PopupRow[] = [];
    const nav: Language[] = [];

    if (q) {
      const matches = [...ALL_LANGUAGES]
        .filter((lang) => lang.label.toLowerCase().includes(q))
        .sort((a, b) => a.label.localeCompare(b.label));
      matches.forEach((lang) => {
        const available = isLanguageAvailable(lang.code);
        built.push({
          type: "lang",
          lang,
          navIndex: available ? nav.length : undefined,
        });
        if (available) nav.push(lang);
      });
    } else {
      // Curated section, intentionally unlabeled (see header comment).
      CURATED_LANGUAGES.forEach((lang) => {
        const available = isLanguageAvailable(lang.code);
        built.push({
          type: "lang",
          lang,
          navIndex: available ? nav.length : undefined,
        });
        if (available) nav.push(lang);
      });
      // All languages section (alphabetical)
      const sortedExtended = [...EXTENDED_LANGUAGES].sort((a, b) =>
        a.label.localeCompare(b.label),
      );
      built.push({ type: "header", label: "All languages" });
      sortedExtended.forEach((lang) => {
        const available = isLanguageAvailable(lang.code);
        built.push({
          type: "lang",
          lang,
          navIndex: available ? nav.length : undefined,
        });
        if (available) nav.push(lang);
      });
    }

    return { rows: built, navRows: nav };
  }, [query, isLanguageAvailable]);

  // When the popup opens, reset the query and seed the highlight on the
  // currently-selected language. If the selection is missing or unknown,
  // fall back to navRows[0].
  useEffect(() => {
    if (!open) return;
    setQuery("");
    // Compute the initial highlight against the post-reset (segmented) row
    // set rather than waiting for query state to flush. We can re-derive
    // the segmented list here cheaply.
    const segmentedNav: Language[] = [
      ...CURATED_LANGUAGES,
      ...[...EXTENDED_LANGUAGES].sort((a, b) =>
        a.label.localeCompare(b.label),
      ),
    ].filter((language) => isLanguageAvailable(language.code));
    const idx = segmentedNav.findIndex((l) => l.code === selectedCode);
    const initialIdx = idx >= 0 ? idx : 0;
    setHighlightIdx(initialIdx);

    const focusId = requestAnimationFrame(() => {
      inputRef.current?.focus();
    });

    // Scroll the seeded highlight into view AFTER the modal enter animation
    // has settled. The standard scrollIntoView effect (gated on
    // highlightIdx) fires before the row is in the DOM, so it silently
    // no-ops on first open. Defer with setTimeout past the 220ms enter
    // transition.
    const scrollId = window.setTimeout(() => {
      const list = listRef.current;
      if (!list) return;
      const row = list.querySelector<HTMLElement>(
        `[data-nav-idx="${initialIdx}"]`,
      );
      if (row) row.scrollIntoView({ block: "center" });
    }, 280);

    return () => {
      cancelAnimationFrame(focusId);
      window.clearTimeout(scrollId);
    };
  }, [open, selectedCode, isLanguageAvailable]);

  // Reset highlight to the first navigable row when filter changes so the
  // index never points past the end of the filtered list.
  useEffect(() => {
    setHighlightIdx(0);
  }, [query]);

  // Keep the highlighted row scrolled into view as the user arrows through
  // (and on initial open, so a non-curated selection is visible).
  useEffect(() => {
    if (!open) return;
    const list = listRef.current;
    if (!list) return;
    const row = list.querySelector<HTMLElement>(
      `[data-nav-idx="${highlightIdx}"]`,
    );
    if (row) row.scrollIntoView({ block: "nearest" });
  }, [highlightIdx, open]);

  // Esc closes; arrows move highlight; Enter picks. Bound to window so the
  // input keeps focus naturally.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlightIdx((i) => Math.min(i + 1, navRows.length - 1));
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlightIdx((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === "Enter") {
        e.preventDefault();
        const pick = navRows[highlightIdx];
        if (pick) {
          onSelect(pick.code);
          onClose();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, navRows, highlightIdx, onSelect, onClose]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
          className="fixed inset-0 z-30 flex items-center justify-center px-6"
        >
          {/* Backdrop — frosted; click to dismiss */}
          <div
            className="absolute inset-0"
            onClick={onClose}
            style={{
              background: "var(--backdrop-soft)",
              backdropFilter: "blur(8px)",
            }}
          />

          {/* Modal */}
          <motion.div
            initial={{ scale: 0.96, y: 6, opacity: 0 }}
            animate={{ scale: 1, y: 0, opacity: 1 }}
            exit={{ scale: 0.98, y: 4, opacity: 0 }}
            transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
            className="relative w-full max-w-[400px] rounded-[var(--radius-card)] flex flex-col"
            style={{
              maxHeight: "min(460px, 80vh)",
              // Bumped to overlay depth so the modal feels as substantial
              // as the rest of the app's layered surfaces.
              background: "var(--surface-overlay)",
              backdropFilter: "blur(24px)",
              border: "1px solid var(--border-emphasis)",
              boxShadow: "var(--shadow-glass)",
            }}
          >
            {/* Header / search */}
            <div className="px-5 pt-5 pb-3 shrink-0">
              <div className="text-[10px] tracking-[0.2em] uppercase text-steel/50 font-medium mb-2">
                Pick a language
              </div>
              <input
                ref={inputRef}
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search languages…"
                className="w-full rounded-full px-4 py-2 text-[13px] text-slate outline-none placeholder:text-steel/40 transition-all duration-300"
                style={{
                  // Recessed input style (PillInput recipe) — see the
                  // textarea in StepTest for the same idiom.
                  background: "var(--surface-control)",
                  border: "1px solid var(--border-emphasis)",
                  boxShadow: "inset 0 2px 4px rgba(0, 0, 0, 0.02)",
                }}
                onFocus={(e) => {
                  // Option 2: focus = global outline only; no fill change on click.
                  e.currentTarget.style.outline = "";
                }}
                onBlur={(e) => {
                  e.currentTarget.style.background = "var(--surface-control)";
                  e.currentTarget.style.borderColor = "var(--border-emphasis)";
                  e.currentTarget.style.boxShadow =
                    "inset 0 2px 4px rgba(0, 0, 0, 0.02)";
                }}
              />
            </div>

            {/* List */}
            <div
              ref={listRef}
              className="flex-1 overflow-y-auto px-2 pb-3"
              style={{ minHeight: 0 }}
            >
              {rows.length === 0 ? (
                <div className="px-3 py-6 text-center text-[12px] text-steel/60 italic">
                  No matches.
                </div>
              ) : (
                rows.map((row, rowIdx) => {
                  if (row.type === "header") {
                    return (
                      <div
                        key={`header-${row.label}-${rowIdx}`}
                        className="px-3 pt-3 pb-1 text-[10px] tracking-[0.18em] uppercase text-steel/45 font-medium"
                      >
                        {row.label}
                      </div>
                    );
                  }
                  const lang = row.lang!;
                  const navIdx = row.navIndex;
                  const isAvailable = navIdx !== undefined;
                  const isSelected = lang.code === selectedCode;
                  const isHighlighted = navIdx === highlightIdx;
                  return (
                    <button
                      key={lang.code}
                      data-nav-idx={navIdx}
                      disabled={!isAvailable}
                      aria-disabled={!isAvailable || undefined}
                      aria-description={!isAvailable ? unavailableReason : undefined}
                      onClick={() => {
                        if (!isAvailable) return;
                        onSelect(lang.code);
                        onClose();
                      }}
                      onMouseEnter={() => {
                        if (isAvailable) setHighlightIdx(navIdx);
                      }}
                      className={`w-full text-left px-3 py-2 rounded-2xl text-[13px] flex items-center gap-2.5 transition-colors duration-100 ${
                        isAvailable ? "cursor-pointer" : "cursor-not-allowed opacity-40"
                      }`}
                      style={{
                        background: isHighlighted
                          ? "var(--surface-hover)"
                          : "transparent",
                        color: isSelected
                          ? "var(--color-slate)"
                          : "var(--color-steel)",
                        fontWeight: isSelected ? 500 : 400,
                      }}
                    >
                      <span
                        className="w-1.5 h-1.5 rounded-full shrink-0"
                        style={{
                          background: isSelected
                            ? "var(--accent-fill)"
                            : "transparent",
                          boxShadow: isSelected
                            ? "var(--shadow-frost-glow)"
                            : "none",
                        }}
                      />
                      {lang.label}
                    </button>
                  );
                })
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
