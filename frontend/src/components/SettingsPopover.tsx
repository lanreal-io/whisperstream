import { useState, useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { Dropdown } from "./Dropdown";
import { Toggle } from "./Toggle";
import { HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE } from "../lib/api/lockdown";

/** Retention windows. Values are days; 0 = Forever (never purge). Order and
 *  labels are LOCKED copy (copy-review.md). Exported so the Lockdown setup
 *  modal reuses the SAME locked option set (do not fork a parallel picker). */
export const RETENTION_OPTIONS = [
  { label: "1 day", value: 1 },
  { label: "7 days", value: 7 },
  { label: "30 days", value: 30 },
  { label: "Forever", value: 0 },
] as const;

interface SettingsPopoverProps {
  /** "Save transcripts" master toggle (config.history_enabled). */
  historyEnabled: boolean;
  historyDisabled?: boolean;
  onHistoryChange: (value: boolean) => void;
  /** "Save audio" sub-toggle (config.save_audio). Dims when history is off. */
  saveAudio: boolean;
  onSaveAudioChange: (value: boolean) => void;
  /** Retention window in days (config.transcript_retention_days; 0 = Forever).
   *  Dims when history is off. */
  retentionDays: number;
  /** Called with the chosen days. The caller decides whether the change shrinks
   *  the window (and must gate it behind the retention-shrink confirm dialog). */
  onRetentionChange: (days: number) => void;
  /** "Delete all transcripts" — caller opens the clear-all confirm dialog. */
  onClearAll: () => void;
  /** Opens standard history's recovery settings. Present only when a
   *  confirmed recovery key exists and Lockdown is off. */
  onRecoverySettings?: () => void;
}

type PanelCoords = {
  /** Right edge of the panel measured from the viewport's right edge, so the
   *  panel is right-aligned under the gear and never clips the window edge. */
  right: number;
  top: number;
  maxHeight: number;
};

const GAP = 8; // px between trigger and panel
const MARGIN = 8; // px breathing room from the viewport edge
const PANEL_WIDTH = 256; // w-64

/** Single setting row: left label, right control. Dims the label when the row's
 *  setting has no effect (history off). */
function Row({
  label,
  disabled = false,
  children,
}: {
  label: string;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span
        className={`text-[13px] text-slate transition-opacity duration-150 ${
          disabled ? "opacity-40" : ""
        }`}
      >
        {label}
      </span>
      {children}
    </div>
  );
}

// A proper toothed cog (not radial spokes, which read as a sun/dark-mode
// switch). Lucide "settings" gear silhouette: a hollow ring of trapezoidal
// teeth around a center circle. viewBox is 24-based to carry the path coords;
// width/height stay 16 so it sits with the sibling icons.
function GearIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

/**
 * Anchored frost-glass settings popover opened by a gear icon button. Holds the
 * four Transcripts settings (Save transcripts, Save audio, retention, Delete
 * all). Renders the panel through a portal with fixed positioning (right-aligned
 * under the gear so it never clips the right window edge) and reuses Dropdown's
 * outside-click / scroll / resize / Esc close behavior.
 */
export function SettingsPopover({
  historyEnabled,
  historyDisabled = false,
  onHistoryChange,
  saveAudio,
  onSaveAudioChange,
  retentionDays,
  onRetentionChange,
  onClearAll,
  onRecoverySettings,
}: SettingsPopoverProps) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState<PanelCoords | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  // Dependent settings (Save audio + retention) dim when history is off.
  const settingsDisabled = historyDisabled || !historyEnabled;

  // Position the panel right-aligned under the gear each time it opens. Layout
  // effect runs before paint so there is no flash at the wrong spot.
  useLayoutEffect(() => {
    if (!open) {
      setCoords(null);
      return;
    }
    const r = triggerRef.current?.getBoundingClientRect();
    if (!r) return;
    // Right-align the panel's right edge with the gear's right edge, clamped so
    // it never crosses the left margin on a narrow window.
    const rightFromEdge = Math.max(MARGIN, window.innerWidth - r.right);
    const clampedRight = Math.min(rightFromEdge, window.innerWidth - PANEL_WIDTH - MARGIN);
    const top = r.bottom + GAP;
    const maxHeight = window.innerHeight - top - MARGIN;
    setCoords({ right: Math.max(MARGIN, clampedRight), top, maxHeight });
  }, [open]);

  // Close on outside click (trigger or panel), scroll/resize (fixed coords go
  // stale), and Esc. Same pattern as Dropdown. A click on a portal-rendered
  // Dropdown panel inside this popover is NOT inside panelRef, so guard by also
  // ignoring clicks that land on any open listbox.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (triggerRef.current?.contains(t) || panelRef.current?.contains(t)) return;
      // The retention Dropdown renders its options through its own portal; a
      // click there must not close this popover.
      const el = t as HTMLElement;
      if (el.closest?.('[role="listbox"]')) return;
      setOpen(false);
    };
    const onScrollOrResize = () => setOpen(false);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <>
      <button
        ref={triggerRef}
        onClick={() => setOpen((o) => !o)}
        aria-label="Transcript settings"
        aria-haspopup="menu"
        aria-expanded={open}
        className="w-9 h-9 shrink-0 flex items-center justify-center rounded-full text-steel hover:text-slate cursor-pointer transition-colors duration-150"
        style={{
          // Option C (round 04), matching SecondaryButton; the open state
          // uses the accent border. Inline (not the primitive) because this
          // button owns a ref + an open-state border.
          background: "var(--surface-control)",
          border: open
            ? "1px solid var(--border-accent)"
            : "1px solid var(--border-hairline)",
          boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
        }}
      >
        <GearIcon />
      </button>

      {createPortal(
        <AnimatePresence>
          {open && coords && (
            <motion.div
              ref={panelRef}
              role="menu"
              aria-label="Transcript settings"
              initial={{ opacity: 0, y: -4, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -4, scale: 0.98 }}
              transition={{ duration: 0.15 }}
              className="fixed z-50 rounded-[var(--radius-card)] p-3 space-y-2.5 overflow-y-auto"
              style={{
                right: coords.right,
                top: coords.top,
                width: PANEL_WIDTH,
                maxHeight: coords.maxHeight,
                background: "var(--surface-overlay)",
                backdropFilter: "blur(24px)",
                WebkitBackdropFilter: "blur(24px)",
                border: "1px solid var(--border-hairline)",
                boxShadow: "var(--shadow-glass)",
              }}
            >
              {historyDisabled && (
                <p id="history-policy-note" className="text-[12px] text-steel leading-relaxed">
                  {HISTORY_DISABLED_BY_ORGANIZATION_MESSAGE}
                </p>
              )}
              <Row label="Save transcripts" disabled={historyDisabled}>
                <Toggle checked={!historyDisabled && historyEnabled} onChange={onHistoryChange} disabled={historyDisabled} ariaLabel="Save transcripts" ariaDescribedBy={historyDisabled ? "history-policy-note" : undefined} />
              </Row>

              <Row label="Save audio" disabled={settingsDisabled}>
                <Toggle
                  checked={!historyDisabled && saveAudio}
                  ariaLabel="Save audio"
                  ariaDescribedBy={historyDisabled ? "history-policy-note" : undefined}
                  onChange={onSaveAudioChange}
                  disabled={settingsDisabled}
                />
              </Row>

              <Row label="Keep for" disabled={settingsDisabled}>
                <Dropdown
                  value={retentionDays}
                  options={RETENTION_OPTIONS.map((o) => ({ ...o }))}
                  onChange={(days) => {
                    // Close the popover before handing off, so a retention-shrink
                    // confirm dialog opens unobscured by the panel.
                    setOpen(false);
                    onRetentionChange(days);
                  }}
                  ariaLabel="Keep transcripts for"
                  triggerClassName="w-[110px]"
                  disabled={settingsDisabled}
                />
              </Row>

              {onRecoverySettings && (
                <button
                  role="menuitem"
                  onClick={() => {
                    // Close first so the dialog opens unobscured by the panel.
                    setOpen(false);
                    onRecoverySettings();
                  }}
                  className="w-full flex items-center justify-between text-left text-[13px] text-slate px-1 py-1 cursor-pointer transition-colors duration-150 hover:text-frost-deep"
                >
                  Recovery settings
                  <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="text-steel/60" aria-hidden="true">
                    <path d="M4.5 3l3 3-3 3" />
                  </svg>
                </button>
              )}

              <div
                className="h-px my-1"
                style={{ background: "var(--divider)" }}
              />

              {/* Destructive, pinned at the bottom below the divider. */}
              <button
                onClick={() => {
                  onClearAll();
                  setOpen(false);
                }}
                className="w-full text-left text-[12px] text-steel hover:text-rose transition-colors duration-150 px-1 py-1 cursor-pointer"
              >
                Delete all transcripts
              </button>
            </motion.div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  );
}
