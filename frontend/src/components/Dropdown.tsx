import {
  useState,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  Fragment,
  type FocusEvent as ReactFocusEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { Badge, type BadgeTone } from "./Badge";

export interface DropdownOption<T extends string | number> {
  value: T;
  label: string;
  /** Optional supporting text rendered below the primary label in both the
   *  collapsed trigger and the option row. It remains inside the option's
   *  accessible content and is omitted cleanly for legacy one-line callers. */
  description?: string;
  /** Optional visible and semantic group heading. Contiguous options with the
   *  same label are wrapped in one labelled `role="group"` while keyboard
   *  navigation continues to follow the original flat option order. */
  groupLabel?: string;
  /** Prevent this individual option from being selected. Disabled rows remain
   *  visible for state/context but expose both native and ARIA semantics. */
  disabled?: boolean;
  /** Optional trailing chip rendered via the shared `Badge` primitive (e.g.
   *  a model's "Customizable"/"Lite" descriptor). Shown on both the trigger
   *  (for the current value) and the option row. */
  badge?: { label: string; tone?: BadgeTone };
  /** Keep tradeoff chips inside the open menu. */
  badgeInOptionsOnly?: boolean;
  /** Optional trailing chips for options that carry more than one descriptor
   *  (e.g. a model that is both "Free Tier" and "Recommended"). Rendered wherever
   *  `badge` renders, inline after it. Additive — existing single-`badge`
   *  callers are unaffected. */
  badges?: { label: string; tone?: BadgeTone }[];
  /** Optional trailing, non-interactive state glyph (e.g. a downloaded-state
   *  check/download icon). Rendered `aria-hidden` by the caller; shown on
   *  both the trigger (for the current value) and the option row. */
  adornment?: ReactNode;
  /** Optional LEADING adornment rendered BEFORE the label (e.g. a first-party
   *  brand mark). Distinct from `adornment`, which is the trailing/right-side
   *  state glyph. Shown on both the trigger (for the current value) and the
   *  option row. Callers should mark it `aria-hidden` — meaning must not ride
   *  on it alone. Optional, so existing flat callers are unaffected. */
  leadingAdornment?: ReactNode;
  /** When true, a thin `--divider` hairline is rendered in the panel BEFORE
   *  this option's row (never on the collapsed trigger). Used to set a group
   *  of rows apart within one list (naming round §4: the opt-in "Customizable"
   *  tier below a divider under the recommended cleaners). Display-only —
   *  callers own the option ORDER; this just draws the line. */
  dividerBefore?: boolean;
  /** Screen-reader-only sentence ASSOCIATED with this option row through
   *  `aria-describedby`.
   *
   *  Not the same thing as an `sr-only` span placed beside the row: a visually
   *  separate sibling is announced only if the reader happens to walk the panel
   *  in browse mode, and never when the option itself takes focus, which is how
   *  a keyboard user actually meets it. It must not go INSIDE the option button
   *  either, because content there joins the button's accessible NAME and would
   *  turn a state sentence into part of the row's label. `aria-describedby` is
   *  the one placement that announces it on focus without renaming the option
   *  or presenting it as an action. */
  accessibleDescription?: string;
  /** Render `description` only in the open panel, never on the collapsed
   *  trigger (and leave it out of the trigger's accessible name). Opt-in so
   *  existing two-line triggers are unchanged. */
  descriptionInOptionsOnly?: boolean;
  /** Rich panel-only replacement for `description` (for example a phrase that
   *  carries its own hover). `description` still feeds search; callers must
   *  keep both in sync. Ignored on the collapsed trigger. */
  descriptionContent?: ReactNode;
  /** Replaces `adornment` on the collapsed trigger only (for example a
   *  progress line along the trigger edge). The panel row keeps `adornment`. */
  triggerAdornment?: ReactNode;
  /** Render `badge`/`badges` inline after the label on the row's first line
   *  instead of as a trailing chip group. */
  badgeInline?: boolean;
  /** Still render `renderOptionAction` when this option is disabled (for
   *  example deleting an installed model that cannot be selected). */
  actionWhenDisabled?: boolean;
}

interface DropdownProps<T extends string | number> {
  value: T;
  options: DropdownOption<T>[];
  onChange: (value: T) => void;
  /** Screen-reader label for the trigger. The current selection is appended
   *  automatically (aria-label overrides the visible text), e.g.
   *  "Input device: Headset". */
  ariaLabel?: string;
  /** Associate the trigger with its setting's explanatory text. */
  ariaDescribedBy?: string;
  /** Tailwind classes controlling the trigger button width/shape.
   *  Default: `w-[140px]`. */
  triggerClassName?: string;
  /** Panel width in px. Defaults to the measured trigger width. */
  panelWidth?: number;
  /** Opt-in override for the group heading's COLOR class only. Defaults to
   *  `"text-steel"`, so every existing grouped consumer (e.g. Enhancement's
   *  local-model picker) renders byte-identically. The fixed layout classes
   *  (`px-4 pt-2 pb-1`, 11px, uppercase, `tracking-[0.08em]`) are unaffected.
   *  `VoicePicker` passes `"text-steel/60"` to mute the FEMALE/MALE headings
   *  into captions that read as separators rather than selectable rows. */
  groupHeadingClassName?: string;
  /** Opt-in override for the `dividerBefore` hairline color. Defaults to
   *  `"var(--divider)"`, so every existing consumer (e.g. Enhancement's
   *  Customizable-tier divider) renders byte-identically. `--divider` is white
   *  at low alpha and near-invisible on the `--surface-overlay` panel in Light,
   *  so `VoicePicker` passes the steel-tint HAIRLINE for its FEMALE/MALE split,
   *  which resolves against the panel in both themes. */
  dividerColor?: string;
  /** Optional override for the collapsed trigger's RESTING border color.
   *  Defaults to the themed `--border-hairline-steel`: a white-alpha hairline
   *  over a white `--surface-control` fill composites to no chrome on the
   *  near-white `--surface-modal` in Light, so the trigger read as flat text.
   *  The steel edge matches SecondaryButton and is a faint white edge in dark.
   *  The setup flow's voice picker still passes its own stronger steel tint.
   *  The OPEN state always keeps `--border-accent`. */
  triggerBorderColor?: string;
  /** Optional control shown inside the closed trigger's right edge, before
   *  the chevron (e.g. an "i" that explains the current choice). It renders as
   *  a sibling overlaid on the trigger, never inside the trigger `<button>`
   *  (button-in-button is invalid HTML), and the trigger reserves room for it.
   *  Absent by default, which leaves every other consumer's markup unchanged. */
  triggerAccessory?: ReactNode;
  /** Optional additional cap (px) on the panel's height, applied on top of the
   *  space-to-the-viewport-edge clamp this component already computes. Opt-in
   *  and undefined by default, so every existing consumer keeps the "use all
   *  the room there is" behavior unchanged.
   *
   *  Reach for it when "it technically fits inside the window" is not good
   *  enough: a long list opened from a control low on a dialog can legally fill
   *  every pixel down to the window edge, which reads as the panel escaping its
   *  dialog and being cut by the window rather than as a list that scrolls.
   *  `VoicePicker` is the consumer, and it derives its cap from the panel's own
   *  row metrics so the fold lands mid-row. */
  panelMaxHeight?: number;
  /** Shown on the trigger when `value` matches no option. */
  placeholder?: string;
  /** When true, the trigger is non-interactive and dimmed and the panel
   *  never opens. Used when the dropdown's setting has no effect (e.g.
   *  retention while history is off). */
  disabled?: boolean;
  /** Opt-in per-row action (e.g. download/delete). When provided, each
   *  option row is wrapped `relative` and the returned node is rendered in
   *  an absolutely-positioned right slot OUTSIDE the option `<button>`
   *  (button-in-button is invalid HTML), with its own click handled
   *  separately from row selection (the caller's action element should
   *  stopPropagation so clicking it doesn't also select the option). */
  renderOptionAction?: (opt: DropdownOption<T>) => ReactNode;
  /** Opt-in: let Tab move from an option INTO that row's action instead of
   *  closing the panel, and close only once focus actually leaves the panel.
   *
   *  Default `false` keeps the shared contract every other consumer relies on
   *  (Tab off an option closes the list and hands focus back to the page).
   *  Turn it on when a `renderOptionAction` node is the only way to reach a
   *  real action — an icon-only delete that a keyboard user cannot focus is an
   *  accessibility defect, not a nicety. Only meaningful together with
   *  `renderOptionAction`. */
  optionActionsFocusable?: boolean;
  /** Panel overflow behavior. Default `"auto"` clips + scrolls internally so a
   *  long list never overflows the viewport. Pass `"visible"` ONLY when the
   *  list is short enough to never need scrolling AND an option renders a
   *  hover affordance (e.g. an "i" HoverTooltip) whose bubble must escape the
   *  panel box instead of being clipped by the scroll container — the shared
   *  overflow-y-auto clips BOTH axes (a `visible` overflow-x is coerced to
   *  `auto` when overflow-y is `auto`), so a tooltip taller/wider than the row
   *  gap would otherwise be cut off. With `"visible"` the bubble spills into
   *  the (much larger) viewport; keep the tooltip on `side="auto"` so it still
   *  stays inside the window. Unsafe for long lists (they'd overflow the
   *  window with no scrollbar). */
  panelOverflow?: "auto" | "visible";
  /** Opt-in text filter rendered inside the open panel. Intended for long,
   *  identifier-heavy lists where typeahead is not enough (for example,
   *  hundreds of historical model runs). The selected trigger value continues
   *  to come from the complete option set while the panel rows filter across
   *  label, description, group, and value. */
  searchable?: boolean;
  /** Placeholder for the opt-in panel filter. */
  searchPlaceholder?: string;
  /** Opt-in programmatic open. Each change to a new non-zero value focuses the
   *  trigger and opens the panel (for example another surface routing the user
   *  to this picker). Zero or undefined never opens anything. */
  openRequest?: number;
}

type PanelCoords = {
  left: number;
  width: number;
  maxHeight: number;
  // Exactly one of top/bottom is set: top anchors a downward panel, bottom
  // anchors an upward panel. Anchoring this way means we never need to measure
  // the panel height to flip — the max-height + internal scroll guarantee it
  // can never overflow the chosen direction.
  top?: number;
  bottom?: number;
};

const GAP = 8; // px between trigger and panel
const MARGIN = 8; // px breathing room from the viewport edge
const TYPEAHEAD_RESET_MS = 500;

type IndexedOption<T extends string | number> = {
  option: DropdownOption<T>;
  index: number;
};

type OptionRun<T extends string | number> = {
  groupLabel?: string;
  entries: IndexedOption<T>[];
};

function coalesceOptionRuns<T extends string | number>(
  options: DropdownOption<T>[],
): OptionRun<T>[] {
  const runs: OptionRun<T>[] = [];
  options.forEach((option, index) => {
    const previous = runs[runs.length - 1];
    if (previous && previous.groupLabel === option.groupLabel) {
      previous.entries.push({ option, index });
      return;
    }
    runs.push({ groupLabel: option.groupLabel, entries: [{ option, index }] });
  });
  return runs;
}

/**
 * Shared frost-glass dropdown. Renders its panel through a portal with fixed
 * positioning so it escapes both transformed ancestors (framer-motion) and
 * scroll-container `overflow` clipping. Opens toward whichever side has more
 * room and clamps its height to that space (scrolling internally for long
 * lists), so it never clips against the window or a scroll edge.
 */
export function Dropdown<T extends string | number>({
  value,
  options,
  onChange,
  ariaLabel,
  ariaDescribedBy,
  triggerClassName = "w-[140px]",
  panelWidth,
  groupHeadingClassName = "text-steel",
  dividerColor = "var(--divider)",
  triggerBorderColor = "var(--border-hairline-steel)",
  triggerAccessory,
  panelMaxHeight,
  placeholder,
  disabled = false,
  renderOptionAction,
  optionActionsFocusable = false,
  panelOverflow = "auto",
  searchable = false,
  searchPlaceholder = "Search options",
  openRequest,
}: DropdownProps<T>) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState<PanelCoords | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const typeaheadRef = useRef({ query: "", lastKeyAt: 0 });
  // True between a mousedown and mouseup on the trigger. See `handlePanelBlur`.
  const triggerPressedRef = useRef(false);
  const dropdownId = useId();

  const current = options.find((o) => o.value === value);
  const currentLabel = current?.label ?? placeholder ?? "";
  // Description shown on (and announced by) the collapsed trigger.
  const triggerDescription = current?.descriptionInOptionsOnly
    ? undefined
    : current?.description;
  const normalizedSearch = searchQuery.trim().toLocaleLowerCase();
  const visibleOptions = normalizedSearch
    ? options.filter((option) =>
        [option.label, option.description, option.groupLabel, String(option.value)]
          .filter(Boolean)
          .some((field) => field!.toLocaleLowerCase().includes(normalizedSearch)),
      )
    : options;
  const optionRuns = coalesceOptionRuns(visibleOptions);

  const resetTypeahead = () => {
    typeaheadRef.current = { query: "", lastKeyAt: 0 };
  };

  const closeAndRestoreFocus = () => {
    resetTypeahead();
    setOpen(false);
    triggerRef.current?.focus();
  };

  const enabledIndexes = () =>
    visibleOptions.flatMap((option, index) => (option.disabled ? [] : [index]));

  const focusOption = (index: number) => {
    // Search filtering can replace the visible row at a given flat index in
    // the same render. Query the live panel first so keyboard focus never
    // lands on a detached/stale ref from the previous result set.
    const liveOption = panelRef.current
      ?.querySelectorAll<HTMLButtonElement>('[role="option"]')
      .item(index);
    (liveOption || optionRefs.current[index])?.focus();
  };

  const moveFocus = (fromIndex: number, direction: 1 | -1) => {
    const enabled = enabledIndexes();
    if (enabled.length === 0) return;
    const currentPosition = enabled.indexOf(fromIndex);
    const nextPosition =
      currentPosition === -1
        ? direction === 1
          ? 0
          : enabled.length - 1
        : (currentPosition + direction + enabled.length) % enabled.length;
    focusOption(enabled[nextPosition]);
  };

  const focusEdge = (edge: "first" | "last") => {
    const enabled = enabledIndexes();
    if (enabled.length === 0) return;
    focusOption(edge === "first" ? enabled[0] : enabled[enabled.length - 1]);
  };

  const focusTypeaheadMatch = (fromIndex: number, key: string) => {
    const now = Date.now();
    const previous = typeaheadRef.current;
    const normalizedKey = key.toLocaleLowerCase();
    let query =
      now - previous.lastKeyAt > TYPEAHEAD_RESET_MS
        ? normalizedKey
        : `${previous.query}${normalizedKey}`;

    // Repeated single-character presses cycle through labels that share the
    // same initial instead of producing an impossible query such as "bbb".
    if (query.length > 1 && [...query].every((character) => character === query[0])) {
      query = normalizedKey;
    }
    typeaheadRef.current = { query, lastKeyAt: now };

    for (let offset = 1; offset <= visibleOptions.length; offset += 1) {
      const candidateIndex = (fromIndex + offset) % visibleOptions.length;
      const candidate = visibleOptions[candidateIndex];
      if (
        !candidate.disabled &&
        candidate.label.toLocaleLowerCase().startsWith(query)
      ) {
        focusOption(candidateIndex);
        return;
      }
    }
  };

  const selectOption = (option: DropdownOption<T>) => {
    if (option.disabled) return;
    onChange(option.value);
    closeAndRestoreFocus();
  };

  const handleOptionKeyDown = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
    option: DropdownOption<T>,
    index: number,
  ) => {
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        moveFocus(index, 1);
        return;
      case "ArrowUp":
        event.preventDefault();
        moveFocus(index, -1);
        return;
      case "Home":
        event.preventDefault();
        focusEdge("first");
        return;
      case "End":
        event.preventDefault();
        focusEdge("last");
        return;
      case "Enter":
      case " ":
      case "Spacebar":
        event.preventDefault();
        selectOption(option);
        return;
      case "Escape":
        event.preventDefault();
        event.stopPropagation();
        closeAndRestoreFocus();
        return;
      case "Tab":
        // With row actions focusable, the next tab stop is this row's action,
        // which lives inside the panel — tearing the panel down here would
        // make it permanently unreachable by keyboard. `handlePanelBlur`
        // closes once focus genuinely leaves.
        if (optionActionsFocusable) return;
        resetTypeahead();
        setOpen(false);
        return;
      default:
        if (
          event.key.length === 1 &&
          !event.altKey &&
          !event.ctrlKey &&
          !event.metaKey
        ) {
          event.preventDefault();
          focusTypeaheadMatch(index, event.key);
        }
    }
  };

  // Only used with `optionActionsFocusable`: once Tab is allowed to walk into
  // a row action, something still has to close the panel when focus leaves it
  // altogether (tabbing past the last action, shift-tabbing off the first
  // option, or a row action opening a dialog).
  const handlePanelBlur = (event: ReactFocusEvent<HTMLDivElement>) => {
    // WebKit does not focus a clicked button, so pressing the trigger blurs
    // the panel with no related target. Closing here would let the trigger's
    // click toggle the panel straight back open; leave the close to it.
    if (triggerPressedRef.current) return;
    const next = event.relatedTarget as Node | null;
    if (
      next &&
      (panelRef.current?.contains(next) || triggerRef.current?.contains(next))
    ) {
      return;
    }
    resetTypeahead();
    setOpen(false);
  };

  // If the dropdown becomes disabled while open, force it closed so a stale
  // portal panel can't linger over a dimmed trigger.
  useEffect(() => {
    if (disabled && open) setOpen(false);
  }, [disabled, open]);

  useEffect(() => {
    if (!open && searchQuery) setSearchQuery("");
  }, [open, searchQuery]);

  useEffect(() => {
    if (!openRequest || disabled) return;
    triggerRef.current?.focus();
    resetTypeahead();
    setOpen(true);
    // Only a NEW request opens the panel; `disabled` changes must not.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRequest]);

  // Position the panel relative to the trigger each time it opens. Runs in a
  // layout effect (before paint) so there is no flash at the wrong spot.
  useLayoutEffect(() => {
    if (!open) {
      setCoords(null);
      return;
    }
    const r = triggerRef.current?.getBoundingClientRect();
    if (!r) return;
    const width = panelWidth ?? r.width;
    const spaceBelow = window.innerHeight - r.bottom - MARGIN;
    const spaceAbove = r.top - MARGIN;
    const openDown = spaceBelow >= spaceAbove;
    const left = Math.max(MARGIN, Math.min(r.right - width, window.innerWidth - width - MARGIN));
    // The caller's optional cap only ever makes the panel SHORTER than the
    // room available, so it can never push the panel past the viewport edge.
    const clamp = (available: number) =>
      Math.max(
        0,
        panelMaxHeight === undefined
          ? available
          : Math.min(available, panelMaxHeight),
      );
    if (openDown) {
      setCoords({
        left,
        width,
        top: r.bottom + GAP,
        maxHeight: clamp(spaceBelow - GAP),
      });
    } else {
      setCoords({
        left,
        width,
        bottom: window.innerHeight - r.top + GAP,
        maxHeight: clamp(spaceAbove - GAP),
      });
    }
  }, [open, panelWidth, panelMaxHeight, options.length]);

  // The portal is mounted only after its fixed coordinates are known. Move
  // focus once that mount completes so the selected row is immediately ready
  // for keyboard navigation. A disabled/missing selection falls back to the
  // first enabled row.
  useLayoutEffect(() => {
    if (!open || !coords) return;
    if (searchable) {
      searchRef.current?.focus();
      return;
    }
    const selectedIndex = visibleOptions.findIndex(
      (option) => option.value === value && !option.disabled,
    );
    const fallbackIndex = visibleOptions.findIndex((option) => !option.disabled);
    const targetIndex = selectedIndex === -1 ? fallbackIndex : selectedIndex;
    if (targetIndex !== -1) focusOption(targetIndex);
  }, [open, coords, searchable]);

  // Close on outside click (checking both the trigger and the portal panel),
  // and on scroll outside the portal panel or resize (fixed coords would
  // otherwise go stale), and on Esc.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (triggerRef.current?.contains(t) || panelRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onScroll = (e: Event) => {
      const t = e.target;
      if (t instanceof Node && panelRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onResize = () => setOpen(false);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closeAndRestoreFocus();
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onResize);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const trigger = (
      <button
        ref={triggerRef}
        onMouseDown={() => {
          triggerPressedRef.current = true;
          document.addEventListener(
            "mouseup",
            () => {
              triggerPressedRef.current = false;
            },
            { once: true },
          );
        }}
        onClick={() => {
          if (disabled) return;
          resetTypeahead();
          setOpen((o) => !o);
        }}
        onKeyDown={(event) => {
          if (
            !disabled &&
            !open &&
            (event.key === "ArrowDown" || event.key === "ArrowUp")
          ) {
            event.preventDefault();
            resetTypeahead();
            setOpen(true);
          }
        }}
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-disabled={disabled || undefined}
        aria-describedby={ariaDescribedBy}
        aria-label={
          ariaLabel
            ? `${ariaLabel}: ${currentLabel}${triggerDescription ? `. ${triggerDescription}` : ""}`
            : undefined
        }
        className={`${triggerClassName} px-3 py-1.5 rounded-[var(--radius-input)] text-[13px] text-left text-slate flex items-center justify-between transition-all duration-200 ${
          disabled ? "opacity-40 cursor-not-allowed" : "cursor-pointer"
        }`}
        style={{
          background: "var(--surface-control)",
          border: open
            ? "1px solid var(--border-accent)"
            : `1px solid ${triggerBorderColor}`,
        }}
      >
        <span className="flex flex-1 items-center gap-2 min-w-0">
          {current?.leadingAdornment}
          <span className="min-w-0 flex-1">
            <span
              className={`block ${triggerDescription ? "whitespace-normal leading-tight" : "truncate"}`}
              style={triggerDescription ? { overflowWrap: "anywhere" } : undefined}
            >
              {currentLabel}
            </span>
            {triggerDescription && (
              <span
                className="mt-0.5 block text-[11px] leading-4 font-normal text-steel whitespace-normal"
                style={{ overflowWrap: "anywhere" }}
              >
                {triggerDescription}
              </span>
            )}
          </span>
          {!current?.badgeInOptionsOnly && (current?.badge || (current?.badges?.length ?? 0) > 0) && (
            <span className="inline-flex items-center gap-1">
              {current?.badge && (
                <Badge label={current.badge.label} tone={current.badge.tone} size="sm" />
              )}
              {current?.badges?.map((b, i) => (
                <Badge key={i} label={b.label} tone={b.tone} size="sm" />
              ))}
            </span>
          )}
          {current?.triggerAdornment ?? current?.adornment}
        </span>
        {/* Room for `triggerAccessory`, which is overlaid here, before the
            chevron, from outside the button. */}
        {triggerAccessory && <span aria-hidden="true" className="w-5 shrink-0" />}
        <motion.svg
          animate={{ rotate: open ? 180 : 0 }}
          transition={{ duration: 0.2 }}
          width="12"
          height="12"
          viewBox="0 0 12 12"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          className="text-steel shrink-0 ml-2"
        >
          <path d="M3 4.5l3 3 3-3" />
        </motion.svg>
      </button>
  );

  return (
    <>
      {triggerAccessory ? (
        <span className="relative inline-flex">
          {trigger}
          <span className="absolute right-[34px] top-1/2 z-10 flex -translate-y-1/2 items-center">
            {triggerAccessory}
          </span>
        </span>
      ) : (
        trigger
      )}

      {createPortal(
        <AnimatePresence>
          {open && coords && (
            <motion.div
              ref={panelRef}
              role={searchable ? undefined : "listbox"}
              aria-label={searchable ? undefined : ariaLabel}
              onBlur={searchable || optionActionsFocusable ? handlePanelBlur : undefined}
              initial={{ opacity: 0, y: coords.top !== undefined ? -4 : 4, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: coords.top !== undefined ? -4 : 4, scale: 0.98 }}
              transition={{ duration: 0.15 }}
              className={`fixed z-50 ${
                searchable
                  ? "flex flex-col overflow-hidden"
                  : panelOverflow === "visible"
                    ? "overflow-visible"
                    : "overflow-y-auto overscroll-contain"
              } rounded-[var(--radius-input)] ${searchable ? "" : "py-1.5"}`}
              style={{
                left: coords.left,
                top: coords.top,
                bottom: coords.bottom,
                width: coords.width,
                maxHeight: coords.maxHeight,
                background: "var(--surface-overlay)",
                backdropFilter: "blur(24px)",
                border: "1px solid var(--border-hairline)",
                boxShadow: "var(--shadow-glass)",
              }}
            >
              {searchable && (
                <div className="shrink-0 border-b border-[var(--divider)] p-2">
                  <div
                    className="flex items-center gap-2 rounded-[var(--radius-input)] px-2.5"
                    style={{
                      background: "var(--surface-control)",
                      border: "1px solid var(--border-hairline)",
                    }}
                  >
                    <svg
                      aria-hidden="true"
                      width="13"
                      height="13"
                      viewBox="0 0 16 16"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="1.5"
                      className="shrink-0 text-steel/60"
                    >
                      <circle cx="7" cy="7" r="4.5" />
                      <path d="m10.5 10.5 3 3" strokeLinecap="round" />
                    </svg>
                    <input
                      ref={searchRef}
                      type="text"
                      role="searchbox"
                      value={searchQuery}
                      onChange={(event) => setSearchQuery(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Escape") {
                          event.preventDefault();
                          event.stopPropagation();
                          closeAndRestoreFocus();
                        } else if (event.key === "ArrowDown") {
                          event.preventDefault();
                          focusEdge("first");
                        }
                      }}
                      aria-label={`Filter ${ariaLabel ?? "options"}`}
                      placeholder={searchPlaceholder}
                      className="min-w-0 flex-1 bg-transparent py-2 text-[12px] text-slate outline-none placeholder:text-steel/50"
                    />
                    {searchQuery && (
                      <button
                        type="button"
                        aria-label="Clear search"
                        onClick={() => {
                          setSearchQuery("");
                          searchRef.current?.focus();
                        }}
                        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[14px] text-steel/60 transition-colors hover:bg-[var(--surface-hover)] hover:text-slate"
                      >
                        ×
                      </button>
                    )}
                  </div>
                </div>
              )}
              <div
                role={searchable ? "listbox" : undefined}
                aria-label={searchable ? ariaLabel : undefined}
                className={
                  searchable
                    ? "min-h-0 flex-1 overflow-y-auto overscroll-contain py-1.5"
                    : undefined
                }
              >
              {optionRuns.map((run, runIndex) => {
                const groupHeadingId = `${dropdownId}-group-${runIndex}`;
                const renderedOptions = run.entries.map(({ option: opt, index }) => {
                  const isSelected = opt.value === value;
                  const isDisabled = opt.disabled === true;
                  const action =
                    isDisabled && !opt.actionWhenDisabled
                      ? null
                      : renderOptionAction?.(opt);
                  const hasBadges =
                    !!opt.badge || (opt.badges?.length ?? 0) > 0;
                  const badgeChips = hasBadges ? (
                    <>
                      {opt.badge && (
                        <Badge
                          label={opt.badge.label}
                          tone={opt.badge.tone}
                          size="sm"
                        />
                      )}
                      {opt.badges?.map((b, i) => (
                        <Badge
                          key={i}
                          label={b.label}
                          tone={b.tone}
                          size="sm"
                        />
                      ))}
                    </>
                  ) : null;
                  const descriptionId = opt.accessibleDescription
                    ? `${dropdownId}-desc-${index}`
                    : undefined;
                  return (
                    <Fragment key={String(opt.value)}>
                      {opt.dividerBefore && (
                        <div
                          role="separator"
                          aria-hidden="true"
                          className="mx-3 my-1.5 h-px"
                          style={{ background: dividerColor }}
                        />
                      )}
                      <div
                        className={action ? "group/row relative" : undefined}
                      >
                        <button
                          ref={(node) => {
                            optionRefs.current[index] = node;
                          }}
                          role="option"
                          aria-selected={isSelected}
                          aria-describedby={descriptionId}
                          aria-disabled={isDisabled || undefined}
                          disabled={isDisabled}
                          onClick={() => {
                            selectOption(opt);
                          }}
                          onKeyDown={(event) =>
                            handleOptionKeyDown(event, opt, index)
                          }
                          className={`w-full ${action ? "pr-10" : ""} px-4 py-2 text-[13px] text-left flex items-center gap-2 transition-colors duration-100 ${
                            isDisabled
                              ? "text-steel/60 cursor-not-allowed"
                              : isSelected
                                ? "text-slate font-medium cursor-pointer"
                                : "text-steel hover:text-slate cursor-pointer"
                          }`}
                          style={{
                            transition: "background 100ms",
                            ...(isSelected
                              ? {
                                  background: "var(--surface-raised)",
                                  borderLeft: "3px solid var(--border-accent)",
                                }
                              : {}),
                          }}
                          onMouseEnter={(e) => {
                            if (!isDisabled && !isSelected)
                              e.currentTarget.style.background =
                                "var(--surface-control)";
                          }}
                          onMouseLeave={(e) => {
                            if (!isDisabled && !isSelected)
                              e.currentTarget.style.background = "transparent";
                          }}
                        >
                          {opt.leadingAdornment}
                          <span className="min-w-0 flex-1">
                            <span
                              data-dropdown-option-label
                              className={`block whitespace-normal leading-tight ${
                                isSelected ? "font-medium" : ""
                              }`}
                              style={{ overflowWrap: "anywhere" }}
                            >
                              {opt.label}
                              {opt.badgeInline && badgeChips && (
                                <span className="ml-1.5 inline-flex items-center gap-1 align-[1px]">
                                  {badgeChips}
                                </span>
                              )}
                            </span>
                            {(opt.descriptionContent ?? opt.description) && (
                              <span
                                className={`mt-0.5 block text-[11px] leading-4 font-normal whitespace-normal ${
                                  isDisabled ? "text-steel/60" : "text-steel"
                                }`}
                                style={{ overflowWrap: "anywhere" }}
                              >
                                {opt.descriptionContent ?? opt.description}
                              </span>
                            )}
                          </span>
                          {!opt.badgeInline && badgeChips && (
                            <span className="inline-flex items-center gap-1">
                              {badgeChips}
                            </span>
                          )}
                          {opt.adornment && (
                            <span className="ml-auto shrink-0">
                              {opt.adornment}
                            </span>
                          )}
                        </button>
                        {descriptionId && (
                          // Outside the button so it stays a DESCRIPTION rather
                          // than joining the option's accessible name.
                          <span id={descriptionId} className="sr-only">
                            {opt.accessibleDescription}
                          </span>
                        )}
                        {action && (
                          <div
                            className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {action}
                          </div>
                        )}
                      </div>
                    </Fragment>
                  );
                });

                if (!run.groupLabel) {
                  return (
                    <Fragment key={`ungrouped-${run.entries[0].index}`}>
                      {renderedOptions}
                    </Fragment>
                  );
                }

                return (
                  <div
                    key={`${run.groupLabel}-${run.entries[0].index}`}
                    role="group"
                    aria-labelledby={groupHeadingId}
                  >
                    <div
                      id={groupHeadingId}
                      className={`px-4 pt-2 pb-1 text-[11px] font-medium uppercase tracking-[0.08em] ${groupHeadingClassName}`}
                    >
                      {run.groupLabel}
                    </div>
                    {renderedOptions}
                  </div>
                );
              })}
              {visibleOptions.length === 0 && (
                <div className="px-4 py-5 text-center text-[12px] text-steel/60">
                  No matching options
                </div>
              )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  );
}
