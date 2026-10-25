import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { PromptTester } from "./PromptTester";
import { AppIcon, SiteGlyph } from "./RunningAppPicker";
import { ShortcutCapture } from "./ShortcutCapture";
import { SecondaryButton } from "./SecondaryButton";
import { ALL_LANGUAGES, isLanguageSupportedByArtifact, type ActiveSttArtifact } from "../lib/languages";
import { Dropdown } from "./Dropdown";
import { ModeSelector } from "./ModeSelector";
import { ProfileMark, deriveProfileLetter } from "./ProfileMark";
import {
  PROFILE_ICON_KEYS,
  PROFILE_ICON_LABELS,
  isProfileIconKey,
  type ProfileIconKey,
} from "./profileIcons";
import { getBuiltinPrompt } from "../lib/api/enhancement";
import type { Profile } from "../lib/profiles";
import { errorType } from "../lib/logCapture";
import type { AppEvent } from "../lib/socket/events";

/* ---------- Dialog-family tokens ----------
   Copied from EnableLocalConfirmDialog because "the dialog family
   has no shared base, by design" — do NOT DRY these into a shared <Dialog>. The
   editor is a near-full-window centered single-column workspace (round 04),
   portalled to document.body so it escapes the page card's overflow clip. It
   fits the 760x540 window at rest and scrolls internally only when "Try it"
   expands.
   Family B workspace modal — uses --backdrop-strong (40px blur). */
const cardStyle: React.CSSProperties = {
  background: "var(--surface-modal)",
  backdropFilter: "blur(24px)",
  WebkitBackdropFilter: "blur(24px)",
  border: "1px solid var(--border-emphasis)",
  boxShadow: "var(--shadow-glass)",
};

const backdropStyle: React.CSSProperties = {
  background: "var(--backdrop-strong)",
  backdropFilter: "blur(40px)",
  WebkitBackdropFilter: "blur(40px)",
};

// The steel-tinted hairline border shared with SecondaryButton (round 04). Using
// it on the inset fields gives every input the same neutral "structural line" so
// the fields read as distinct against the light card instead of washing out
// behind the old near-white border.
const STEEL_BORDER = "1px solid rgba(107,130,153,0.28)";
// The same steel hairline, dashed: a field that is visibly off (design.md
// "Profile editor Cleanup card").
const STEEL_BORDER_DASHED = "1px dashed rgba(107,130,153,0.28)";

// Inset textarea / input token. Keeps the frost fill but swaps the near-white
// border for the steel hairline + inset top-light so the field separates from
// the card (issue 4/5).
const insetStyle: React.CSSProperties = {
  background: "var(--surface-control)",
  border: STEEL_BORDER,
  boxShadow:
    "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale))), inset 0 2px 4px rgba(0,0,0,0.02)",
};

const XGlyph = () => (
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
    <path d="M3 3l6 6M9 3l-6 6" />
  </svg>
);

