/**
 * Shared app-routing primitives, extracted verbatim from the (now-removed)
 * Power Mode tab so the unified profile rows on the Enhancement tab and the
 * legacy Power Mode page can render the SAME chip + picker without a second
 * copy.
 *
 *   - `AppChip`        a removable pill naming one routed app (process name ->
 *                      friendly label), with an X that deletes its
 *                      `power_mode_map` entry.
 *   - `RunningAppPicker` the Level-4 frost-glass popover: a search over the
 *                      running-app list (apps already mapped elsewhere are
 *                      hidden) plus a manual `notepad.exe` fallback row.
 *
 * Both write through `power_mode_map` (lowercased process name -> profile id)
 * via the caller's debounced full-config patch — the picker itself owns no
 * persistence, it only surfaces the user's pick via `onPick(processName)`.
 *
 * Copy strings are the locked values from copy-review.md (round 01); the
 * frost tokens match the existing dropdown / dialog family.
 */
import { useState, useRef, useEffect, useMemo } from "react";
import { motion } from "framer-motion";
import type { RunningApp } from "../lib/api/apps";

// ---- inline icons (copied from Dictionary.tsx, no icon library) ----
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

// ---------------- AppIcon ----------------
// Recognition aid in the picker rows AND the profile-row / editor icon stacks
// (round 03): shows the app's real icon (a PNG data-URI from the backend) when
// available, otherwise a frost chip with the app's first initial. The container
// is a Level-3 control surface so an icon-less row still reads as a small frost
// element consistent with the page. Falls back to the glyph if the data-URI
// fails to decode (onError). Accepts a loose `{ display_name, process_name,
// icon }` shape so callers can pass either a full RunningApp or a synthesized
// record (a routed app that isn't currently running has no live RunningApp).
export function AppIcon({
  app,
}: {
  app: { display_name?: string; process_name?: string; icon?: string | null };
}) {
  const [failed, setFailed] = useState(false);
  const showImg = !!app.icon && !failed;
  const initial =
    (app.display_name || app.process_name || "").trim().charAt(0).toUpperCase() || "?";

  return (
    <span
      aria-hidden="true"
      className="shrink-0 flex items-center justify-center w-[18px] h-[18px] rounded-md overflow-hidden"
      style={
        showImg
          ? undefined
          : {
              background: "color-mix(in srgb, var(--color-frost) 14%, transparent)",
              border: "1px solid var(--border-accent)",
            }
      }
    >
      {showImg ? (
        <img
          src={app.icon as string}
          alt=""
          width={18}
          height={18}
          className="w-[18px] h-[18px] object-contain"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="text-[10px] font-medium leading-none text-steel">{initial}</span>
      )}
    </span>
  );
}

// ---------------- SiteGlyph ----------------
// The site counterpart to AppIcon. Round container (rounded-full) distinguishes
// a site rule from a square app icon in the shared 18px stack. When `icon` is
// supplied (a favicon data-URI the backend read LOCALLY from the browser's
// favicon cache — never a network fetch by this component), it renders as a
// rounded `<img>` and falls back to the globe glyph on decode error (same
// onError pattern as AppIcon). When `icon` is absent/null/failed, it renders the
// inline globe exactly as before — existing callers that pass no `icon` are
// visually unchanged. The round-vs-square distinction (rounded-full vs AppIcon's
// rounded-md) IS the entire app-vs-site visual difference (locked design
// decision 3: shape, not color or badge). Never makes a network call: any
// data-URI is passed in by the caller, SiteGlyph itself fetches nothing.
export function SiteGlyph({
  className = "",
  icon,
}: {
  className?: string;
  /** Optional favicon data-URI ("data:image/...;base64,...") for the site.
   *  When set and it decodes, it replaces the globe glyph. Local-cache sourced
   *  upstream; SiteGlyph never fetches. */
  icon?: string | null;
}) {
  const [failed, setFailed] = useState(false);
  const showImg = !!icon && !failed;

  return (
    <span
      aria-hidden="true"
      className={`shrink-0 flex items-center justify-center w-[18px] h-[18px] rounded-full overflow-hidden ${className}`}
      style={{
        background: "color-mix(in srgb, var(--color-frost) 14%, transparent)",
        border: "1px solid var(--border-accent)",
      }}
    >
      {showImg ? (
        <img
          src={icon as string}
          alt=""
          width={18}
          height={18}
          className="w-[18px] h-[18px] object-contain"
          onError={() => setFailed(true)}
        />
      ) : (
        <svg
          width="11" height="11" viewBox="0 0 16 16" fill="none"
          stroke="var(--color-steel)" strokeWidth="1.4"
          strokeLinecap="round" strokeLinejoin="round"
        >
          <circle cx="8" cy="8" r="6" />
          <ellipse cx="8" cy="8" rx="3" ry="6" />
          <path d="M2 8h12" />
        </svg>
      )}
    </span>
  );
}

