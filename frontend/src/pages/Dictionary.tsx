import { useState, useEffect, useMemo, useRef } from "react";
import { motion, AnimatePresence, useReducedMotion } from "framer-motion";
import type { AppConfig } from "../lib/api/config";
import { DictionaryImportModal, DictionaryFileIcon } from "../components/DictionaryImportModal";
import { SecondaryButton } from "../components/SecondaryButton";
import { normalizeDictionaryWord, reviewVocabulary, vocabularyImportPatch, type VocabularyFile } from "../lib/dictionaryImport";

// Build-time variant toggle. Change, rebuild, screenshot. See design.md section 6.
const VARIANT: "traffic-light" | "frost" = "traffic-light";

type EntryType = "boost" | "filter" | "replace";

interface DictionaryEntry {
  id: string;
  type: EntryType;
  word: string;
  replacement?: string; // only when type === "replace"
}

interface VariantPalette {
  bg: string;
  border: string;
  icon: string;
  text: string;
}

// WCAG AA: traffic-light text colors are deliberately darker than the raw tokens.
// See design.md section 6 and section 9. Dark re-derivation per glacier-dark.md §9:
// tint alpha 0.18, border 0.40, text/icon = the brightened semantic hue (AA on
// the dark tint). The light theme uses the Glacier-derived token hue with
// darkened text for AA contrast. Expressed with CSS vars so both themes
// resolve at paint time.
const PALETTE: Record<
  "traffic-light" | "frost",
  Record<EntryType, VariantPalette>
> = {
  "traffic-light": {
    // bg/border use the raw semantic hue via CSS color-mix or direct var. Since
    // rgba() in JS can't inline a CSS var, we keep the static rgb triplets from
    // the Light branch (which the token tracks) and supply dark values via
    // per-callsite style overrides in TypeBadge below.
    boost: {
      bg: "color-mix(in srgb, var(--color-mint) 15%, transparent)",
      border: "color-mix(in srgb, var(--color-mint) 38%, transparent)",
      icon: "var(--color-mint)",
      text: "color-mix(in srgb, var(--color-mint) 80%, var(--color-slate))",
    },
    filter: {
      bg: "color-mix(in srgb, var(--color-rose) 15%, transparent)",
      border: "color-mix(in srgb, var(--color-rose) 38%, transparent)",
      icon: "var(--color-rose)",
      text: "color-mix(in srgb, var(--color-rose) 80%, var(--color-slate))",
    },
    replace: {
      bg: "color-mix(in srgb, var(--color-gold) 15%, transparent)",
      border: "color-mix(in srgb, var(--color-gold) 38%, transparent)",
      icon: "var(--color-gold-deep)",
      text: "color-mix(in srgb, var(--color-gold-deep) 80%, var(--color-slate))",
    },
  },
  frost: {
    boost: {
      bg: "color-mix(in srgb, var(--color-frost) 20%, transparent)",
      border: "color-mix(in srgb, var(--color-frost) 40%, transparent)",
      icon: "var(--color-frost-deep)",
      text: "var(--color-slate)",
    },
    filter: {
      bg: "color-mix(in srgb, var(--color-frost) 20%, transparent)",
      border: "color-mix(in srgb, var(--color-frost) 50%, transparent)",
      icon: "var(--color-frost-deep)",
      text: "var(--color-slate)",
    },
    replace: {
      bg: "color-mix(in srgb, var(--color-frost-light) 25%, transparent)",
      border: "color-mix(in srgb, var(--color-frost-light) 60%, transparent)",
      icon: "var(--color-frost-deep)",
      text: "var(--color-slate)",
    },
  },
};

const LABELS: Record<EntryType, string> = {
  boost: "BOOST",
  filter: "FILTER",
  replace: "REPLACE",
};
const BUTTON_LABELS: Record<EntryType, string> = {
  boost: "Boost",
  filter: "Filter",
  replace: "Replace",
};