const PlusGlyph = () => (
  <svg
    width="12"
    height="12"
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

const LockGlyph = () => (
  <svg
    width="11"
    height="11"
    viewBox="0 0 12 12"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <rect x="2" y="5.5" width="8" height="5.5" rx="1.5" />
    <path d="M4 5.5V3.5a2 2 0 014 0v2" />
  </svg>
);

export type RoutedTarget =
  | { kind: "app"; processName: string; label: string; icon: string | null }
  | { kind: "site"; domain: string; label: string; icon: string | null };

/* ---------- Template prefill (issue 12a) ----------
 * Uses the shared `Dropdown` primitive (now on this branch after the main
 * merge). Options: Blank / Chat / Code. Picking Chat/Code prefills the prompt
 * via getBuiltinPrompt; Blank clears it. The selection is a one-shot prefill,
 * not a persisted field, so the trigger always rests on the "Template"
 * placeholder rather than tracking a value. */
const TEMPLATE_OPTIONS = ["Blank", "Chat", "Code", "Email"] as const;
type TemplateOption = (typeof TEMPLATE_OPTIONS)[number];

/* Registry-owned fixed-prompt modes. Each model supplies its supported subset
 * through `trained_modes` (Stream-1 Mini: Chat + Email) without another
 * model-specific UI branch. */
const CLEANUP_MODE_OPTIONS = [
  { value: "chat", label: "Chat" },
  { value: "code", label: "Code" },
  { value: "email", label: "Email" },
] as const;
type CleanupMode = "chat" | "code" | "email";
const MODE_LABEL: Record<CleanupMode, string> = {
  chat: "Chat",
  code: "Code",
  email: "Email",
};

/* ---------- Profile icon picker (transparent-indicators) ----------
 * A 5x3 radiogroup of exactly 15 cells: cell 1 is the LETTER FALLBACK, cells
 * 2-15 are the curated glyphs in table order. There is no "nothing selected"
 * state by design — a profile with no stored `icon` has the letter cell
 * selected, and that cell renders the live first grapheme of the name draft, so
 * the fallback is visibly a choice rather than an absence.
 *
 * Roving tabindex: exactly one cell is tabbable. Arrows move focus AND select
 * (the native radiogroup convention); Left/Right step one cell, Up/Down step a
 * row (5), Home/End jump to the ends, and every move clamps rather than wraps.
 */
const ICON_GRID_COLUMNS = 5;
const ICON_CELL_COUNT = PROFILE_ICON_KEYS.length + 1;

function ProfileIconPicker({
  name,
  value,
  onChange,
  testId,
}: {
  /** Live profile-name draft; drives the letter cell's glyph and label. */
  name: string;
  /** The stored curated key, if any. Anything unrecognized selects the letter. */
  value: string | undefined;
  onChange: (key: ProfileIconKey | undefined) => void;
  testId?: string;
}) {
  const cellRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const selectedIndex = isProfileIconKey(value)
    ? PROFILE_ICON_KEYS.indexOf(value) + 1
    : 0;
  // Focus is tracked locally so keyboard traversal keeps working while the host
  // persists asynchronously; it re-seeds whenever the stored selection changes.
  const [focusIndex, setFocusIndex] = useState(selectedIndex);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  useEffect(() => setFocusIndex(selectedIndex), [selectedIndex]);

  const letter = deriveProfileLetter(name);
  const labelFor = (index: number) =>
    index === 0
      ? letter
        ? `First letter, ${letter}`
        : "First letter, no name yet"
      : PROFILE_ICON_LABELS[PROFILE_ICON_KEYS[index - 1]];

  // Re-selecting the current cell is a no-op, never a toggle-off: there is no
  // "no icon" state to toggle back to.
  const select = (index: number, close = false) => {
    if (index !== selectedIndex) {
      onChange(index === 0 ? undefined : PROFILE_ICON_KEYS[index - 1]);
    }
    if (close) setIsOpen(false);
  };

  const moveTo = (index: number) => {
    const next = Math.max(0, Math.min(ICON_CELL_COUNT - 1, index));
    setFocusIndex(next);
    cellRefs.current[next]?.focus();
    select(next);
  };

  const indexOfTarget = (target: EventTarget | null) => {
    const found = cellRefs.current.indexOf(target as HTMLButtonElement);
    return found === -1 ? focusIndex : found;
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    const from = indexOfTarget(e.target);
    switch (e.key) {
      case "ArrowRight":
        e.preventDefault();
        moveTo(from + 1);
        break;
      case "ArrowLeft":
        e.preventDefault();
        moveTo(from - 1);
        break;
      case "ArrowDown":
        e.preventDefault();
        moveTo(from + ICON_GRID_COLUMNS);
        break;
      case "ArrowUp":
        e.preventDefault();
        moveTo(from - ICON_GRID_COLUMNS);
        break;
      case "Home":
        e.preventDefault();
        moveTo(0);
        break;
      case "End":
        e.preventDefault();
        moveTo(ICON_CELL_COUNT - 1);
        break;
      case " ":
      case "Enter":
        e.preventDefault();
        setFocusIndex(from);
        select(from, true);
        break;
      default:
        break;
    }
  };

  return (
    <div className="relative">
      <button
        type="button"
        aria-label={`Choose profile icon, current: ${labelFor(selectedIndex)}`}
        aria-expanded={isOpen}
        aria-controls="profile-icon-options"
        onClick={() => setIsOpen((open) => !open)}
        className="w-9 h-9 flex items-center justify-center rounded-xl text-steel hover:text-slate cursor-pointer transition-colors duration-150"
        style={{ background: "var(--surface-control)", border: STEEL_BORDER }}
      >
        <ProfileMark
          testId={testId}
          name={name}
          iconKey={selectedIndex === 0 ? undefined : PROFILE_ICON_KEYS[selectedIndex - 1]}
          size={24}
        />
      </button>
      {isOpen ? (
        <div
          id="profile-icon-options"
          role="radiogroup"
          aria-label="Profile icon"
          onKeyDown={onKeyDown}
          className="absolute left-0 top-full z-20 mt-2 grid gap-2 rounded-[var(--radius-row)] p-2 backdrop-blur-2xl"
          style={{
            gridTemplateColumns: `repeat(${ICON_GRID_COLUMNS}, 40px)`,
            width: 248,
            background: "var(--surface-overlay)",
            border: "1px solid var(--border-emphasis)",
            boxShadow: "var(--shadow-glass)",
          }}
        >
          {Array.from({ length: ICON_CELL_COUNT }, (_, index) => {
            const selected = index === selectedIndex;
            return (
              <button
                key={index === 0 ? "letter" : PROFILE_ICON_KEYS[index - 1]}
                ref={(el) => {
                  cellRefs.current[index] = el;
                }}
                type="button"
                role="radio"
                aria-checked={selected}
                aria-label={labelFor(index)}
                tabIndex={index === focusIndex ? 0 : -1}
                onFocus={() => setFocusIndex(index)}
                onClick={() => select(index, true)}
                onMouseEnter={() => setHoverIndex(index)}
                onMouseLeave={() => setHoverIndex((i) => (i === index ? null : i))}
                className="w-10 h-10 flex items-center justify-center rounded-xl cursor-pointer"
              >
                <ProfileMark
                  name={name}
                  iconKey={index === 0 ? undefined : PROFILE_ICON_KEYS[index - 1]}
                  size={32}
                  appearance={
                    selected
                      ? {
                          // Elevation level 5, the active ModeSelector pill treatment.
                          background: "var(--accent-fill)",
                          border: "1px solid transparent",
                          boxShadow: "none",
                          ink: "#fff",
                        }
                      : {
                          background:
                            hoverIndex === index
                              ? "var(--surface-hover)"
                              : "var(--surface-control)",
                          border: STEEL_BORDER,
                          boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
                          ink:
                            hoverIndex === index
                              ? "var(--color-slate)"
                              : "var(--color-steel)",
                        }
                  }
                />
              </button>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

/**
 * ProfileEditorModal — round 04 single-column rebuild (issues 9, 10/13, 11,
 * 12a, 12b). A near-full-window centered overlay (~720 wide) portalled to
 * document.body. Vertical block order: header (grounded name input + Default
 * toggle + dismiss), "Activated when using" chip row, a bordered Cleanup-prompt
 * card (under a fixed-prompt model via `promptFixedByModel`: a one-line dimmed
 * preview of a custom profile's saved prompt plus guidance and `Change model`,
 * or only the style sentence for a built-in profile, which never reveals its
 * shipped prompt; otherwise custom profiles keep an editable textarea +
 * Template dropdown), a lighter
 * inline "Try it" tester, and a footer (quiet
 * Delete profile bottom-left for custom/non-draft; draft -> Cancel + Create,
 * existing -> Done).
 *
 * Draft model (issue 7): when `mode === "draft"`, nothing has been persisted.
 * The header name + prompt edits stay in local buffers; `onCommit(name, prompt)`
 * fires Create (the host flushes the profile + its pending rules in one config
 * write); Cancel / dismiss / Esc / backdrop call `onClose` which discards. For
 * an existing profile, edits autosave through `onRename` / `onPromptChange` and
 * the footer shows `Done`.
 *
 * The per-profile Hotkey card (issue 14) sits between the prompt card and the
 * footer: a `ShortcutCapture` bound to `hotkey` (capture target `profile:${id}`),
 * a `Clear` button when set, and the "Press to switch to this profile." caption.
 * For a draft the captured hotkey buffers locally (`onHotkeyChange`) and flushes
 * on Create.
 */
export function ProfileEditorModal({
  open,
  profile,
  mode = "existing",
  isDefault,
  isBuiltin,
  promptFixedByModel = false,
  trainedModesByModel = ["chat", "code"],
  fixedModelLabel = "This model",
  onChangeModel,
  focusName,
  testerGate,
  targets,
  hotkey,
  onHotkeyChange,
  globalLanguage = "en",
  sttArtifact,
  onLanguageChange,
  globalInputStyle,
  onInputStyleChange,
  onIconChange,
  onModeChange,
  onSignatureChange,
  send,
  subscribe,
  connected,
  onSetDefault,
  onPromptChange,
  onRename,
  onUnassign,
  onAddRule,
  addRuleDisabled,
  onDelete,
  onCommit,
  onClose,
  reducedMotion,
}: {
  open: boolean;
  /** The profile being edited (or the draft being created). Null = closed. */
  profile: Profile | null;
  /** "draft" defers persistence until Create; "existing" autosaves + Done. */
  mode?: "draft" | "existing";
  isDefault: boolean;
  isBuiltin: boolean;
  /** True when the active enhancement model owns a fixed, trained cleanup
   *  prompt. The Cleanup prompt card then locks: a custom profile shows a
   *  dimmed one-line preview of its saved prompt, a built-in profile shows only
   *  its style. Gemma and cloud providers leave this false; their custom
   *  profiles remain editable, while bundled prompts stay hidden. */
  promptFixedByModel?: boolean;
  /** Registry modes supported by the selected fixed-prompt local model. */
  trainedModesByModel?: CleanupMode[];
  /** Display name of the fixed-prompt model ("Stream-1 Mini") for guidance. */
  fixedModelLabel?: string;
  /** Close the editor and open the local model picker. Omitted = no
   *  `Change model` action (for example while creating a draft profile). */
  onChangeModel?: () => void;
  /** Focus the name field on open (set by the new-draft flow). */
  focusName: boolean;
  testerGate: import("./PromptTester").PromptTesterGate;
  /** App and site targets routed to this profile. Apps first, then sites. */
  targets: RoutedTarget[];
  /** Current manual-switch hotkey for this profile ("" = none). For a draft it
   *  is the draft-local hotkey; for an existing profile it is
   *  `config.profile_hotkeys[id]`. */
  hotkey: string;
  /** Persist (existing) / buffer (draft) the captured hotkey. "" clears it. */
  onHotkeyChange: (keys: string) => void;
  /** The resolved global input_style (e.g. "clipboard" | "keystroke"), threaded
   *  in so the Default pill can display "Default (Clipboard)" etc. live. */
  globalLanguage?: string | null;
  sttArtifact?: ActiveSttArtifact | null;
  onLanguageChange?: (language: string | undefined) => void;
  globalInputStyle: string;
  /** Persist (existing) / buffer (draft) the per-profile input style override.
   *  undefined = clear the key (inherit global); explicit value pins it. */
  onInputStyleChange: (style: "clipboard" | "keystroke" | undefined) => void;
  /** Persist (existing) / buffer (draft) the curated profile icon, mirroring
   *  onInputStyleChange: undefined clears the key (letter fallback), an explicit
   *  key pins it. Never called for a built-in, whose glyph is derived from its
   *  immutable id. */
  onIconChange: (icon: ProfileIconKey | undefined) => void;
  /** Persist (existing) / buffer (draft) the registry-owned cleanup mode.
   *  Only surfaced for a fixed-prompt model and constrained to its advertised
   *  trained modes. */
  onModeChange: (mode: CleanupMode) => void;
  /** Live Email-profile signature change. "" clears the key (no default
   *  sign-off). The field is only rendered for the built-in `email` profile. */
  onSignatureChange: (signature: string) => void;
  /** WS send/subscribe/connected for the Hotkey ShortcutCapture (issue 14). */
  send: (msg: Record<string, unknown>) => boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  connected: boolean;
  /** Set or clear the default. `next` is `true` to make this the default,
   *  `false` to remove default (the host writes default_profile_id=""). */
  onSetDefault: (next: boolean) => void;
  /** Live prompt change (existing profiles autosave; drafts buffer locally and
   *  flush via onCommit). Always called so a draft's local mirror stays in
   *  sync without the host persisting. */
  onPromptChange: (prompt: string) => void;
  /** Live rename (existing profiles autosave; drafts buffer + flush on Create). */
  onRename: (name: string) => void;
  /** Generalized remove: kind="app" removes from power_mode_map (or draft
   *  pending rules), kind="site" from site_map (or draft pending rules). */
  onUnassign: (kind: "app" | "site", key: string) => void;
  /** Open the add-rule flow pre-targeted to this profile / draft. */
  onAddRule: () => void;
  /** Authoritative capability gate for application/site routing actions. */
  addRuleDisabled: boolean;
  onDelete: () => void;
  /** Commit a draft: persist the profile (name, prompt) + its pending rules in
   *  one write. Only called in draft mode (Create). */
  onCommit: (name: string, prompt: string) => void;
  onClose: () => void;
  reducedMotion: boolean;
}) {
  const isDraft = mode === "draft";
  const [nameDraft, setNameDraft] = useState(profile?.name ?? "");
  const [promptDraft, setPromptDraft] = useState(profile?.prompt ?? "");
  const [signatureDraft, setSignatureDraft] = useState(profile?.signature ?? "");
  const [deletePending, setDeletePending] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  // Re-seed local buffers when the open profile changes (the host keys this
  // modal by mounting it with a different profile object / draft id).
  useEffect(() => {
    setNameDraft(profile?.name ?? "");
    setPromptDraft(profile?.prompt ?? "");
    setSignatureDraft(profile?.signature ?? "");
    setDeletePending(false);
  }, [profile?.id, profile?.name, profile?.prompt, profile?.signature]);

  // Focus the name field for the new-draft flow; otherwise leave focus alone.
  useEffect(() => {
    if (open && focusName) {
      nameRef.current?.focus();
      nameRef.current?.select();
    }
  }, [open, focusName]);

  // Esc dismisses (discards a draft; for existing profiles edits already saved).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || !profile) return null;

  // Custom profiles: live prompt edits write through (existing) / buffer (draft).
  const setPrompt = (next: string) => {
    setPromptDraft(next);
    onPromptChange(next);
  };

  // Email signature edits write through for the built-in Email profile.
  const setSignature = (next: string) => {
    setSignatureDraft(next);
    onSignatureChange(next);
  };

  // Template prefill (issue 12a). Blank clears with no confirm. Chat/Code
  // prefill from the bundled prompt; confirm before overwriting a non-empty
  // prompt (locked copy "Replace the current prompt?").
  const pickTemplate = async (option: TemplateOption) => {
    if (option === "Blank") {
      if (promptDraft.trim() && !window.confirm("Replace the current prompt?")) return;
      setPrompt("");
      return;
    }
    if (promptDraft.trim() && !window.confirm("Replace the current prompt?")) return;
    try {
      const mode = option === "Code" ? "code" : option === "Email" ? "email" : "chat";
      const { prompt } = await getBuiltinPrompt(mode);
      setPrompt(prompt);
    } catch (err) {
      console.warn(`[Enhancement] template prefill failed error_type=${errorType(err)}`);
    }
  };

  // Fine-tune cleanup mode (Feature 2). A built-in profile's mode IS its id, so
  // its dropdown is fixed and read-only. A custom profile's mode is its stored
  // `mode` field (the source of truth the backend routes on), defaulting to
  // chat — the same fallback the backend applies when the field is absent, so
  // the dropdown always reflects what will actually run.
  const builtinMode: CleanupMode =
    profile.id === "code" ? "code" : profile.id === "email" ? "email" : "chat";
  const trainedModeOptions = CLEANUP_MODE_OPTIONS.filter((option) =>
    trainedModesByModel.includes(option.value),
  );
  const customMode: CleanupMode = profile.mode ?? "chat";
  const effectiveMode: CleanupMode = isBuiltin ? builtinMode : customMode;
  const effectiveModeSupported = trainedModesByModel.includes(effectiveMode);
  const fixedGuidanceId = `profile-fixed-prompt-guidance-${profile.id}`;
  const builtinModeSupported = trainedModesByModel.includes(builtinMode);

  const commitName = () => {
    if (isDraft) return; // draft buffers locally; flushed on Create
    onRename(nameDraft);
  };

  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.4 }}
        className="absolute inset-0"
        style={backdropStyle}
        onClick={onClose}
      />

      <motion.div
        initial={{ scale: 0.92, opacity: 0, y: 12 }}
        animate={{ scale: 1, opacity: 1, y: 0 }}
        transition={{ type: "spring", stiffness: 400, damping: 30, delay: 0.08 }}
        className="relative w-[720px] max-w-[calc(100vw-40px)] max-h-[calc(100vh-40px)] overflow-y-auto rounded-[var(--radius-card)] p-7"
        style={cardStyle}
        role="dialog"
        aria-modal="true"
        aria-label={isDraft ? "New profile" : `Edit ${profile.name}`}
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute top-4 right-4 w-6 h-6 rounded-full flex items-center justify-center text-steel/60 hover:text-slate transition-colors cursor-pointer"
        >
          <XGlyph />
        </button>

        {/* Header — compact identity fields + Make/Remove default button. */}
        <div className="flex items-start justify-between gap-6 pr-8">
          <div className="flex items-start gap-3 min-w-0">
            <div className="shrink-0">
              <div className="text-[13px] font-medium text-slate mb-1.5">Icon</div>
              {isBuiltin ? (
                <div
                  className="w-9 h-9 flex items-center justify-center"
                  aria-label={`${profile.name} uses its built-in icon`}
                >
                  <ProfileMark
                    testId="profile-mark-preview"
                    profileId={profile.id}
                    name={nameDraft}
                    size={24}
                  />
                </div>
              ) : (
                <ProfileIconPicker
                  testId="profile-mark-preview"
                  name={nameDraft}
                  value={profile.icon}
                  onChange={onIconChange}
                />
              )}
            </div>
            <div className="min-w-0">
              <label
                htmlFor="profile-name-input"
                className="block text-[13px] font-medium text-slate mb-1.5"
              >
                Profile Name
              </label>
              <input
                id="profile-name-input"
                ref={nameRef}
                value={nameDraft}
                onChange={(e) => {
                  setNameDraft(e.target.value);
                  if (isDraft) onRename(e.target.value);
                }}
                onBlur={commitName}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitName();
                    (e.target as HTMLInputElement).blur();
                  } else if (e.key === "Escape") {
                    e.stopPropagation();
                    if (!isDraft) setNameDraft(profile.name);
                    (e.target as HTMLInputElement).blur();
                  }
                }}
                aria-label="Profile Name"
                className="w-[280px] max-w-full h-9 rounded-2xl px-3.5 text-[14px] font-medium text-slate outline-none transition-all duration-200"
                style={insetStyle}
              />
            </div>
          </div>

          {/* Make / Remove default: a link-style toggle whose label tracks state.
              "Make default" sets this profile as the default; "Remove as default"
              clears it (the host writes default_profile_id=""). The label itself
              conveys whether this profile IS the default. */}
          <div className="shrink-0 pt-1.5">
            <button
              type="button"
              onClick={() => onSetDefault(!isDefault)}
              aria-pressed={isDefault}
              className="text-[12px] font-medium text-frost-deep hover:underline cursor-pointer"
            >
              {isDefault ? "Remove as default" : "Make default"}
            </button>
          </div>
        </div>

        {/* Activated when using (issue 12b) — horizontal wrapped chip row. */}
        <div className="mt-5">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[13px] text-slate font-medium">Activated when using</span>
            <SecondaryButton
              onClick={onAddRule}
              disabled={addRuleDisabled}
              className="inline-flex items-center gap-1 h-7 px-2.5 rounded-full text-[12px] font-medium text-steel disabled:opacity-50 disabled:cursor-default"
            >
              <PlusGlyph />
              Add
            </SecondaryButton>
          </div>
          <div className="flex flex-wrap gap-2">
            <AnimatePresence initial={false}>
              {targets.map((t) =>
                t.kind === "app" ? (
                  <motion.span
                    key={`app:${t.processName}`}
                    layout={!reducedMotion}
                    initial={{ opacity: 0, scale: 0.9 }}
                    animate={{ opacity: 1, scale: 1 }}
                    exit={{ opacity: 0, scale: 0.9 }}
                    transition={
                      reducedMotion
                        ? { duration: 0 }
                        : { duration: 0.18, ease: [0.16, 1, 0.3, 1] }
                    }
                    className="inline-flex items-center gap-1.5 h-8 pl-2 pr-1.5 rounded-full"
                    style={{
                      background: "var(--surface-raised)",
                      border: STEEL_BORDER,
                      boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
                    }}
                  >
                    <AppIcon
                      app={{
                        display_name: t.label,
                        process_name: t.processName,
                        icon: t.icon,
                      }}
                    />
                    <span className="text-[12px] text-slate truncate max-w-[160px]">
                      {t.label}
                    </span>
                    <button
                      type="button"
                      onClick={() => onUnassign("app", t.processName)}
                      aria-label={`Remove ${t.label} from ${profile.name}`}
                      className="w-5 h-5 rounded-full flex items-center justify-center text-steel/60 hover:text-rose transition-colors duration-150 cursor-pointer"
                    >
                      <XGlyph />
                    </button>
                  </motion.span>
                ) : (
                  <motion.span
                    key={`site:${t.domain}`}
                    layout={!reducedMotion}
                    initial={{ opacity: 0, scale: 0.9 }}
                    animate={{ opacity: 1, scale: 1 }}
                    exit={{ opacity: 0, scale: 0.9 }}
                    transition={
                      reducedMotion
                        ? { duration: 0 }
                        : { duration: 0.18, ease: [0.16, 1, 0.3, 1] }
                    }
                    className="inline-flex items-center gap-1.5 h-8 pl-2 pr-1.5 rounded-full"
                    style={{
                      background: "var(--surface-raised)",
                      border: STEEL_BORDER,
                      boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
                    }}
                  >
                    <SiteGlyph icon={t.icon} />
                    <span className="text-[12px] text-slate truncate max-w-[160px]">
                      {t.domain}
                    </span>
                    <button
                      type="button"
                      onClick={() => onUnassign("site", t.domain)}
                      aria-label={`Remove ${t.domain} from ${profile.name}`}
                      className="w-5 h-5 rounded-full flex items-center justify-center text-steel/60 hover:text-rose transition-colors duration-150 cursor-pointer"
                    >
                      <XGlyph />
                    </button>
                  </motion.span>
                ),
              )}
            </AnimatePresence>
            {targets.length === 0 && (
              <span className="text-[11px] text-steel/60">No apps or sites yet.</span>
            )}
          </div>
        </div>

        {/* Cleanup prompt — bordered card (issues 9, 12a, 12b). Steel hairline so
            the card reads as a distinct field on the light surface (issue 4). */}
        <div
          className="mt-5 rounded-2xl p-3"
          style={{
            background: "var(--surface-raised)",
            border: STEEL_BORDER,
            boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
          }}
        >
          <div className="flex items-center justify-between gap-3 mb-2">
            <span className="text-[13px] text-slate font-medium">Cleanup prompt</span>
            {/* Fixed-prompt models keep the Chat/Email mode choice. */}
            {promptFixedByModel ? (
              isBuiltin ? (
                builtinModeSupported ? (
                  <Dropdown<string>
                    value={builtinMode}
                    disabled
                    options={trainedModeOptions.map((o) => ({
                      value: o.value,
                      label: o.label,
                    }))}
                    onChange={() => {}}
                    ariaLabel="Cleanup mode"
                    triggerClassName="w-[130px]"
                  />
                ) : null
              ) : (
                <Dropdown<CleanupMode>
                  value={customMode}
                  placeholder={MODE_LABEL[customMode]}
                  options={trainedModeOptions.map((o) => ({
                    value: o.value,
                    label: o.label,
                  }))}
                  onChange={(m) => onModeChange(m)}
                  ariaLabel="Cleanup mode"
                  triggerClassName="w-[130px]"
                />
              )
            ) : isBuiltin ? (
              <span className="inline-flex items-center gap-1 text-[11px] text-steel/70">
                <LockGlyph />
                Built-in
              </span>
            ) : (
              <Dropdown<TemplateOption | "">
                value=""
                placeholder="Template"
                options={TEMPLATE_OPTIONS.map((opt) => ({ value: opt, label: opt }))}
                onChange={(opt) => {
                  if (opt) pickTemplate(opt);
                }}
                ariaLabel="Template"
                triggerClassName="w-[130px]"
              />
            )}
          </div>
          {promptFixedByModel ? (
            !effectiveModeSupported ? (
              // The profile's mode has no trained prompt on this model: its
              // dictation is inserted without cleanup.
              <div className="text-[12px] leading-relaxed text-steel" data-fixed-prompt-unsupported>
                <p className="font-medium text-slate">
                  {`${MODE_LABEL[effectiveMode]} isn't available with ${fixedModelLabel}`}
                </p>
                <div className="mt-1 flex flex-wrap items-center justify-between gap-x-2.5 gap-y-2">
                  <p className="min-w-0 flex-[1_1_260px]">
                    Dictation in this profile is inserted without cleanup. To clean it up, switch to Customizable or a cloud model.
                  </p>
                  {onChangeModel && (
                    <SecondaryButton
                      onClick={onChangeModel}
                      className="shrink-0 px-3 py-[5px] rounded-full text-[12px] font-medium text-slate"
                    >
                      Change model
                    </SecondaryButton>
                  )}
                </div>
              </div>
            ) : (
              <>
                {!isBuiltin && (
                  <div
                    role="textbox"
                    aria-readonly="true"
                    aria-disabled="true"
                    aria-label={`${profile.name} cleanup prompt`}
                    aria-describedby={fixedGuidanceId}
                    title={profile.prompt.trim() ? profile.prompt : undefined}
                    className={`w-full truncate rounded-xl px-3 py-1.5 text-[12px] leading-relaxed cursor-not-allowed ${
                      profile.prompt.trim() ? "text-steel/75" : "text-steel/55"
                    }`}
                    style={{ background: "transparent", border: STEEL_BORDER_DASHED }}
                    data-fixed-prompt-preview
                  >
                    {profile.prompt.trim() ? profile.prompt : "No custom prompt yet"}
                  </div>
                )}
                <div
                  className={`${isBuiltin ? "" : "mt-2.5 "}flex flex-wrap items-center justify-between gap-x-2.5 gap-y-2`}
                >
                  <p
                    id={fixedGuidanceId}
                    className="min-w-0 flex-[1_1_260px] flex items-start gap-1.5 text-[12px] leading-relaxed text-steel"
                  >
                    <span className="mt-[3px] shrink-0 opacity-70">
                      <LockGlyph />
                    </span>
                    <span>
                      {isBuiltin
                        ? `${fixedModelLabel} cleans this profile with its built-in ${MODE_LABEL[builtinMode]} style.`
                        : `${fixedModelLabel} uses its own built-in prompts. To edit this one, switch to Customizable or a cloud model.`}
                    </span>
                  </p>
                  {!isBuiltin && onChangeModel && (
                    <SecondaryButton
                      onClick={onChangeModel}
                      className="shrink-0 px-3 py-[5px] rounded-full text-[12px] font-medium text-slate"
                    >
                      Change model
                    </SecondaryButton>
                  )}
                </div>
              </>
            )
          ) : isBuiltin ? (
            <p className="text-[12px] leading-relaxed text-steel/70 px-0.5 py-1">
              This profile uses Whisperstream's built-in {profile.name} cleanup
              style.
            </p>
          ) : (
            <textarea
              value={promptDraft}
              onChange={(e) => setPrompt(e.target.value)}
              onBlur={() => onPromptChange(promptDraft)}
              rows={4}
              aria-label={`${profile.name} cleanup prompt`}
              className="w-full rounded-xl px-3.5 py-2.5 text-[12px] leading-relaxed font-body text-slate placeholder:text-steel/40 outline-none resize-none transition-all duration-200"
              style={insetStyle}
            />
          )}
        </div>

        {onLanguageChange && (
          <div className="mt-5">
            <div className="flex items-center justify-between gap-4">
              <span className="text-[13px] text-slate font-medium">Dictation language</span>
              <Dropdown
                ariaLabel="Dictation language for this profile"
                value={profile.language ?? ""}
                onChange={(language) => onLanguageChange(language || undefined)}
                options={[
                  {
                    value: "",
                    label: `Default (${ALL_LANGUAGES.find(l => l.code === (globalLanguage || "en"))?.label ?? "English"})`,
                  },
                  ...ALL_LANGUAGES.filter(l =>
                    !sttArtifact || isLanguageSupportedByArtifact(sttArtifact, l.code) || l.code === profile.language,
                  ).map(l => ({
                    value: l.code,
                    label: l.label,
                    disabled: !!sttArtifact && !isLanguageSupportedByArtifact(sttArtifact, l.code),
                  })),
                ]}
                searchable
                triggerClassName="w-[180px]"
                panelWidth={240}
              />
            </div>
            <p className="text-[11px] text-steel/70 mt-1.5">
              {profile.language && sttArtifact && !isLanguageSupportedByArtifact(sttArtifact, profile.language)
                ? "This language is not supported by the selected model. Your default language will be used."
                : "Applies even when enhancement is off. Uses the selected speech model."}
            </p>
          </div>
        )}

        {/* Input Style override (Task 2.4) — three-pill: Default (inherit) | Clipboard | Keystroke.
            Default pill active when profile has no input_style key. Explicit pills pin the value.
            onInputStyleChange(undefined) clears the key (inherits global). */}
        <div className="mt-5">
          <div className="flex items-center justify-between gap-4">
            <span className="text-[13px] text-slate font-medium">Input Style</span>
            {/* "" is the sentinel for "inherit the global style"; the stored
                value for that case is undefined. */}
            <ModeSelector<"" | "clipboard" | "keystroke">
              ariaLabel="Input style for this profile"
              value={(profile?.input_style ?? "") as "" | "clipboard" | "keystroke"}
              onChange={(next) =>
                onInputStyleChange(next === "" ? undefined : next)
              }
              options={[
                {
                  value: "",
                  label: `Default (${globalInputStyle === "keystroke" ? "Keystroke" : "Clipboard"})`,
                  ariaLabel: `Default, currently ${globalInputStyle === "keystroke" ? "Keystroke" : "Clipboard"}`,
                },
                { value: "clipboard", label: "Clipboard" },
                { value: "keystroke", label: "Keystroke" },
              ]}
            />
          </div>
          <div className="text-[11px] text-steel/70 mt-1.5">
            {profile?.input_style === "keystroke"
              ? "Types each character for this app, even with cleanup off. Your clipboard stays untouched."
              : profile?.input_style === "clipboard"
                ? "Pastes into this app, even with cleanup off."
                : "Uses your global Input Style unless you pick one here. Applies with or without cleanup."}
          </div>
        </div>

        {/* Default sign-off is an Email-only feature. A stale signature key on
            another profile stays hidden and cannot be edited from this modal. */}
        {profile.id === "email" && (
          <div className="mt-5">
            <label
              htmlFor="profile-signature"
              className="text-[13px] text-slate font-medium"
            >
              Signature
            </label>
            <textarea
              id="profile-signature"
              value={signatureDraft}
              onChange={(e) => setSignature(e.target.value)}
              rows={3}
              placeholder="e.g. Best, Alex"
              className="mt-1.5 w-full rounded-[var(--radius-input)] px-4 py-2.5 text-[13px] leading-relaxed font-body text-slate placeholder:text-steel/40 outline-none resize-none transition-all duration-200"
              style={insetStyle}
            />
            <div className="text-[11px] text-steel/70 mt-1.5">
              Added to the end when this profile runs and you don't dictate a sign-off. Leave blank for none.
            </div>
          </div>
        )}

        {/* Try it (issue 12b) — lighter inline tester. Runs the CURRENT prompt.
            Hidden when a fixed-prompt model cannot clean this profile's mode. */}
        {!(promptFixedByModel && !effectiveModeSupported) && <div className="mt-3">
          <PromptTester
            key={`tester-${profile.id}`}
            systemPrompt={promptDraft}
            profileId={profile.id}
            gate={testerGate}
            insetStyle={insetStyle}
            reducedMotion={reducedMotion}
            compact
          />
        </div>}

        {/* Hotkey (issue 14) — manual-switch shortcut. ShortcutCapture bound to
            this profile's hotkey (capture target profile:<id>), a Clear button
            when set, and the switch-to caption. Draft buffers locally + flushes
            on Create; existing autosaves through onHotkeyChange. */}
        <div className="mt-5 flex items-center gap-3 flex-wrap">
          <span className="text-[13px] text-slate font-medium">Hotkey</span>
          <ShortcutCapture
            value={hotkey}
            onChange={onHotkeyChange}
            send={send}
            subscribe={subscribe}
            connected={connected}
            captureTarget={`profile:${profile.id}`}
          />
          <span className="text-[12px] text-steel/60">
            Press to switch to this profile.
          </span>
        </div>

        {/* Footer — Delete (custom, non-draft, left) + draft/existing actions (right). */}
        <div className="flex items-center justify-between mt-6">
          <div>
            {!isBuiltin &&
              !isDraft &&
              (deletePending ? (
                <div className="flex items-center gap-1.5">
                  <span className="text-[11px] font-medium" style={{ color: "var(--color-rose)" }}>
                    Delete?
                  </span>
                  <SecondaryButton
                    variant="destructive"
                    onClick={() => {
                      setDeletePending(false);
                      onDelete();
                    }}
                    aria-label={`Confirm delete ${profile.name}`}
                    className="w-6 h-6 rounded-full flex items-center justify-center text-rose"
                  >
                    <svg
                      viewBox="0 0 12 12"
                      width="10"
                      height="10"
                      fill="none"
                      stroke="var(--color-rose)"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      aria-hidden="true"
                    >
                      <path d="M2.5 6.5l2.5 2.5 4.5-5" />
                    </svg>
                  </SecondaryButton>
                  <button
                    type="button"
                    onClick={() => setDeletePending(false)}
                    aria-label="Cancel delete"
                    className="w-6 h-6 rounded-full flex items-center justify-center text-steel hover:text-slate cursor-pointer"
                  >
                    <XGlyph />
                  </button>
                </div>
              ) : (
                <SecondaryButton
                  variant="destructive"
                  onClick={() => setDeletePending(true)}
                  className="h-8 px-3.5 rounded-full text-[12px] font-medium text-rose"
                >
                  Delete profile
                </SecondaryButton>
              ))}
          </div>

          {isDraft ? (
            <div className="flex items-center gap-2.5">
              <SecondaryButton
                onClick={onClose}
                className="px-5 py-2 rounded-full text-[13px] font-medium text-slate"
              >
                Cancel
              </SecondaryButton>
              <button
                type="button"
                onClick={() => onCommit(nameDraft, promptDraft)}
                disabled={!nameDraft.trim()}
                className="px-5 py-2 rounded-full text-[13px] font-medium cursor-pointer disabled:cursor-default transition-all duration-200"
                style={
                  nameDraft.trim()
                    ? {
                        background: "var(--accent-fill)",
                        color: "#fff",
                        boxShadow:
                          "var(--shadow-pearl)",
                      }
                    : {
                        background: "rgba(255,255,255,0.06)",
                        color: "rgba(107,130,153,0.5)",
                        border: "1px solid rgba(255,255,255,0.2)",
                      }
                }
              >
                Create
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={onClose}
              className="px-5 py-2 rounded-full text-[13px] font-medium text-white cursor-pointer transition-all duration-200"
              style={{
                background: "var(--accent-fill)",
                boxShadow: "var(--shadow-pearl)",
              }}
            >
              Done
            </button>
          )}
        </div>
      </motion.div>
    </div>,
    document.body,
  );
}
