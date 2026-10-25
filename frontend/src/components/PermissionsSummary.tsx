import { motion } from "framer-motion";
import { PermissionRestart } from "./PermissionRestart";
import type { PermissionKind, PermissionSnapshot } from "../lib/api/system";
import {
  type PermissionActionViewState,
  DICTATION_PERMISSION_KINDS,
  permissionPresentation,
  permissionAction,
  permissionGuidance,
  usePermissionRecovery,
} from "../lib/usePermissionRecovery";

const DETAILS: Record<PermissionKind, { title: string; pane: string }> = {
  microphone: { title: "Microphone", pane: "Microphone" },
  accessibility: { title: "Accessibility", pane: "Accessibility" },
  "input-monitoring": { title: "Input Monitoring", pane: "Input Monitoring" },
};

type Tone = "mint" | "gold" | "steel";

const TONE_COLOR: Record<Tone, string> = {
  mint: "var(--color-mint)",
  gold: "var(--color-gold)",
  steel: "var(--color-steel)",
};
const TONE_HALO: Record<Tone, string | null> = {
  mint: "var(--halo-mint)",
  gold: "var(--halo-gold)",
  steel: null,
};

function PermissionStatus({
  label,
  tone,
  ariaLabel,
  summary = false,
}: {
  label: string;
  tone: Tone;
  ariaLabel: string;
  summary?: boolean;
}) {
  const halo = TONE_HALO[tone];
  return (
    <span role="status" aria-label={ariaLabel} className="flex items-center gap-2">
      <span className="relative flex h-3.5 w-3.5 shrink-0 items-center justify-center" aria-hidden>
        {halo && (
          <span
            className="absolute inset-0 scale-[1.6] rounded-full"
            style={{ background: `radial-gradient(circle, ${halo} 0%, transparent 70%)` }}
          />
        )}
        <span
          className="relative h-[7px] w-[7px] rounded-full"
          style={{
            background: TONE_COLOR[tone],
            boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.6 * var(--rim-scale)))",
          }}
        />
      </span>
      <span className={`${summary ? "text-[12px]" : "text-[11.5px]"} ${tone === "steel" ? "text-steel" : "text-slate"} whitespace-nowrap`}>
        {label}
      </span>
    </span>
  );
}

function PermissionButton({
  children,
  onClick,
  disabled = false,
  emphasized = false,
  ariaBusy,
}: {
  children: string;
  onClick: () => void;
  disabled?: boolean;
  emphasized?: boolean;
  ariaBusy?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-busy={ariaBusy}
      className="h-[26px] rounded-full px-3 text-[11.5px] font-medium whitespace-nowrap cursor-pointer disabled:cursor-default disabled:opacity-60 transition-colors duration-150"
      style={{
        color: emphasized ? "var(--color-steel)" : "var(--color-slate)",
        background: "var(--surface-control)",
        border: `1px solid ${emphasized ? "var(--border-accent)" : "var(--border-hairline)"}`,
        boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.7 * var(--rim-scale)))",
      }}
      onMouseEnter={(event) => {
        if (disabled) return;
        event.currentTarget.style.background = "var(--surface-hover)";
        event.currentTarget.style.borderColor = "var(--border-accent)";
      }}
      onMouseLeave={(event) => {
        event.currentTarget.style.background = "var(--surface-control)";
        event.currentTarget.style.borderColor = emphasized
          ? "var(--border-accent)"
          : "var(--border-hairline)";
      }}
    >
      {children}
    </button>
  );
}

function PermissionRow({
  snapshot,
  actionState,
  attempted,
  divided,
  onRequest,
  onRecover,
  onRecheck,
  disabled,
}: {
  snapshot: PermissionSnapshot;
  actionState: PermissionActionViewState;
  attempted: boolean;
  divided: boolean;
  onRequest: () => void;
  onRecover: () => void;
  onRecheck: () => void;
  disabled: boolean;
}) {
  const details = DETAILS[snapshot.kind];
  const presentation = permissionPresentation(snapshot);
  const action = permissionAction(snapshot, attempted);
  const restart = snapshot.kind === "accessibility" && snapshot.state !== "granted"
    && actionState.guidance && !actionState.caption?.error;
  return (
    <div
      className="py-[7px] pl-[9px]"
      style={divided ? { borderTop: "1px solid var(--divider)" } : undefined}
    >
      <div className="flex items-center justify-between gap-4">
        <span className="text-[11.5px] font-medium text-slate">{details.title}</span>
        <div className="flex shrink-0 items-center gap-4">
          <PermissionStatus
            label={presentation.label}
            tone={presentation.tone}
            ariaLabel={`${details.title} ${presentation.aria}`}
          />
          {actionState.busy === "request" ? (
            <PermissionButton disabled ariaBusy onClick={() => {}}>Asking macOS...</PermissionButton>
          ) : actionState.busy === "recovery" ? (
            <PermissionButton disabled ariaBusy onClick={() => {}}>Opening...</PermissionButton>
          ) : restart ? <PermissionRestart compact disabled={disabled} /> : action === "request" ? (
            <PermissionButton disabled={disabled} onClick={onRequest}>Allow</PermissionButton>
          ) : action === "recovery" ? (
            <PermissionButton disabled={disabled} onClick={onRecover}>Open System Settings</PermissionButton>
          ) : action === "recheck" ? <PermissionButton disabled={disabled} onClick={onRecheck}>Check again</PermissionButton> : null}
        </div>
      </div>
      {actionState.caption ? (
        <div
          aria-live="polite"
          className={`mt-1 text-[10.5px] leading-[1.45] ${actionState.caption.error ? "text-rose" : "text-steel"}`}
        >
          {actionState.caption.text}
        </div>
      ) : presentation.caption ? (
        <div className="mt-1 text-[10.5px] leading-[1.45] text-steel">
          {presentation.caption}
        </div>
      ) : null}
      {actionState.guidance && (
        <p aria-live="polite" className="mt-2 text-[10.5px] leading-[1.5] text-steel">
          {restart ? "Already enabled? Restart to refresh access." : permissionGuidance(snapshot.kind)}
          {restart && <button type="button" onClick={onRecover} disabled={disabled}
            className="ml-2 text-slate underline disabled:opacity-50">Open System Settings</button>}
        </p>
      )}
    </div>
  );
}