// ---------- Icons ----------
// All icons are inline SVG. No icon library. Stroke widths per design.md section 5
// (1.5 default, 1.75 for badges/action pills). Decorative icons get aria-hidden="true".

const BoostIcon = ({ size = 12 }: { size?: number }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 14 14"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.75"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M7 3v8M3 7h8" />
  </svg>
);

const FilterIcon = ({ size = 12 }: { size?: number }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 14 14"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.75"
    strokeLinecap="round"
    aria-hidden="true"
  >
    <path d="M3 7h8" />
  </svg>
);

const ReplaceIcon = ({ size = 12 }: { size?: number }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 14 14"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.75"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M3 7h8m0 0l-2.5-2.5M11 7L8.5 9.5" />
  </svg>
);

const CheckIcon = ({ size = 12 }: { size?: number }) => (
  <svg
    width={size}
    height={size}
    viewBox="0 0 14 14"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.75"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M3 7.5l3 3 5-6" />
  </svg>
);

const PencilIcon = () => (
  <svg
    width="12"
    height="12"
    viewBox="0 0 12 12"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M8.5 2.5l1 1-6 6-1.5.5.5-1.5 6-6z" />
  </svg>
);

const XIcon = () => (
  <svg
    width="12"
    height="12"
    viewBox="0 0 12 12"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    aria-hidden="true"
  >
    <line x1="3" y1="3" x2="9" y2="9" />
    <line x1="9" y1="3" x2="3" y2="9" />
  </svg>
);

const ArrowIcon = () => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 14 14"
    fill="none"
    className="shrink-0 text-steel/40"
    aria-hidden="true"
  >
    <path
      d="M3 7h8m0 0l-2.5-2.5M11 7L8.5 9.5"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </svg>
);

// ---------- Badge ----------
function TypeBadge({ type }: { type: EntryType }) {
  const palette = PALETTE[VARIANT][type];
  const Icon =
    type === "boost" ? BoostIcon : type === "filter" ? FilterIcon : ReplaceIcon;
  return (
    <span className="relative group shrink-0">
      <span
        className="inline-flex items-center justify-center w-5 h-5 rounded-full cursor-default"
        style={{
          background: palette.bg,
          border: `1px solid ${palette.border}`,
          color: palette.icon,
        }}
      >
        <Icon size={10} />
      </span>
      <span
        className="pointer-events-none absolute top-full left-1/2 -translate-x-1/2 mt-1.5 rounded-lg px-2 py-1 text-[10px] font-medium text-slate opacity-0 group-hover:opacity-100 transition-opacity duration-150 z-30 whitespace-nowrap"
        style={{
          background: "var(--surface-overlay)",
          border: "1px solid var(--border-emphasis)",
          backdropFilter: "blur(12px)",
          boxShadow: "var(--shadow-glass-sm)",
        }}
      >
        {LABELS[type]}
      </span>
    </span>
  );
}

// ---------- Add-row action pill ----------
function ActionPill({
  type,
  disabled,
  onClick,
  label,
  icon,
}: {
  type: EntryType;
  disabled: boolean;
  onClick: () => void;
  label?: string;
  icon?: React.ReactNode;
}) {
  const palette = PALETTE[VARIANT][type];
  const DefaultIcon =
    type === "boost" ? BoostIcon : type === "filter" ? FilterIcon : ReplaceIcon;
  const steelMuted = "color-mix(in srgb, var(--color-steel) 50%, transparent)";
  const displayLabel = label ?? BUTTON_LABELS[type];
  // Hover: brighten background + border opacity (no movement, matches frost-glass system)
  const hoverBg = palette.bg.replace(/0\.\d+\)$/, "0.22)");
  const hoverBorder = palette.border.replace(/0\.\d+\)$/, "0.55)");
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="inline-flex items-center gap-1 h-8 px-2.5 rounded-full text-[12px] font-medium cursor-pointer disabled:cursor-default shrink-0 transition-all duration-150"
      style={{
        background: disabled ? "var(--surface-control)" : palette.bg,
        border: `1px solid ${disabled ? "var(--border-hairline)" : palette.border}`,
        color: disabled ? steelMuted : palette.text,
      }}
      onMouseEnter={(e) => {
        if (disabled) return;
        e.currentTarget.style.background = hoverBg;
        e.currentTarget.style.borderColor = hoverBorder;
      }}
      onMouseLeave={(e) => {
        if (disabled) return;
        e.currentTarget.style.background = palette.bg;
        e.currentTarget.style.borderColor = palette.border;
      }}
      aria-label={displayLabel}
    >
      <span style={{ color: disabled ? steelMuted : palette.icon }}>
        {icon ?? <DefaultIcon />}
      </span>
      <span>{displayLabel}</span>
    </button>
  );
}

