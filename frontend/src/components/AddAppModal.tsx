import { useEffect, useMemo, useState, useId } from "react";
import { createPortal } from "react-dom";
import { motion } from "framer-motion";
import { RunningAppList, AppIcon, SiteGlyph } from "./RunningAppPicker";
import { normalizeDomain } from "../lib/normalizeDomain";
import { resolveAppByPath } from "../lib/api/apps";
import type { RunningApp } from "../lib/api/apps";
import type { Profile } from "../lib/profiles";
import type { AdvancedCapabilityDecision } from "../lib/advancedCapabilities";
import { errorType } from "../lib/logCapture";

/* ---------- Dialog-family tokens ----------
   Copied from EnableLocalConfirmDialog because "the dialog family
   has no shared base, by design" — do NOT DRY these into a shared <Dialog>. The
   modal is a full-window centered overlay (round 03), portalled to document.body
   so it escapes the page card's overflow clip.
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

// The inner running-app list gets a solid frost surface so its rows read against
// the translucent card (the legacy popover used the same white/95 + border).
const listSurfaceStyle: React.CSSProperties = {
  background: "var(--surface-hover)",
  border: "1px solid var(--border-standard)",
};

// Steel hairline chip (round 04) — matches the editor's "Activated when using"
// chips so a pending rule reads as the same chip it becomes once committed.
const pendingChipStyle: React.CSSProperties = {
  background: "rgba(255,255,255,0.35)",
  border: "1px solid rgba(107,130,153,0.28)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
};

// Move-confirmation banner (round-04 follow-up). A prominent inline card (NOT a
// nested portal dialog) shown when the user picks a key routed to another
// profile. Gold-tinted so it reads as an "are you sure" prompt against the frost
// card, using the dialog family's surface idiom (white frost + hairline border).
const confirmCardStyle: React.CSSProperties = {
  background: "var(--surface-caution)",
  border: "1px solid rgba(214, 178, 94, 0.45)",
  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale))), 0 8px 20px -8px rgba(214,178,94,0.3)",
};

/** A rule the user has staged but not yet committed. */
type Pending = {
  kind: "app" | "site";
  /** Lowercased process name OR normalized domain — the committed key. */
  key: string;
  /** Display label (app friendly name or domain). */
  label: string;
  /** App icon data-URI when known; sites have none. */
  icon: string | null;
  /** Full exe path for an app rule (running pick / installed pick / Browse), so
   *  it can be recorded into power_mode_paths on commit. null for sites or an
   *  app whose path we couldn't determine. */
  path?: string | null;
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
    <line x1="3" y1="3" x2="9" y2="9" />
    <line x1="9" y1="3" x2="3" y2="9" />
  </svg>
);

const FolderGlyph = () => (
  <svg
    width="13"
    height="13"
    viewBox="0 0 14 14"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.4"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M1.5 3.5a1 1 0 011-1h3l1.5 1.5h4a1 1 0 011 1v6a1 1 0 01-1 1h-9a1 1 0 01-1-1z" />
  </svg>
);

/**
 * AddAppModal — the round-03/04 "Add a rule" experience. A centered overlay over
 * the whole 760x540 window, always opened from one profile's editor so the target
 * profile is unambiguous (round 04 dropped the "Route to:" chooser).
 *
 * The user accumulates a PENDING LIST of mixed app + site rules and commits them
 * all at once. In App mode, clicking a running-app row (or typing a manual process
 * name) ADDS the app to the pending list; picked apps then disappear from the list.
 * In Website mode, the domain input + Enter (or the inline Add) appends a normalized
 * domain. The footer Add button commits every pending rule to `initialProfileId` in
 * one batch via `onAdd(rules, profileId)`.
 *
 * The segmented App|Website toggle uses role="radiogroup" + role="radio" +
 * aria-checked (a mutually exclusive mode choice, not a tablist).
 */
