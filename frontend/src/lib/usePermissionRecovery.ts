import { useCallback, useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { getPermissions, getShortcutReadiness, reconcileShortcuts, type ShortcutReadiness, openPermissionSettings, requestPermission, type PermissionActionFailure, type PermissionActionOutcome, type PermissionKind, type PermissionReport, type PermissionSnapshot, type PermissionState } from "./api/system";

export const DICTATION_PERMISSION_KINDS: PermissionKind[] = ["microphone", "accessibility"];

const PERMISSION_KINDS: PermissionKind[] = [
  "microphone",
  "accessibility",
  "input-monitoring",
];
const PERMISSION_STATES = new Set<PermissionState>([
  "not-determined",
  "granted",
  "denied",
  "restricted",
  "not-required",
  "unsupported",
  "unknown",
]);
const ACTION_STATUSES = new Set([
  "completed",
  "declined",
  "unchanged",
  "failed",
  "unsupported",
]);
const ACTION_FAILURES = new Set<PermissionActionFailure>([
  "intent-required",
  "intent-reused",
  "not-requestable",
  "recovery-unavailable",
  "native-error",
]);

// Settle quickly, then keep checking while the user changes System Settings.
export const PERMISSION_SETTLEMENT_DELAYS_MS = [250, 750, 1500, 3000] as const;
export const READINESS_SETTLEMENT_DELAYS_MS = [500, 750, 1500, 3000] as const;

// The backend owns the authoritative latch. This mirror prevents Home tab
// unmount/remount from presenting a second request affordance in one process.
const requestedKinds = new Set<PermissionKind>();

export function resetPermissionRequestAttemptsForTests() {
  requestedKinds.clear();
}

export function parsePermissionReport(value: unknown): PermissionReport {
  if (!value || typeof value !== "object") throw new Error("invalid permission report");
  const report = value as Record<string, unknown>;
  if (report.schema_version !== 1 || report.platform !== "macos") {
    throw new Error("unsupported permission report");
  }
  if (!Array.isArray(report.permissions) || report.permissions.length !== 3) {
    throw new Error("incomplete permission report");
  }

  const byKind = new Map<PermissionKind, PermissionSnapshot>();
  for (const value of report.permissions) {
    if (!value || typeof value !== "object") throw new Error("invalid permission snapshot");
    const item = value as Record<string, unknown>;
    if (!PERMISSION_KINDS.includes(item.kind as PermissionKind)) {
      throw new Error("invalid permission kind");
    }
    if (!PERMISSION_STATES.has(item.state as PermissionState)) {
      throw new Error("invalid permission state");
    }
    if (typeof item.query_supported !== "boolean" || typeof item.request_supported !== "boolean") {
      throw new Error("invalid permission capability metadata");
    }
    const kind = item.kind as PermissionKind;
    if (byKind.has(kind)) throw new Error("duplicate permission kind");
    byKind.set(kind, {
      kind,
      state: item.state as PermissionState,
      query_supported: item.query_supported,
      request_supported: item.request_supported,
    });
  }
  if (byKind.size !== PERMISSION_KINDS.length) throw new Error("incomplete permission report");
  // Older backends omit this metadata. Never let malformed metadata silently
  // weaken the audited active-tap requirement.
  if (report.required_for_operations !== undefined) {
    const operations = report.required_for_operations as { dictation?: unknown } | null;
    const required = operations?.dictation;
    if (!Array.isArray(required) || required.length !== 2
        || !DICTATION_PERMISSION_KINDS.every(kind => required.includes(kind))) {
      throw new Error("invalid dictation permission requirements");
    }
  }
  return {
    schema_version: 1,
    platform: "macos",
    permissions: PERMISSION_KINDS.map((kind) => byKind.get(kind)!),
    ...(report.required_for_operations === undefined ? {} : { required_for_operations: { dictation: [...DICTATION_PERMISSION_KINDS] } }),
  };
}

function parseActionOutcome(
  value: unknown,
  kind: PermissionKind,
  action: PermissionActionOutcome["action"],
): PermissionActionOutcome {
  if (!value || typeof value !== "object") throw new Error("invalid permission action outcome");
  const outcome = value as Record<string, unknown>;
  const failed = outcome.status === "failed" || outcome.status === "unsupported";
  if (
    outcome.schema_version !== 1 ||
    outcome.kind !== kind ||
    outcome.action !== action ||
    !ACTION_STATUSES.has(String(outcome.status)) ||
    !PERMISSION_STATES.has(outcome.before as PermissionState) ||
    !PERMISSION_STATES.has(outcome.after as PermissionState) ||
    typeof outcome.relaunch_required !== "boolean" ||
    (failed
      ? !ACTION_FAILURES.has(outcome.failure as PermissionActionFailure)
      : outcome.failure !== null)
  ) {
    throw new Error("invalid permission action outcome");
  }
  return outcome as unknown as PermissionActionOutcome;
}

export function parseShortcutReadiness(value: unknown): ShortcutReadiness {
  const item = value as ShortcutReadiness | null;
  if (!item || !["ready", "pending", "failed", "blocked", "disabled"].includes(item.state)
      || ![null, "permission_required", "native_error", "busy", "shutdown"].includes(item.failure)) {
    throw new Error("invalid shortcut readiness");
  }
  return { state: item.state, failure: item.failure };
}

export interface PermissionActionViewState {
  busy: "request" | "recovery" | null;
  caption: { text: string; error: boolean } | null;
  guidance: boolean;
}

function emptyActionState(): Record<PermissionKind, PermissionActionViewState> {
  return {
    microphone: { busy: null, caption: null, guidance: false },
    accessibility: { busy: null, caption: null, guidance: false },
    "input-monitoring": { busy: null, caption: null, guidance: false },
  };
}

function isResolved(snapshot: PermissionSnapshot | undefined): boolean {
  return snapshot?.state === "granted";
}

function failureCopy(failure: PermissionActionFailure | null): string {
  switch (failure) {
    case "intent-required":
    case "intent-reused":
      return "The request didn't go through. Open System Settings to allow access.";
    case "not-requestable":
      return "Turn on access in System Settings.";
    case "recovery-unavailable":
      return "Couldn't open System Settings. Open it from the Apple menu.";
    default:
      return "macOS didn't respond. Open System Settings to allow access.";
  }
}

type Tone = "mint" | "gold" | "steel";

export function permissionPresentation(snapshot: PermissionSnapshot): {
  label: string;
  tone: Tone;
  caption: string | null;
  aria: string;
} {
  if (snapshot.state === "unknown" && !snapshot.query_supported) {
    return {
      label: "Can't check access",
      tone: "steel",
      caption:
        "Whisperstream can't check this permission right now.",
      aria: "status could not be checked",
    };
  }
  switch (snapshot.state) {
    case "granted":
      return { label: "Allowed", tone: "mint", caption: null, aria: "access is allowed" };
    case "not-determined":
      return {
        label: "Not allowed yet",
        tone: "gold",
        caption: null,
        aria: "access has not been allowed yet",
      };
    case "denied":
      return { label: "Not allowed", tone: "gold", caption: null, aria: "access is not allowed" };
    case "restricted":
      return {
        label: "Managed by this Mac",
        tone: "gold",
        caption: "Access is restricted on this Mac and can't be changed here.",
        aria: "access is managed by this Mac",
      };
    case "unknown":
      return { label: "Not confirmed", tone: "gold", caption: null, aria: "access is not confirmed" };
    case "not-required":
      return { label: "Can't confirm access", tone: "steel", caption: "Whisperstream couldn't confirm this permission.", aria: "access could not be confirmed" };
    case "unsupported":
      return {
        label: "Unavailable",
        tone: "steel",
        caption: "Whisperstream can't check this permission on this Mac.",
        aria: "status is unavailable on this system",
      };
  }
}

export function permissionAction(
  snapshot: PermissionSnapshot,
  attempted: boolean,
): "request" | "recovery" | "recheck" | null {
  if (snapshot.state === "not-required") return "recheck";
  if (
    snapshot.state === "granted" ||
    snapshot.state === "unsupported" ||
    snapshot.state === "restricted"
  ) return null;
  if (
    (snapshot.state === "not-determined" ||
      (snapshot.state === "unknown" && snapshot.query_supported)) &&
    snapshot.request_supported &&
    !attempted
  ) return "request";
  return "recovery";
}

export function permissionGuidance(kind: PermissionKind): string {
  const pane = kind === "microphone" ? "Microphone" : kind === "accessibility" ? "Accessibility" : "Input Monitoring";
  return `In System Settings, open Privacy & Security, then ${pane}. Turn on Whisperstream, then come back here.`;
}

export function usePermissionRecovery(connected: boolean) {
  const [readiness, setReadiness] = useState<ShortcutReadiness>({ state: "pending", failure: null });
  const reconciliationAttemptedRef = useRef(false);
  const readinessPollCountRef = useRef(0);
  const [report, setReport] = useState<PermissionReport | null>(null);
  const [loadState, setLoadState] = useState<"loading" | "ready" | "failed">("loading");
  const [checking, setChecking] = useState(false);
  const [backgroundCheck, setBackgroundCheck] = useState(false);
  const [actions, setActions] = useState(emptyActionState);

  const mountedRef = useRef(true);
  const connectedRef = useRef(connected);
  const reportRef = useRef<PermissionReport | null>(null);
  const queryGenerationRef = useRef(0);
  const queryInFlightRef = useRef<{
    generation: number;
    promise: Promise<PermissionReport | null>;
  } | null>(null);
  const actionInFlightRef = useRef(new Set<PermissionKind>());
  const actionGenerationRef = useRef<Record<PermissionKind, number>>({
    microphone: 0,
    accessibility: 0,
    "input-monitoring": 0,
  });
  const settlementRef = useRef<{ token: number; timer: ReturnType<typeof setTimeout> | null }>({
    token: 0,
    timer: null,
  });

  const stopSettlement = useCallback(() => {
    settlementRef.current.token += 1;
    if (settlementRef.current.timer !== null) {
      clearTimeout(settlementRef.current.timer);
      settlementRef.current.timer = null;
    }
  }, []);

  const applyReport = useCallback((next: PermissionReport) => {
    reportRef.current = next;
    setReport(next);
    setLoadState("ready");
    setActions((current) => {
      const updated = { ...current };
      for (const snapshot of next.permissions) {
        if (isResolved(snapshot) && current[snapshot.kind].busy === null) {
          updated[snapshot.kind] = { busy: null, caption: null, guidance: false };
        }
      }
      return updated;
    });
  }, []);

  const runQuery = useCallback(async (background = false): Promise<PermissionReport | null> => {
    if (!connectedRef.current) return null;
    const generation = queryGenerationRef.current;
    const inFlight = queryInFlightRef.current;
    if (inFlight) {
      if (inFlight.generation === generation) return inFlight.promise;
      return inFlight.promise.then(() => runQuery(background));
    }
    setBackgroundCheck(background);
    setChecking(true);
    let task!: Promise<PermissionReport | null>;
    task = (async () => {
      try {
        const next = parsePermissionReport(await getPermissions());
        if (!mountedRef.current || generation !== queryGenerationRef.current) return null;
        applyReport(next);
        const allowed = DICTATION_PERMISSION_KINDS.every(kind => next.permissions.some(item => item.kind === kind && item.state === "granted"));
        if (!allowed) {
          reconciliationAttemptedRef.current = false;
          setReadiness({ state: "blocked", failure: "permission_required" });
        } else {
          try {
            let status = parseShortcutReadiness(await getShortcutReadiness());
            if (!mountedRef.current || generation !== queryGenerationRef.current) return null;
            if (status.state !== "ready" && status.state !== "disabled" && !reconciliationAttemptedRef.current) {
              reconciliationAttemptedRef.current = true;
              setReadiness({ state: "pending", failure: null });
              status = parseShortcutReadiness(await reconcileShortcuts());
              if (!mountedRef.current || generation !== queryGenerationRef.current) return null;
              // Reconciliation crosses a native boundary. Reconfirm grants after
              // it, so a permission revoked during registration cannot unlock UI.
              if (status.state === "ready") {
                const confirmed = parsePermissionReport(await getPermissions());
                if (!mountedRef.current || generation !== queryGenerationRef.current) return null;
                applyReport(confirmed);
                if (!DICTATION_PERMISSION_KINDS.every(kind => confirmed.permissions.some(item => item.kind === kind && item.state === "granted"))) {
                  reconciliationAttemptedRef.current = false;
                  status = { state: "blocked", failure: "permission_required" };
                }
              }
            }
            if (mountedRef.current && generation === queryGenerationRef.current) setReadiness(status);
          } catch {
            if (mountedRef.current && generation === queryGenerationRef.current) setReadiness({ state: "failed", failure: "native_error" });
          }
        }
        return next;
      } catch {
        if (mountedRef.current && generation === queryGenerationRef.current) {
          reportRef.current = null;
          setReport(null);
          setLoadState("failed");
        }
        return null;
      } finally {
        if (queryInFlightRef.current?.promise === task) queryInFlightRef.current = null;
        if (mountedRef.current && generation === queryGenerationRef.current) setChecking(false);
      }
    })();
    queryInFlightRef.current = { generation, promise: task };
    return task;
  }, [applyReport]);

  useEffect(() => {
    if (readiness.state !== "pending" || readiness.failure !== "busy") {
      readinessPollCountRef.current = 0;
      return;
    }
    if (!connected || checking || loadState !== "ready"
        || readinessPollCountRef.current >= READINESS_SETTLEMENT_DELAYS_MS.length) return;
    const generation = queryGenerationRef.current;
    const timer = setTimeout(() => {
      if (!mountedRef.current || !connectedRef.current || generation !== queryGenerationRef.current) return;
      readinessPollCountRef.current += 1;
      void runQuery(true);
    }, READINESS_SETTLEMENT_DELAYS_MS[readinessPollCountRef.current]);
    return () => clearTimeout(timer);
  }, [connected, checking, loadState, readiness, runQuery]);

  const patchReportedState = useCallback((kind: PermissionKind, state: PermissionState) => {
    const current = reportRef.current;
    if (!current) return;
    const next = {
      ...current,
      permissions: current.permissions.map((item) =>
        item.kind === kind ? { ...item, state } : item,
      ),
    };
    applyReport(next);
  }, [applyReport]);

  const startSettlement = useCallback((kind: PermissionKind) => {
    stopSettlement();
    if (isResolved(reportRef.current?.permissions.find((item) => item.kind === kind))) { void runQuery(true); return; }
    const token = settlementRef.current.token;

    const schedule = (index: number) => {
      settlementRef.current.timer = setTimeout(async () => {
        settlementRef.current.timer = null;
        if (
          !mountedRef.current ||
          !connectedRef.current ||
          token !== settlementRef.current.token
        ) return;
        const next = await runQuery(true);
        if (token !== settlementRef.current.token) return;
        const snapshot = next?.permissions.find((item) => item.kind === kind);
        if (isResolved(snapshot)) return;
        schedule(index + 1);
      }, PERMISSION_SETTLEMENT_DELAYS_MS[index] ?? 2000);
    };
    schedule(0);
  }, [runQuery, stopSettlement]);

  const setAction = useCallback(
    (kind: PermissionKind, patch: Partial<PermissionActionViewState>) => {
      setActions((current) => ({
        ...current,
        [kind]: { ...current[kind], ...patch },
      }));
    },
    [],
  );

  const request = useCallback(async (kind: PermissionKind) => {
    if (!connectedRef.current || requestedKinds.has(kind) || actionInFlightRef.current.size > 0) return;
    requestedKinds.add(kind);
    actionInFlightRef.current.add(kind);
    stopSettlement();
    queryGenerationRef.current += 1;
    const actionGeneration = ++actionGenerationRef.current[kind];
    setReadiness({ state: "pending", failure: null });
    setAction(kind, { busy: "request", caption: null, guidance: false });
    try {
      const outcome = parseActionOutcome(await requestPermission(kind), kind, "request");
      if (!mountedRef.current || actionGeneration !== actionGenerationRef.current[kind]) return;
      queryGenerationRef.current += 1;
      patchReportedState(kind, outcome.after);
      if (outcome.status === "failed" || outcome.status === "unsupported") {
        setAction(kind, {
          caption: { text: failureCopy(outcome.failure), error: true },
        });
      } else if (outcome.status === "declined") {
        setAction(kind, {
          caption: {
            text: "Turn on access in System Settings.",
            error: false,
          },
        });
      } else {
        setAction(kind, { guidance: outcome.after !== "granted" });
        startSettlement(kind);
      }
    } catch {
      if (mountedRef.current && actionGeneration === actionGenerationRef.current[kind]) {
        setAction(kind, { caption: { text: failureCopy("native-error"), error: true } });
      }
    } finally {
      actionInFlightRef.current.delete(kind);
      if (mountedRef.current && actionGeneration === actionGenerationRef.current[kind]) {
        setAction(kind, { busy: null });
        setChecking(false);
      }
    }
  }, [patchReportedState, setAction, startSettlement, stopSettlement]);

  const recover = useCallback(async (kind: PermissionKind) => {
    if (!connectedRef.current || actionInFlightRef.current.size > 0) return;
    actionInFlightRef.current.add(kind);
    stopSettlement();
    queryGenerationRef.current += 1;
    const actionGeneration = ++actionGenerationRef.current[kind];
    setAction(kind, { busy: "recovery", caption: null, guidance: false });
    try {
      const outcome = parseActionOutcome(
        await openPermissionSettings(kind),
        kind,
        "open-settings",
      );
      if (!mountedRef.current || actionGeneration !== actionGenerationRef.current[kind]) return;
      queryGenerationRef.current += 1;
      patchReportedState(kind, outcome.after);
      if (outcome.status === "failed" || outcome.status === "unsupported") {
        setAction(kind, {
          caption: { text: failureCopy("recovery-unavailable"), error: true }, guidance: true,
        });
      } else {
        setAction(kind, { guidance: true });
        startSettlement(kind);
      }
    } catch {
      if (mountedRef.current && actionGeneration === actionGenerationRef.current[kind]) {
        setAction(kind, { caption: { text: failureCopy("recovery-unavailable"), error: true }, guidance: true });
      }
    } finally {
      actionInFlightRef.current.delete(kind);
      if (mountedRef.current && actionGeneration === actionGenerationRef.current[kind]) {
        setAction(kind, { busy: null });
        setChecking(false);
      }
    }
  }, [patchReportedState, setAction, startSettlement, stopSettlement]);

  const retryListener = useCallback(() => {
    if (actionInFlightRef.current.size > 0) return;
    reconciliationAttemptedRef.current = false;
    readinessPollCountRef.current = 0;
    void runQuery();
  }, [runQuery]);

  const recheck = useCallback(() => {
    void runQuery();
  }, [runQuery]);

  useEffect(() => {
    connectedRef.current = connected;
    if (!connected) {
      stopSettlement();
      queryGenerationRef.current += 1;
      reconciliationAttemptedRef.current = false;
      setReadiness({ state: "pending", failure: null });
      reportRef.current = null;
      setReport(null);
      setLoadState("loading");
      for (const kind of PERMISSION_KINDS) actionGenerationRef.current[kind] += 1;
      setActions(emptyActionState());
      setChecking(false);
      return;
    }
    void runQuery();
  }, [connected, runQuery, stopSettlement]);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | null = null;
    let focusProbeTimer: ReturnType<typeof setTimeout> | null = null;
    let focusLossProbe: Promise<boolean> | null = null;
    try {
      const currentWindow = getCurrentWindow();
      currentWindow.onFocusChanged((event) => {
        if (!event.payload) {
          if (focusProbeTimer !== null) clearTimeout(focusProbeTimer);
          focusLossProbe = new Promise((resolve) => {
            focusProbeTimer = setTimeout(() => {
              focusProbeTimer = null;
              Promise.all([
                invoke<boolean>("macos_application_is_active").catch(() => true),
                currentWindow.isVisible().catch(() => true),
              ]).then(([applicationActive, mainVisible]) => {
                resolve(!applicationActive || !mainVisible);
              });
            }, 0);
          });
          return;
        }
        const probe = focusLossProbe;
        focusLossProbe = null;
        if (probe) {
          void probe.then((shouldRefresh) => {
            if (!disposed && shouldRefresh && connectedRef.current) {
              reconciliationAttemptedRef.current = false;
              void runQuery(true);
            }
          });
        }
      }).then((dispose) => {
        if (disposed) dispose();
        else unlisten = dispose;
      }).catch(() => {});
    } catch {
      // Plain browser development has no Tauri window lifecycle.
    }
    return () => {
      disposed = true;
      if (focusProbeTimer !== null) clearTimeout(focusProbeTimer);
      unlisten?.();
    };
  }, [runQuery, stopSettlement]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stopSettlement();
    };
  }, [stopSettlement]);

  return {
    report,
    readiness,
    retryListener,
    ready: connected && loadState === "ready" && !checking && readiness.state === "ready"
      && !Object.values(actions).some(action => action.busy)
      && DICTATION_PERMISSION_KINDS.every(kind => report?.permissions.some(item => item.kind === kind && item.state === "granted")),
    loadState,
    checking: checking && !backgroundCheck,
    actions,
    requestedKinds,
    request,
    recover,
    recheck,
  };
}