// ---------- Add row with Replace morph ----------
function AddRow({
  onAdd,
  reducedMotion,
}: {
  onAdd: (entry: Omit<DictionaryEntry, "id">) => void;
  reducedMotion: boolean;
}) {
  const [word, setWord] = useState("");
  const [replacement, setReplacement] = useState("");
  const [morphed, setMorphed] = useState(false);
  const canSubmit = word.trim().length > 0;
  const canCommitReplace = canSubmit && replacement.trim().length > 0;

  const commitBoost = () => {
    if (canSubmit) {
      onAdd({ type: "boost", word: word.trim() });
      reset();
    }
  };
  const commitFilter = () => {
    if (canSubmit) {
      onAdd({ type: "filter", word: word.trim() });
      reset();
    }
  };
  const commitReplace = () => {
    if (canCommitReplace) {
      onAdd({
        type: "replace",
        word: word.trim(),
        replacement: replacement.trim(),
      });
      reset();
    }
  };
  const reset = () => {
    setWord("");
    setReplacement("");
    setMorphed(false);
  };
  const handleKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      morphed ? commitReplace() : commitFilter();
    } else if (e.key === "Escape") {
      if (morphed) {
        setMorphed(false);
      } else {
        reset();
      }
    }
  };

  const morphSpring = reducedMotion
    ? { duration: 0 }
    : { type: "spring" as const, stiffness: 380, damping: 28 };

  return (
    <div
      className="flex items-center gap-2 py-1 pl-1 pr-1 rounded-[var(--radius-row)] frost-field-group"
      style={{
        background: "var(--surface-control)",
        border: "1px solid var(--border-hairline)",
      }}
    >
      <input
        value={word}
        onChange={(e) => setWord(e.target.value)}
        onKeyDown={handleKey}
        placeholder="Add a word..."
        aria-label="Dictionary entry word"
        className="flex-1 min-w-0 bg-transparent text-[13px] text-slate border-none outline-none placeholder:text-steel/40 px-3 h-8 mr-4"
      />

      <AnimatePresence initial={false}>
        {morphed && (
          <motion.div
            key="replace-input"
            initial={{ opacity: 0, width: 0 }}
            animate={{ opacity: 1, width: "40%" }}
            exit={{ opacity: 0, width: 0 }}
            transition={morphSpring}
            className="flex items-center gap-2 overflow-hidden"
          >
            <div className="h-5 w-px bg-white/35 shrink-0" />
            <input
              value={replacement}
              onChange={(e) => setReplacement(e.target.value)}
              onKeyDown={handleKey}
              placeholder="Replace with"
              aria-label="Replacement word"
              autoFocus
              className="flex-1 min-w-0 bg-transparent text-[13px] text-slate border-none outline-none placeholder:text-steel/40 px-2 h-8"
            />
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence initial={false}>
        {!morphed && (
          <motion.div
            key="boost-filter"
            initial={{ opacity: 0, width: 0 }}
            animate={{ opacity: 1, width: "auto" }}
            exit={{ opacity: 0, width: 0 }}
            transition={morphSpring}
            className="flex items-center gap-2 overflow-hidden"
          >
            <ActionPill
              type="boost"
              disabled={!canSubmit}
              onClick={commitBoost}
            />
            <ActionPill
              type="filter"
              disabled={!canSubmit}
              onClick={commitFilter}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* Replace pill: morphs into Save while the replacement input is open. */}
      <ActionPill
        type="replace"
        disabled={morphed ? !canCommitReplace : !canSubmit}
        onClick={() => (morphed ? commitReplace() : setMorphed(true))}
        label={morphed ? "Save" : "Replace"}
        icon={morphed ? <CheckIcon /> : <ReplaceIcon />}
      />
    </div>
  );
}

// ---------- Entry row with edit mode ----------
function EntryRow({
  entry,
  onRemove,
  onEdit,
  isEditing,
  onStartEdit,
  onCancelEdit,
  reducedMotion,
}: {
  entry: DictionaryEntry;
  onRemove: () => void;
  onEdit: (next: DictionaryEntry) => void;
  isEditing: boolean;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  reducedMotion: boolean;
}) {
  const [editWord, setEditWord] = useState(entry.word);
  const [editReplacement, setEditReplacement] = useState(
    entry.replacement ?? ""
  );
  const editInputRef = useRef<HTMLInputElement>(null);

  // Sync local edit state when entering edit mode or entry changes
  useEffect(() => {
    if (isEditing) {
      setEditWord(entry.word);
      setEditReplacement(entry.replacement ?? "");
      // Focus the input after state settles
      requestAnimationFrame(() => {
        editInputRef.current?.focus();
      });
    }
  }, [isEditing, entry.word, entry.replacement]);

  const commitEdit = () => {
    const trimmedWord = editWord.trim();
    if (!trimmedWord) return;
    if (entry.type === "replace") {
      const trimmedReplacement = editReplacement.trim();
      if (!trimmedReplacement) return;
      onEdit({ ...entry, word: trimmedWord, replacement: trimmedReplacement });
    } else {
      onEdit({ ...entry, word: trimmedWord });
    }
  };

  const handleEditKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commitEdit();
    } else if (e.key === "Escape") {
      onCancelEdit();
    }
  };

  const rowLabel =
    entry.type === "replace"
      ? `Replace "${entry.word}" with "${entry.replacement ?? ""}"`
      : entry.type === "filter"
        ? `Filter out "${entry.word}"`
        : `Boost "${entry.word}"`;

  const entryAnimation = reducedMotion
    ? { duration: 0 }
    : { duration: 0.25, ease: [0.16, 1, 0.3, 1] as const };

  // Confirm check icon color depends on variant
  const checkColor =
    VARIANT === "traffic-light"
      ? PALETTE["traffic-light"].boost.icon
      : PALETTE.frost.filter.icon;

  return (
    <motion.div
      layout={!reducedMotion}
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      exit={{ opacity: 0, height: 0 }}
      transition={entryAnimation}
      className="group"
      aria-label={rowLabel}
    >
      <div
        className="flex items-center gap-3 h-8 px-4 rounded-[var(--radius-row)] transition-[background-color,box-shadow] duration-200"
        style={{
          background: isEditing
            ? "var(--surface-entry-hover)"
            : "var(--surface-entry)",
          border: "1px solid var(--border-entry)",
        }}
        onMouseEnter={(e) => {
          if (!isEditing) {
            e.currentTarget.style.background = "var(--surface-entry-hover)";
          }
        }}
        onMouseLeave={(e) => {
          if (!isEditing) {
            e.currentTarget.style.background = "var(--surface-entry)";
          }
        }}
      >
        <TypeBadge type={entry.type} />

        {isEditing ? (
          <div className="flex-1 min-w-0 flex items-center gap-2">
            <input
              ref={editInputRef}
              value={editWord}
              onChange={(e) => setEditWord(e.target.value)}
              onKeyDown={handleEditKey}
              className="flex-1 min-w-0 bg-transparent text-[13px] font-medium text-slate border-none outline-none placeholder:text-steel/40"
              aria-label="Edit word"
            />
            {entry.type === "replace" && (
              <>
                <ArrowIcon />
                <input
                  value={editReplacement}
                  onChange={(e) => setEditReplacement(e.target.value)}
                  onKeyDown={handleEditKey}
                  className="flex-1 min-w-0 bg-transparent text-[13px] font-medium text-slate border-none outline-none placeholder:text-steel/40"
                  aria-label="Edit replacement"
                />
              </>
            )}
          </div>
        ) : (
          <div className="flex-1 min-w-0 flex items-center gap-2 text-[13px] font-medium text-slate">
            <span className="truncate">{entry.word}</span>
            {entry.type === "replace" && (
              <>
                <ArrowIcon />
                <span className="truncate">{entry.replacement}</span>
              </>
            )}
          </div>
        )}

        {/* Pencil / Check button */}
        <button
          onClick={() => {
            if (isEditing) {
              commitEdit();
            } else {
              onStartEdit();
            }
          }}
          aria-label={isEditing ? "Save edit" : "Edit entry"}
          className="shrink-0 w-6 h-6 flex items-center justify-center rounded-full opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity duration-200 text-steel hover:text-slate cursor-pointer"
          style={
            isEditing
              ? { opacity: 1, color: checkColor }
              : undefined
          }
        >
          {isEditing ? <CheckIcon /> : <PencilIcon />}
        </button>

        {/* X / Cancel button */}
        <button
          onClick={() => {
            if (isEditing) {
              onCancelEdit();
            } else {
              onRemove();
            }
          }}
          aria-label={isEditing ? "Cancel edit" : "Remove entry"}
          className="shrink-0 w-6 h-6 flex items-center justify-center rounded-full opacity-0 group-hover:opacity-100 focus-visible:opacity-100 transition-opacity duration-200 text-steel hover:text-rose cursor-pointer"
          style={isEditing ? { opacity: 1 } : undefined}
        >
          <XIcon />
        </button>
      </div>
    </motion.div>
  );
}