export function PermissionsSummary({ connected }: { connected: boolean }) {
  const recovery = usePermissionRecovery(connected);
  const problems = recovery.report?.permissions.filter((snapshot) => DICTATION_PERMISSION_KINDS.includes(snapshot.kind) && snapshot.state !== "granted") ?? [];
  const summary = !connected
    ? { label: "Couldn't confirm access", tone: "steel" as const, aria: "Couldn't confirm access" }
    : recovery.loadState === "failed"
    ? { label: "Couldn't confirm access", tone: "steel" as const, aria: "Couldn't confirm access" }
    : recovery.loadState === "loading" || recovery.checking
      ? { label: "Checking...", tone: "steel" as const, aria: "Checking permission status" }
      : problems.length === 0 && recovery.ready
        ? { label: "All permissions allowed", tone: "mint" as const, aria: "All permissions are allowed" }
        : problems.length === 0
          ? { label: "Shortcut needs attention", tone: "steel" as const, aria: "Shortcut needs attention" }
        : {
            label: problems.length === 1 ? "1 needs attention" : `${problems.length} need attention`,
            tone: "gold" as const,
            aria: problems.length === 1 ? "One permission needs attention" : `${problems.length} permissions need attention`,
          };

  return (
    <motion.section
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 5 * 0.03, type: "spring", stiffness: 400, damping: 30 }}
      aria-labelledby="permissions-heading"
      className="py-3"
    >
      <div className="flex min-h-[30px] items-center justify-between gap-4">
        <h3 id="permissions-heading" className="text-[13px] font-medium text-slate">Permissions</h3>
        <div className="flex shrink-0 items-center gap-3">
          <PermissionStatus label={summary.label} tone={summary.tone} ariaLabel={summary.aria} summary />
          {recovery.loadState !== "failed" && (
            <PermissionButton
              emphasized
              disabled={recovery.checking || !connected}
              ariaBusy={recovery.checking}
              onClick={recovery.recheck}
            >
              Check again
            </PermissionButton>
          )}
        </div>
      </div>

      {!connected && <p className="text-[11.5px] text-steel">Whisperstream isn't responding. Access can't be confirmed yet.</p>}
      {connected && problems.length === 0 && recovery.loadState === "ready" && !recovery.checking && recovery.readiness.state !== "ready" && (
        <div className="flex items-center justify-between gap-4 py-2 text-[11.5px] text-steel">
          <span>{recovery.readiness.state === "pending" ? "Getting your shortcut ready..." : "Access is allowed, but your shortcut isn't ready."}</span>
          <PermissionButton onClick={recovery.retryListener}>Try again</PermissionButton>
        </div>
      )}
      {(recovery.loadState === "failed" ||
        (recovery.loadState === "ready" && problems.length > 0)) && (
        <div className="ml-[3px] mt-[5px] pl-3.5">
          {recovery.loadState === "failed" ? (
            <div className="flex items-center justify-between gap-4 py-[7px] pl-[9px]">
              <div>
                <div className="text-[11.5px] font-medium text-slate">Couldn't check permissions. Try again.</div>
                <div className="mt-0.5 text-[10.5px] text-steel">Access can't be confirmed yet.</div>
              </div>
              <PermissionButton disabled={!connected} onClick={recovery.recheck}>Try again</PermissionButton>
            </div>
          ) : (
            problems.map((snapshot, index) => (
              <PermissionRow
                key={snapshot.kind}
                snapshot={snapshot}
                actionState={recovery.actions[snapshot.kind]}
                attempted={recovery.requestedKinds.has(snapshot.kind)}
                divided={index > 0}
                onRequest={() => void recovery.request(snapshot.kind)}
                onRecover={() => void recovery.recover(snapshot.kind)}
                onRecheck={recovery.recheck}
                disabled={!connected || Object.values(recovery.actions).some(action => action.busy !== null)}
              />
            ))
          )}
        </div>
      )}
    </motion.section>
  );
}