export function AddAppModal({
  open,
  profiles,
  initialProfileId,
  runningApps,
  installedApps = [],
  appCapability,
  websiteCapability,
  pathBrowseCapability,
  loading,
  mappedProcessNames,
  otherProfileName,
  onAdd,
  onClose,
}: {
  open: boolean;
  /** Used only to label the running-app list (the profile the rules route to). */
  profiles: Profile[];
  /** The profile every staged rule commits to (the editor's `+ Add rule` passes
   *  that profile's id; a draft passes the draft id). */
  initialProfileId: string;
  runningApps: RunningApp[];
  /** All installed apps (Start Menu + App Paths + Uninstall registry). Merged
   *  AFTER the running list (running ranks higher: it's what the user most
   *  likely wants to route, and carries a live icon) and de-duplicated by
   *  process name. Defaults to [] so callers that only have the running list
   *  still work. */
  installedApps?: RunningApp[];
  /** Authoritative presentation decisions for the three Power Mode actions. */
  appCapability: AdvancedCapabilityDecision;
  websiteCapability: AdvancedCapabilityDecision;
  pathBrowseCapability: AdvancedCapabilityDecision;
  loading: boolean;
  /** Lowercased process names hidden from the list. The host passes ONLY the
   *  apps routed to the TARGET profile (not every mapped app), so apps routed
   *  to OTHER profiles still appear and can be moved here. The modal unions its
   *  own pending app keys on top. */
  mappedProcessNames: Set<string>;
  /** Returns the NAME of the profile a key is currently routed to IF that
   *  profile is NOT the target this modal commits to, else null. Lets the modal
   *  confirm a move before staging a rule that would reassign the key off
   *  another profile. Default undefined = no key is ever "owned elsewhere", so
   *  every pick stages directly (the pre-move behavior). */
  otherProfileName?: (kind: "app" | "site", key: string) => string | null;
  /** Commit: assign every rule in `rules` to `profileId` in ONE write. Keys are
   *  lowercased process names (kind "app") or normalized domains (kind "site").
   *  A key already routed to another profile MOVES here on commit (overwrite). */
  onAdd: (
    rules: Array<{ kind: "app" | "site"; key: string; path?: string | null }>,
    profileId: string,
  ) => void;
  onClose: () => void;
}) {
  const [pending, setPending] = useState<Pending[]>([]);
  const [mode, setMode] = useState<"app" | "website">("app");
  const [domainDraft, setDomainDraft] = useState("");
  // A pending MOVE awaiting confirmation: the user picked/typed a key that's
  // currently routed to `from` (another profile). Staging is deferred until they
  // confirm. Only one at a time; null = no confirmation in flight.
  const [confirmMove, setConfirmMove] = useState<{
    kind: "app" | "site";
    key: string;
    label: string;
    from: string;
    icon?: string | null;
    path?: string | null;
  } | null>(null);

  // Stable id for the aria-describedby link between the domain input and hint.
  const hintId = useId();

  const profileName =
    profiles.find((p) => p.id === initialProfileId)?.name ?? "this profile";

  // Reset the pending list, domain draft, and mode each time the modal opens (or
  // the caller changes which profile it targets).
  useEffect(() => {
    if (open) {
      setPending([]);
      setMode(appCapability.enabled ? "app" : websiteCapability.enabled ? "website" : "app");
      setDomainDraft("");
      setConfirmMove(null);
    }
  }, [open, initialProfileId, appCapability.enabled, websiteCapability.enabled]);

  // Esc dismisses (discarding the pending list).
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

  // The list hides apps already routed elsewhere AND apps already staged here, so
  // a picked app can't be added twice. Recomputed as `pending` grows.
  const listHidden = useMemo(() => {
    const s = new Set(mappedProcessNames);
    for (const p of pending) if (p.kind === "app") s.add(p.key);
    return s;
  }, [mappedProcessNames, pending]);

  // Running apps first (higher pick-priority + a live icon), then installed apps
  // the user isn't currently running, de-duplicated by process name. This is the
  // single list the picker searches and renders.
  const mergedApps = useMemo(() => {
    const seen = new Set<string>();
    const out: RunningApp[] = [];
    for (const a of [...runningApps, ...installedApps]) {
      const key = a.process_name.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(a);
    }
    return out;
  }, [runningApps, installedApps]);

  const normalized = normalizeDomain(domainDraft);

  if (!open) return null;

  // The label + icon + exe path for an app key, resolved from the merged app row
  // when one matches, else the bare process name (an app we can't detect).
  const appLabelFor = (
    processName: string,
  ): { label: string; icon: string | null; path: string | null } => {
    const key = processName.toLowerCase();
    const hit = mergedApps.find((a) => a.process_name.toLowerCase() === key);
    return {
      label: hit?.display_name || processName,
      icon: hit?.icon ?? null,
      path: hit?.path ?? null,
    };
  };

  // Stage a rule into the pending list (de-duped). The single staging seam that
  // both the conflict-free path and the confirmed-move path funnel through.
  const stageRule = (
    kind: "app" | "site",
    key: string,
    label: string,
    icon: string | null,
    path: string | null = null,
  ) => {
    setPending((prev) =>
      prev.some((p) => p.kind === kind && p.key === key)
        ? prev
        : [...prev, { kind, key, label, icon, path }],
    );
  };

  // Stage an app. If the key is currently routed to another profile, defer to a
  // confirmation (the move is staged only on confirm); otherwise stage directly.
  // De-dupes by lowercased key (an already-pending app is a no-op, no confirm).
  const addApp = (processName: string) => {
    const key = processName.toLowerCase();
    if (pending.some((p) => p.kind === "app" && p.key === key)) return;
    const { label, icon, path } = appLabelFor(processName);
    const from = otherProfileName?.("app", key);
    if (from) {
      setConfirmMove({ kind: "app", key, label, from, icon, path });
      return;
    }
    stageRule("app", key, label, icon, path);
  };

  // Browse for an app on disk via the Tauri file dialog, for an app we can't
  // auto-detect (no Start Menu / App Paths / Uninstall entry, or a portable exe).
  // Resolves the picked .exe to a process name + system name + icon, then stages
  // it through the same conflict-aware path as a list pick. No-op on cancel or an
  // unresolvable path.
  const browseForApp = async () => {
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const selected = await open({
        multiple: false,
        directory: false,
        title: "Choose an application",
        filters: [{ name: "Applications", extensions: ["exe"] }],
      });
      if (!selected || typeof selected !== "string") return;
      const resolved = await resolveAppByPath(selected);
      if (!resolved) return;
      const key = resolved.process_name.toLowerCase();
      if (pending.some((p) => p.kind === "app" && p.key === key)) return;
      const from = otherProfileName?.("app", key);
      if (from) {
        setConfirmMove({
          kind: "app",
          key,
          label: resolved.display_name,
          from,
          icon: resolved.icon,
          path: resolved.path ?? null,
        });
        return;
      }
      stageRule("app", key, resolved.display_name, resolved.icon, resolved.path ?? null);
    } catch (err) {
      console.warn(`[AddApp] browse for app failed error_type=${errorType(err)}`);
    }
  };

  // Stage a site from the domain draft (Enter or the inline Add). Normalizes and
  // de-dupes. A domain routed to another profile defers to a confirmation (input
  // kept until confirm/cancel); otherwise stages directly and clears the input.
  const addSite = () => {
    if (!normalized) return;
    if (pending.some((p) => p.kind === "site" && p.key === normalized)) {
      setDomainDraft("");
      return;
    }
    const from = otherProfileName?.("site", normalized);
    if (from) {
      setConfirmMove({ kind: "site", key: normalized, label: normalized, from });
      return;
    }
    stageRule("site", normalized, normalized, null);
    setDomainDraft("");
  };

  // Confirm a deferred move: stage the rule (it reassigns the key off `from` on
  // commit, via the host's overwrite) and clear the confirmation. For a site,
  // also clear the now-staged domain input.
  const confirmMoveAccept = () => {
    if (!confirmMove) return;
    const { kind, key, label } = confirmMove;
    const icon = kind === "app" ? (confirmMove.icon ?? appLabelFor(key).icon) : null;
    const path = kind === "app" ? (confirmMove.path ?? appLabelFor(key).path) : null;
    stageRule(kind, key, label, icon, path);
    if (kind === "site") setDomainDraft("");
    setConfirmMove(null);
  };

  // Cancel a deferred move: drop the confirmation, stage nothing. For a site the
  // input is left intact so the user can edit or retry.
  const confirmMoveCancel = () => setConfirmMove(null);

  const removePending = (kind: "app" | "site", key: string) => {
    setPending((prev) => prev.filter((p) => !(p.kind === kind && p.key === key)));
  };

  const commit = () => {
    if (pending.length === 0) return;
    onAdd(
      pending.map((p) =>
        p.path
          ? { kind: p.kind, key: p.key, path: p.path }
          : { kind: p.kind, key: p.key },
      ),
      initialProfileId,
    );
    onClose();
  };

  const addLabel =
    pending.length > 1 ? `Add ${pending.length} rules` : pending.length === 1 ? "Add rule" : "Add";

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
        className="relative w-[420px] rounded-[var(--radius-card)] p-7"
        style={cardStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-rule-heading"
      >
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="absolute top-4 right-4 w-6 h-6 rounded-full flex items-center justify-center text-steel/60 hover:text-slate transition-colors cursor-pointer"
        >
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
        </button>

        {/* Locked heading: "Add a rule" */}
        <h2
          id="add-rule-heading"
          className="text-[14px] font-medium text-slate mb-1"
        >
          Add a rule
        </h2>
        {/* Subtitle (round 04: no profile chooser, so the copy no longer asks the
            user to pick one). */}
        <p className="text-[12px] text-steel mb-3 leading-relaxed">
          Pick the apps and websites this profile cleans dictation for.
        </p>

        {/* Segmented App|Website toggle.
            Accessibility: role="radiogroup" + role="radio" + aria-checked
            (a mutually exclusive mode choice, not a tablist). Do not rely on
            the gradient alone for selection state. */}
        <div
          className="inline-flex p-[3px] rounded-full mb-3.5 gap-0.5"
          style={{
            background: "var(--surface-control)",
            border: "1px solid rgba(255,255,255,0.45)",
          }}
          role="radiogroup"
          aria-label="Rule type"
        >
          {(["app", "website"] as const).map((m) => {
            const on = mode === m;
            return (
              <button
                key={m}
                type="button"
                role="radio"
                aria-checked={on}
                disabled={m === "app" ? !appCapability.enabled : !websiteCapability.enabled}
                onClick={() => setMode(m)}
                className="h-7 px-4 rounded-full text-[12px] font-medium cursor-pointer disabled:cursor-default disabled:opacity-50 transition-all duration-150"
                style={
                  on
                    ? {
                        background: "var(--accent-fill)",
                        color: "#fff",
                      }
                    : {
                        background: "transparent",
                        color: "var(--color-steel)",
                      }
                }
              >
                {m === "app" ? "App" : "Website"}
              </button>
            );
          })}
        </div>

        {/* Mode body: App = installed/running app list + Browse; Website = domain */}
        {mode === "app" ? (
          <div>
            <div className="rounded-2xl overflow-hidden" style={listSurfaceStyle}>
              <RunningAppList
                runningApps={mergedApps}
                loading={loading}
                mappedProcessNames={listHidden}
                profileName={profileName}
                // A LIST pick adds the app to the pending list; the row then
                // disappears (it's in `listHidden`).
                onPick={(processName) => addApp(processName)}
                // No manual type-a-name row: the list shows every installed app
                // and Browse handles anything undetected (single fallback path).
                showManualEntry={false}
                // Round 04: picks accumulate, so no single-selected row. Pass null
                // and rely on hide-on-pick (keeps the legacy popover signature).
                selectedProcessName={null}
              />
            </div>
            {/* Capability-gated path escape hatch for anything not auto-detected. */}
            {pathBrowseCapability.enabled && <button
              type="button"
              onClick={browseForApp}
              className="mt-3.5 inline-flex items-center gap-1.5 h-8 px-3 rounded-full text-[12px] font-medium text-steel hover:text-slate cursor-pointer transition-all duration-150"
              style={{
                background: "var(--surface-raised)",
                border: "1px solid rgba(107,130,153,0.28)",
                boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
              }}
            >
              <FolderGlyph />
              Browse...
            </button>}
          </div>
        ) : (
          <div>
            <div className="flex items-center gap-2">
              <input
                autoFocus
                value={domainDraft}
                onChange={(e) => setDomainDraft(e.target.value)}
                onBlur={() => {
                  const n = normalizeDomain(domainDraft);
                  if (n) setDomainDraft(n);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && normalized) {
                    e.preventDefault();
                    addSite();
                  }
                }}
                placeholder="github.com"
                aria-label="Website domain"
                aria-describedby={hintId}
                className="flex-1 min-w-0 h-10 rounded-2xl px-3.5 text-[13px] text-slate outline-none placeholder:text-steel/40"
                style={{
                  background: "var(--surface-control)",
                  border: "1px solid var(--border-emphasis)",
                  boxShadow: "inset 0 2px 4px rgba(0,0,0,0.02)",
                }}
              />
              <button
                type="button"
                onClick={addSite}
                disabled={!normalized}
                aria-label="Add website"
                className="inline-flex items-center h-10 px-4 rounded-2xl text-[13px] font-medium cursor-pointer disabled:cursor-default shrink-0"
                style={
                  normalized
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
            {/* Locked hint: host-only — subdomains are separate sites */}
            <p
              id={hintId}
              className="text-[11px] mt-2 leading-relaxed"
              style={{ color: "var(--color-steel)", opacity: 0.7 }}
            >
              Subdomains are separate sites (mail.google.com is not google.com).
            </p>
          </div>
        )}

        {/* Move confirmation (round-04 follow-up). When the picked/typed key is
            routed to another profile, staging is deferred to this prominent inline
            prompt. Confirm stages it (it reassigns off the other profile on
            commit, via the host's overwrite); Cancel stages nothing. Not a nested
            portal dialog — a banner inside the existing card. */}
        {confirmMove && (
          <div
            className="mt-4 rounded-2xl p-3.5"
            style={confirmCardStyle}
            role="alertdialog"
            aria-label="Move rule to this profile"
          >
            <p className="text-[12px] text-slate leading-relaxed">
              <span className="font-medium">{confirmMove.label}</span> is routed to{" "}
              <span className="font-medium">{confirmMove.from}</span>.
            </p>
            <p className="text-[12px] text-slate leading-relaxed mb-3">
              Move it to {profileName}?
            </p>
            <div className="flex items-center justify-end gap-2">
              <button
                type="button"
                onClick={confirmMoveCancel}
                className="px-3.5 py-1.5 rounded-full text-[12px] font-medium text-slate cursor-pointer transition-all duration-200"
                style={{
                  background: "var(--surface-hover)",
                  border: "1px solid rgba(255, 255, 255, 0.85)",
                  boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirmMoveAccept}
                className="px-3.5 py-1.5 rounded-full text-[12px] font-medium cursor-pointer transition-all duration-200"
                style={{
                  background: "var(--accent-fill)",
                  color: "#fff",
                  boxShadow:
                    "var(--shadow-pearl)",
                }}
              >
                Move here
              </button>
            </div>
          </div>
        )}

        {/* Pending list — the staged rules (apps + sites), spanning both modes so
            it's visible no matter which tab is active. Wrapped removable chips,
            matching the editor's "Activated when using" chip look. */}
        {pending.length > 0 && (
          <div className="flex flex-wrap gap-2 mt-4" aria-label="Rules to add">
            {pending.map((p) => (
              <span
                key={`${p.kind}:${p.key}`}
                className="inline-flex items-center gap-1.5 h-8 pl-2 pr-1.5 rounded-full"
                style={pendingChipStyle}
              >
                {p.kind === "app" ? (
                  <AppIcon app={{ display_name: p.label, process_name: p.key, icon: p.icon }} />
                ) : (
                  <SiteGlyph />
                )}
                <span className="text-[12px] text-slate truncate max-w-[160px]">
                  {p.label}
                </span>
                <button
                  type="button"
                  onClick={() => removePending(p.kind, p.key)}
                  aria-label={`Remove ${p.label}`}
                  className="w-5 h-5 rounded-full flex items-center justify-center text-steel/60 hover:text-rose transition-colors duration-150 cursor-pointer"
                >
                  <XGlyph />
                </button>
              </span>
            ))}
          </div>
        )}

        {/* Footer — Cancel ghost + Add primary pill (disabled until something is
            staged). Add label carries the count when more than one rule. */}
        <div className="flex items-center justify-end gap-2.5 mt-5">
          <button
            type="button"
            onClick={onClose}
            className="px-5 py-2 rounded-full text-[13px] font-medium text-slate cursor-pointer transition-all duration-200"
            style={{
              background: "var(--surface-modal)",
              border: "1px solid rgba(255, 255, 255, 0.85)",
              boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.9 * var(--rim-scale)))",
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={commit}
            disabled={pending.length === 0}
            className="px-5 py-2 rounded-full text-[13px] font-medium cursor-pointer disabled:cursor-default transition-all duration-200"
            style={
              pending.length > 0
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
            {addLabel}
          </button>
        </div>
      </motion.div>
    </div>,
    document.body,
  );
}