// ---------- Top-level component ----------
interface DictionaryProps {
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  onImportConfig: (buildPatch: (latest: AppConfig) => Partial<AppConfig>) => Promise<void>;
  onOpenEnhancement?: () => void;
}

export function Dictionary({ config, onConfigChange, onImportConfig, onOpenEnhancement }: DictionaryProps) {
  const reducedMotion = useReducedMotion() ?? false;
  const [editingId, setEditingId] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [visibleCount, setVisibleCount] = useState(100);
  const [recentEntryIds, setRecentEntryIds] = useState<string[]>([]);

  // Build unified entries from the three typed fields. Newest-first ordering
  // comes from the backing arrays' existing order; new additions are prepended.
  const entries = useMemo<DictionaryEntry[]>(() => {
    const result: DictionaryEntry[] = [];

    // Replacements first (newest-first means array order is preserved,
    // and addEntry prepends so newest is index 0)
    for (const [word, replacement] of Object.entries(
      config.word_replacements
    )) {
      result.push({
        id: `replace:${word}->${replacement}`,
        type: "replace",
        word,
        replacement,
      });
    }

    // Filters
    for (const word of config.word_filters) {
      result.push({
        id: `filter:${word}`,
        type: "filter",
        word,
      });
    }

    for (const word of config.word_boosts) {
      result.push({ id: `boost:${word}`, type: "boost", word });
    }

    // Keep freshly added entries visible even when another action type fills
    // the first page. Saved arrays retain their existing ordering semantics.
    const recent = new Map(recentEntryIds.map((id, index) => [id, index]));
    return [...result.filter(entry => recent.has(entry.id)).sort((a, b) => recent.get(a.id)! - recent.get(b.id)!), ...result.filter(entry => !recent.has(entry.id))];
  }, [config.word_boosts, config.word_filters, config.word_replacements, recentEntryIds]);

  // Fresh-install prefill. See design.md section 6 for the exact guard. Fire once.
  const prefillFired = useRef<boolean>(false);
  useEffect(() => {
    if (prefillFired.current) return;
    if (
      config.setup_completed === false &&
      config.word_boosts.length === 0 &&
      config.word_filters.length === 0 &&
      Object.keys(config.word_replacements).length === 0
    ) {
      prefillFired.current = true;
      console.log("[Dictionary] filler filters prefilled reason=fresh_install");
      onConfigChange({
        word_filters: ["um", "er", "uh"],
      });
    }
  }, []);

  const addEntry = (entry: Omit<DictionaryEntry, "id">) => {
    // Commit any in-progress edit before adding
    setEditingId(null);

    if (entry.type === "boost") {
      const word = entry.word.trim();
      if (config.word_boosts.some(saved => normalizeDictionaryWord(saved) === normalizeDictionaryWord(word))) return;
      setRecentEntryIds([`boost:${word}`]);
      onConfigChange({
        word_boosts: [word, ...config.word_boosts],
      });
    } else if (entry.type === "filter") {
      const word = entry.word.toLowerCase();
      if (config.word_filters.includes(word)) return;
      setRecentEntryIds([`filter:${word}`]);
      onConfigChange({
        word_filters: [word, ...config.word_filters],
      });
    } else if (entry.type === "replace" && entry.replacement) {
      const word = entry.word.toLowerCase();
      setRecentEntryIds([`replace:${word}->${entry.replacement}`]);
      // Prepend by rebuilding the object with the new key first
      const newReplacements: Record<string, string> = {
        [word]: entry.replacement,
      };
      for (const [k, v] of Object.entries(config.word_replacements)) {
        if (k !== word) newReplacements[k] = v;
      }
      onConfigChange({ word_replacements: newReplacements });
    }
  };

  const removeEntry = (id: string) => {
    // If we're editing this entry, cancel the edit
    if (editingId === id) setEditingId(null);

    const colonIdx = id.indexOf(":");
    const type = id.substring(0, colonIdx) as EntryType;
    const rest = id.substring(colonIdx + 1);

    if (type === "boost") {
      onConfigChange({
        word_boosts: config.word_boosts.filter((w) => w !== rest),
      });
    } else if (type === "filter") {
      onConfigChange({
        word_filters: config.word_filters.filter((w) => w !== rest),
      });
    } else if (type === "replace") {
      const arrowIdx = rest.indexOf("->");
      const word = rest.substring(0, arrowIdx);
      const newReplacements = { ...config.word_replacements };
      delete newReplacements[word];
      onConfigChange({ word_replacements: newReplacements });
    }
  };

  const editEntry = (next: DictionaryEntry) => {
    // Find the original entry to know what to remove
    const original = entries.find((e) => e.id === next.id);
    if (!original) return;

    if (next.type === "boost") {
      const newWord = next.word.trim();
      const oldWord = original.word;
      if (config.word_boosts.some(word => word !== oldWord && normalizeDictionaryWord(word) === normalizeDictionaryWord(newWord))) return;
      onConfigChange({
        word_boosts: config.word_boosts.map((w) =>
          w === oldWord ? newWord : w
        ),
      });
    } else if (next.type === "filter") {
      const newWord = next.word.toLowerCase();
      const oldWord = original.word;
      onConfigChange({
        word_filters: config.word_filters.map((w) =>
          w === oldWord ? newWord : w
        ),
      });
    } else if (next.type === "replace" && next.replacement) {
      const oldWord = original.word;
      const newWord = next.word.toLowerCase();
      const newReplacements: Record<string, string> = {};
      for (const [k, v] of Object.entries(config.word_replacements)) {
        if (k === oldWord) {
          newReplacements[newWord] = next.replacement;
        } else {
          newReplacements[k] = v;
        }
      }
      onConfigChange({ word_replacements: newReplacements });
    }

    setEditingId(null);
  };

  const handleStartEdit = (entryId: string) => {
    // Only one row editable at a time. Clicking pencil on a second row
    // while the first is editing commits the first row, then enters the second.
    if (editingId && editingId !== entryId) {
      // Auto-commit is handled by the EntryRow's local state; we just
      // switch the editing id. The previous row will revert to display mode.
    }
    setEditingId(entryId);
  };

  const importVocabulary = async (file: VocabularyFile) => {
    const expected = reviewVocabulary(file, config);
    await onImportConfig(latest => {
      const current = reviewVocabulary(file, latest);
      if (JSON.stringify(current.additions) !== JSON.stringify(expected.additions)) {
        throw new Error("Dictionary changed while reviewing import.");
      }
      return vocabularyImportPatch(file, latest);
    });
    setRecentEntryIds(expected.additions.map(row => row.replacement ? `replace:${row.word.toLowerCase()}->${row.replacement}` : `boost:${row.word}`));
    setVisibleCount(100);
    return expected.additions.length;
  };

  return (
    <div className="space-y-4">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30 }}
      >
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-[18px] font-medium text-slate">Dictionary</h2>
          <SecondaryButton onClick={() => setImportOpen(true)} className="rounded-full px-3.5 py-1.5 text-[12px] text-slate inline-flex items-center gap-1.5"><DictionaryFileIcon />Import CSV</SecondaryButton>
        </div>
        <p className="text-[12px] text-steel leading-relaxed mt-1">
          Boost names with AI enhancement, filter out words, or swap one word for another.
        </p>
      </motion.div>
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{
          delay: 0.02,
          type: "spring",
          stiffness: 400,
          damping: 30,
        }}
      >
        <AddRow onAdd={addEntry} reducedMotion={reducedMotion} />
        <p className="text-[11px] text-steel mt-2 px-1">Boost helps AI enhancement choose the right spelling when it fits your dictation.</p>
      </motion.div>
      {config.word_boosts.length > 0 && (!config.enhancement_enabled || !config.enhancement_vocabulary_supported) && <div className="text-[12px] text-steel px-3 py-2.5 rounded-[var(--radius-input)]" style={{ background: "color-mix(in srgb, var(--color-gold) 7%, transparent)", border: "1px solid color-mix(in srgb, var(--color-gold) 25%, transparent)" }}>
        <p>{!config.enhancement_enabled ? "Boost is inactive while AI enhancement is off. You can still save words." : "Your selected enhancement model does not support Boost. Saved words will stay in your dictionary."}</p>
        {onOpenEnhancement && <button onClick={onOpenEnhancement} className="text-[11px] underline mt-1 cursor-pointer">Open enhancement settings</button>}
      </div>}
      {entries.length === 0 && <div className="text-center py-6 text-[12px] text-steel">Your dictionary is empty. Add a word or import a CSV.</div>}
      <div className="space-y-1">
        <AnimatePresence initial={false}>
          {entries.slice(0, visibleCount).map((entry) => (
            <EntryRow
              key={entry.id}
              entry={entry}
              onRemove={() => removeEntry(entry.id)}
              onEdit={editEntry}
              isEditing={editingId === entry.id}
              onStartEdit={() => handleStartEdit(entry.id)}
              onCancelEdit={() => setEditingId(null)}
              reducedMotion={reducedMotion}
            />
          ))}
        </AnimatePresence>
      </div>
      {entries.length > 100 && <div className="flex items-center justify-between gap-3 text-[11px] text-steel">
        <span>Showing {Math.min(visibleCount, entries.length)} of {entries.length} entries.</span>
        {visibleCount < entries.length && <SecondaryButton onClick={() => setVisibleCount(count => count + 100)} className="rounded-full px-3.5 py-1.5 text-[12px] text-slate">Load more</SecondaryButton>}
      </div>}
      {importOpen && <DictionaryImportModal config={config} onClose={() => setImportOpen(false)} onImport={importVocabulary} />}
    </div>
  );
}