// ---------------- AppChip ----------------
export function AppChip({
  label,
  profileName,
  onRemove,
  reducedMotion,
}: {
  label: string;
  profileName: string;
  onRemove: () => void;
  reducedMotion: boolean;
}) {
  return (
    <motion.div
      layout={!reducedMotion}
      initial={{ opacity: 0, scale: 0.9 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.9 }}
      transition={
        reducedMotion ? { duration: 0 } : { duration: 0.18, ease: [0.16, 1, 0.3, 1] }
      }
      className="inline-flex items-center gap-1.5 h-7 pl-3 pr-1.5 rounded-full"
      style={{ background: "rgba(255,255,255,0.25)", border: "1px solid rgba(255,255,255,0.45)" }}
      onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.35)")}
      onMouseLeave={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.25)")}
    >
      <span className="text-[12px] text-slate truncate max-w-[140px]">{label}</span>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${label} from ${profileName}`}
        className="w-5 h-5 rounded-full flex items-center justify-center text-steel/60 hover:text-rose transition-colors duration-150 cursor-pointer"
      >
        <XIcon />
      </button>
    </motion.div>
  );
}

// ---------------- RunningAppList (search + filtered list + manual fallback) ----------------
// The reusable guts of the picker — a search field over the running-app list
// (apps already mapped elsewhere are hidden), each row an AppIcon + display-name
// + process-name, plus a trailing manual-entry row. NO chrome of its own (no
// card, no positioning), so it drops into both the legacy `RunningAppPicker`
// popover AND the round-03 `AddAppModal` centered card. Picking calls `onPick`;
// the manual row calls `onPick` with a lowercased typed process name. Whether
// picking also dismisses (popover) or just selects (modal) is the caller's job.
export function RunningAppList({
  runningApps,
  loading,
  mappedProcessNames,
  profileName,
  onPick,
  onManual,
  showManualEntry = true,
  autoFocus = true,
  listMaxHeightClass = "max-h-[200px]",
  selectedProcessName,
}: {
  runningApps: RunningApp[];
  loading: boolean;
  /** Lowercased process names already routed to ANY profile. Hidden from the
   *  list so an app can't be double-assigned; the caller moves an app that's
   *  picked here off its previous profile. */
  mappedProcessNames: Set<string>;
  /** Used only for the manual-entry button's aria-label. */
  profileName: string;
  /** Receives a lowercased process name when a LIST row is clicked. */
  onPick: (processName: string) => void;
  /** Receives the lowercased manual-entry name. Defaults to `onPick` so the
   *  legacy popover (which commits both paths identically) needn't pass it.
   *  The modal passes a distinct handler so a typed name commits immediately
   *  even if it coincides with a running app's process name. */
  onManual?: (processName: string) => void;
  /** Render the type-a-process-name fallback row. The AddAppModal hides it (it
   *  lists every installed app and offers Browse as the single "not in the list"
   *  path); any other caller keeps it by default. */
  showManualEntry?: boolean;
  autoFocus?: boolean;
  listMaxHeightClass?: string;
  /** When set, the matching list row is highlighted as selected (used by the
   *  modal, which selects-then-confirms rather than picking immediately). */
  selectedProcessName?: string | null;
}) {
  const [query, setQuery] = useState("");
  const [manual, setManual] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return runningApps.filter((a) => {
      if (mappedProcessNames.has(a.process_name.toLowerCase())) return false;
      if (!q) return true;
      return (
        a.display_name.toLowerCase().includes(q) || a.process_name.toLowerCase().includes(q)
      );
    });
  }, [runningApps, query, mappedProcessNames]);

  const commitManual = () => {
    const name = manual.trim().toLowerCase();
    if (!name) return;
    (onManual ?? onPick)(name);
  };

  return (
    <>
      <input
        autoFocus={autoFocus}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="Search apps…"
        aria-label="Search apps"
        className="w-full px-3 py-2 text-[13px] text-slate bg-transparent outline-none placeholder:text-steel/40 border-b border-white/40"
      />

      <div className={`${listMaxHeightClass} overflow-y-auto py-1`} aria-live="polite">
        {loading ? (
          <div className="px-3 py-3 text-[12px] text-steel/60 text-center">Loading apps...</div>
        ) : filtered.length === 0 ? (
          <div className="px-3 py-3 text-[12px] text-steel/60 text-center">
            No apps found
          </div>
        ) : (
          filtered.map((a) => {
            const opaqueMacIdentity = a.process_name.startsWith("macos-app:");
            const displayName = a.display_name && !a.display_name.startsWith("macos-app:")
              ? a.display_name
              : opaqueMacIdentity ? "Application" : a.process_name;
            const selected =
              !!selectedProcessName &&
              selectedProcessName.toLowerCase() === a.process_name.toLowerCase();
            return (
              <button
                key={a.process_name}
                type="button"
                onClick={() => onPick(a.process_name.toLowerCase())}
                aria-pressed={selectedProcessName !== undefined ? selected : undefined}
                className="w-full flex items-center gap-2.5 px-3 py-2 text-left text-[13px] text-steel hover:text-slate transition-colors duration-100 cursor-pointer"
                style={selected ? { background: "color-mix(in srgb, var(--color-frost) 18%, transparent)" } : undefined}
                onMouseEnter={(e) => (e.currentTarget.style.background = "color-mix(in srgb, var(--color-frost) 15%, transparent)")}
                onMouseLeave={(e) =>
                  (e.currentTarget.style.background = selected
                    ? "color-mix(in srgb, var(--color-frost) 18%, transparent)"
                    : "transparent")
                }
              >
                <AppIcon app={a} />
                <span className="min-w-0 flex-1">
                  <span className="block text-slate truncate">
                    {displayName}
                  </span>
                  {!opaqueMacIdentity && (
                    <span className="block text-[11px] text-steel/60 truncate">{a.process_name}</span>
                  )}
                </span>
              </button>
            );
          })
        )}
      </div>

      {showManualEntry && (
      <div className="border-t border-white/40 p-2 flex items-center gap-2">
        <input
          value={manual}
          onChange={(e) => setManual(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitManual();
            }
          }}
          placeholder="e.g. notepad.exe"
          aria-label="Type a process name"
          className="flex-1 min-w-0 px-2 h-7 text-[12px] text-slate bg-transparent outline-none placeholder:text-steel/40"
        />
        <button
          type="button"
          onClick={commitManual}
          disabled={!manual.trim()}
          aria-label={`Add app to ${profileName}`}
          className="inline-flex items-center h-7 px-2.5 rounded-full text-[12px] font-medium cursor-pointer disabled:cursor-default shrink-0"
          style={
            manual.trim()
              ? {
                  background: "var(--accent-fill)",
                  color: "#fff",
                  border: "1px solid var(--border-accent)",
                }
              : {
                  background: "rgba(255,255,255,0.06)",
                  color: "rgba(107,130,153,0.5)",
                  border: "1px solid rgba(255,255,255,0.2)",
                }
          }
        >
          Add
        </button>
      </div>
      )}
    </>
  );
}

// ---------------- RunningAppPicker (popover) ----------------
// Legacy popover wrapper around RunningAppList — kept for any anchored-popover
// caller. Owns its own click-outside / Escape dismissal and frost card chrome;
// picking commits AND closes.
export function RunningAppPicker({
  runningApps,
  loading,
  mappedProcessNames,
  profileName,
  onPick,
  onClose,
  positioned = true,
  ignoreRef,
}: {
  runningApps: RunningApp[];
  loading: boolean;
  mappedProcessNames: Set<string>;
  profileName: string;
  onPick: (processName: string) => void;
  onClose: () => void;
  /** When false, the popover drops its own absolute positioning (`absolute
   *  top-full left-0`) so a parent portal/anchor wrapper can place it instead. */
  positioned?: boolean;
  /** Optional trigger element to exempt from click-outside. */
  ignoreRef?: React.RefObject<HTMLElement | null>;
}) {
  const rootRef = useRef<HTMLDivElement>(null);

  // Click-outside + Escape close without committing.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (rootRef.current && rootRef.current.contains(target)) return;
      if (ignoreRef?.current && ignoreRef.current.contains(target)) return;
      onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose, ignoreRef]);

  return (
    <motion.div
      ref={rootRef}
      initial={{ opacity: 0, y: -4, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -4, scale: 0.98 }}
      transition={{ duration: 0.15 }}
      className={
        positioned
          ? "absolute z-30 top-full left-0 mt-2 w-[260px] rounded-2xl overflow-hidden"
          : "w-[260px] rounded-2xl overflow-hidden"
      }
      style={{
        background: "var(--surface-overlay)",
        backdropFilter: "blur(24px)",
        border: "1px solid var(--border-emphasis)",
        boxShadow: "var(--shadow-glass)",
      }}
    >
      <RunningAppList
        runningApps={runningApps}
        loading={loading}
        mappedProcessNames={mappedProcessNames}
        profileName={profileName}
        onPick={(processName) => {
          onPick(processName);
          onClose();
        }}
      />
    </motion.div>
  );
}
