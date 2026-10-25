import type { PermissionKind } from "../../lib/api/system";
import {
  DICTATION_PERMISSION_KINDS,
  permissionAction,
  permissionGuidance,
  permissionPresentation,
  usePermissionRecovery,
} from "../../lib/usePermissionRecovery";
import { SecondaryButton } from "../SecondaryButton";
import { PermissionRestart } from "../PermissionRestart";

type Recovery = ReturnType<typeof usePermissionRecovery>;
const DETAILS: Partial<Record<PermissionKind, { title: string; purpose: string }>> = {
  microphone: { title: "Microphone", purpose: "Record your voice for dictation." },
  accessibility: { title: "Accessibility", purpose: "Use your shortcut and type into other apps." },
};

// Intent: first-run users approve two specific requests without losing their
// place. Keep the approved frost-glass surface, Outfit Light heading, 4px
// spacing grid, quiet steel rings, mint checks, and shared secondary actions.
export function StepPermissions({ recovery, connected }: { recovery: Recovery; connected: boolean }) {
  const stale = !connected || recovery.loadState === "failed";
  const busy = recovery.checking || Object.values(recovery.actions).some(action => action.busy);
  const allAllowed = DICTATION_PERMISSION_KINDS.every(kind =>
    recovery.report?.permissions.some(item => item.kind === kind && item.state === "granted"));
  return (
    <section aria-labelledby="onboarding-permissions-heading"
      className="mx-auto w-[560px] max-w-full rounded-[28px] p-7 backdrop-blur-xl shadow-glass-sm"
      style={{ background: "var(--surface-card)", border: "1px solid var(--border-hairline)", borderTopColor: "var(--border-emphasis)" }}>
      <h1 id="onboarding-permissions-heading" className="font-display text-[24px] font-light tracking-tight text-slate">Allow access for dictation</h1>
      <p className="mt-2 text-[12px] leading-relaxed text-steel">Allow access to record your voice and type into other apps.</p>
      <div className="mt-5">
        {DICTATION_PERMISSION_KINDS.map((kind, index) => {
          const details = DETAILS[kind]!;
          const snapshot = recovery.report?.permissions.find(item => item.kind === kind);
          const actionState = recovery.actions[kind];
          const presentation = snapshot ? permissionPresentation(snapshot) : null;
          const allowed = !stale && snapshot?.state === "granted";
          const action = snapshot ? permissionAction(snapshot, recovery.requestedKinds.has(kind)) : null;
          const status = stale ? "Couldn't confirm access" : actionState.busy === "request" ? "Asking macOS..."
            : actionState.busy === "recovery" ? "Opening..."
            : actionState.guidance && !allowed ? "Waiting for access" : presentation?.label ?? "Checking...";
          const guidance = !stale && !allowed && (actionState.guidance || snapshot?.state === "denied");
          const restart = !stale && !allowed && kind === "accessibility" && actionState.guidance && !actionState.caption?.error;
          const showAction = !stale && !allowed && snapshot && action;
          const invoke = () => action === "request" ? void recovery.request(kind)
            : action === "recovery" ? void recovery.recover(kind) : recovery.recheck();
          const label = action === "request" ? `Allow ${details.title.toLowerCase()}`
            : action === "recovery" ? "Open System Settings" : "Check again";
          return (
            <section key={kind} aria-label={`${details.title}: ${status}`} className="py-4"
              style={{ borderTop: index ? "1px solid var(--divider)" : undefined }}>
              <div className="grid grid-cols-[20px_minmax(0,1fr)_auto] max-[600px]:grid-cols-[20px_minmax(0,1fr)] items-center gap-x-3 gap-y-2">
                <span aria-hidden="true" data-permission-approved={allowed} className="flex h-5 w-5 items-center justify-center rounded-full motion-safe:transition-colors"
                  style={{ background: allowed ? "var(--color-mint)" : "transparent", border: `1.5px solid ${allowed ? "var(--color-mint)" : "color-mix(in srgb, var(--color-steel) 50%, transparent)"}`, color: "var(--status-check-ink)" }}>
                  {allowed && <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 6.4 4.8 8.8 9.5 3.4" /></svg>}
                </span>
                <div><div className="text-[13px] font-medium text-slate">{details.title}</div><p className="mt-0.5 text-[12px] leading-normal text-steel">{details.purpose}</p></div>
                <div className="max-[600px]:col-start-2 max-[600px]:justify-self-start">
                  {restart ? <PermissionRestart compact disabled={busy} /> : showAction ? <SecondaryButton type="button" onClick={invoke} disabled={busy}
                    aria-label={action === "request" ? label : `${label} for ${details.title}`}
                    className="rounded-full px-3 py-1.5 text-[11.5px] text-slate disabled:opacity-50">
                    {actionState.busy ? status : label}
                  </SecondaryButton> : <span className={`text-[11.5px] ${allowed ? "text-slate" : "text-steel"}`}>{status}</span>}
                </div>
              </div>
              {!stale && !allowed && (actionState.caption || presentation?.caption) && <p className="mt-2 ml-8 text-[12px] leading-relaxed text-steel">{actionState.caption?.text ?? presentation?.caption}</p>}
              {guidance && <p className="mt-2 ml-8 text-[12px] leading-relaxed text-steel" aria-live="polite">
                {restart ? "Already enabled? Restart to refresh access." : permissionGuidance(kind)}
                {restart && <button type="button" onClick={() => void recovery.recover(kind)} disabled={busy}
                  className="ml-2 text-slate underline disabled:opacity-50">Open System Settings</button>}
              </p>}
            </section>
          );
        })}
      </div>
      <div role="status" aria-live="polite" className="mt-2 text-[12px] leading-relaxed text-steel">
        {!connected ? "Whisperstream isn't responding. Access can't be confirmed yet."
          : recovery.loadState === "failed" ? <><span>Couldn't check permissions. Try again.</span><Retry onClick={recovery.recheck} /></>
          : recovery.checking ? "Checking permissions..."
          : allAllowed && !recovery.ready ? <><span>{recovery.readiness.state === "pending" ? "Getting your shortcut ready..." : "Access is allowed, but your shortcut isn't ready."}</span><Retry onClick={recovery.retryListener} /></>
          : recovery.ready ? "Ready to choose your shortcut." : null}
      </div>
    </section>
  );
}

function Retry({ onClick }: { onClick: () => void }) {
  return <button type="button" onClick={onClick} className="ml-3 text-slate underline">Try again</button>;
}
