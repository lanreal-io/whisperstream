import { agentRows, agentsEnabled as readAgentsEnabled, deriveProviderRow, rowNeedsUser } from "../components/agentSpeak/agentCompatibility";
import type { AgentProviderActivityMap, AgentProviderLastConnectMap, ProviderActivity } from "../components/agentSpeak/agentCompatibility";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { PrimaryButton } from "../components/PrimaryButton";
import { HudConfigModal } from "../components/HudConfigModal";
import { AgentSpeakSetupModal } from "../components/agentSpeak/AgentSpeakSetupModal";
import { AGENT_PROVIDER_LABEL, type AgentProviderResultMap } from "../components/agentSpeak/AgentProviderInstallList";
import type { AppConfig } from "../lib/api/config";
import type { CodingAgentProvider } from "../lib/api/agents";
import type { SpeechEngine, SpeechStatus } from "../lib/api/speech";
import { enableClaudeHud, enableCodexHud, disableClaudeHud, disableCodexHud, getClaudeHudStatus, getCodexHudStatus } from "../lib/api/agents";
import { saveConfig } from "../lib/api/config";
import { getSpeechStatus, startSpeechModelDownload } from "../lib/api/speech";
import { cancelDownload } from "../lib/api/models";
import type { AppEvent } from "../lib/socket/events";
import { subscribeToDownloadEvents } from "../lib/socket/downloads";

export interface AgentsProps {
  config: AppConfig;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  onCommitConfig?: (patch: Partial<AppConfig>) => Promise<void>;
  send: (msg: Record<string, unknown>) => boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  connected: boolean;
  /** Surface a transient status message in the Sidebar toast. */
  onShowStatus: (message: string, isError: boolean) => void;
}

/** Provider status read budgets. The first read probes installed engines and
 *  can take over 10 s cold; polls after a result keep the short budget. */
const FIRST_STATUS_TIMEOUT_MS = 30000;
const STATUS_POLL_TIMEOUT_MS = 5000;

/** Owns provider reconciliation and voice downloads across all three settings tabs. */
export function Agents({
  config,
  onConfigChange,
  onCommitConfig,
  send,
  subscribe,
  connected,
  onShowStatus,
}: AgentsProps) {
  // Opening setup is transient. Only its saved completion reveals settings;
  // closing an unfinished flow keeps the introduction and configured providers.
  const [setupOpen, setSetupOpen] = useState(false);
  const introId = useId();
  const hasConfiguredProvider = Boolean(
    config.claude_hud_enabled || config.codex_hud_enabled,
  );

  // Selection, install, health, and measured size state is backend-authoritative.
  // The queue records concrete pending downloads and preserves model identity.
  const [speechStatus, setSpeechStatus] = useState<SpeechStatus | null>(null);
  const [speechPlaying, setSpeechPlaying] = useState(false);
  const [activeSpeechEngine, setActiveSpeechEngine] =
    useState<SpeechEngine | null>(null);
  const speechStatusRequestRef = useRef(0);
  const downloadQueueRef = useRef<SpeechEngine[]>([]);
  const [providerActivity, setProviderActivityMap] =
    useState<AgentProviderActivityMap>({ codex: "idle", claude: "idle" });
  const [providerLastConnect, setProviderLastConnect] = useState<AgentProviderLastConnectMap>({});
  const [agentsActivity, setAgentsActivity] = useState<"idle" | "pausing" | "resuming">("idle");
  const [providerResults, setProviderResults] =
    useState<AgentProviderResultMap>({ codex: null, claude: null });

  const refreshSpeechStatus = useCallback(async (): Promise<SpeechStatus | null> => {
    const requestId = ++speechStatusRequestRef.current;
    try {
      const status = await getSpeechStatus();
      if (requestId !== speechStatusRequestRef.current) return null;
      setSpeechStatus(status);
      setActiveSpeechEngine(status.active_engine);
      setSpeechPlaying(status.active_engine !== null);
      return status;
    } catch {
      // Keep the last authoritative snapshot while the local backend reconnects.
      return null;
    }
  }, []);

  useEffect(() => {
    void refreshSpeechStatus();
  }, [refreshSpeechStatus]);

  // Config is saved on a 500 ms debounce. Refresh after that boundary so the
  // resolved readout never invents a client-side RAM or health decision.
  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refreshSpeechStatus();
    }, 650);
    return () => window.clearTimeout(timer);
  }, [config.agent_speech_engine, refreshSpeechStatus]);

  const providerStateRef = useRef({ config, results: providerResults, activity: providerActivity });
  providerStateRef.current = { config, results: providerResults, activity: providerActivity };
  const scheduleProviderPollRef = useRef<() => void>(() => {});
  const cancelProviderReadRef = useRef<(provider: CodingAgentProvider) => void>(() => {});
  useEffect(() => {
    let cancelled = false;
    let running = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // One in-flight read per provider. They run in parallel: a slow Codex
    // probe must not hold back Claude's row.
    const owners: Partial<Record<CodingAgentProvider, AbortController>> = {};
    // Rows waiting on the user update on their own. Polling stops while
    // Agents is paused: a paused provider has nothing to resolve.
    const unresolved = (provider: CodingAgentProvider) => {
      const state = providerStateRef.current;
      // A row with no status yet reads as loading and cannot Connect, so keep
      // asking until the first read lands, remembered provider or not.
      if (state.activity[provider] === "idle" && !state.results[provider]) return true;
      const remembered = provider === "codex" ? state.config.codex_hud_enabled : state.config.claude_hud_enabled;
      if (!remembered || !readAgentsEnabled(state.config) || state.activity[provider] !== "idle") return false;
      const row = deriveProviderRow(provider, { agentsEnabled: true, remembered: true, activity: "idle", result: state.results[provider] });
      return row.state !== "connected" || row.unconfirmed === true;
    };
    const needsPolling = () => (["codex", "claude"] as const).some(unresolved);
    const refresh = async (all = true) => {
      if (running || cancelled) return;
      running = true;
      clearTimeout(timer);
      timer = undefined;
      const read = async (provider: CodingAgentProvider) => {
        const owner = new AbortController();
        owners[provider] = owner;
        // A cold status read can take over 10 s (engine probes), so the
        // first one gets room; later polls keep the short budget.
        const budget = providerStateRef.current.results[provider] ? STATUS_POLL_TIMEOUT_MS : FIRST_STATUS_TIMEOUT_MS;
        const timeout = setTimeout(() => owner.abort(), budget);
        try {
          const result = await (provider === "codex" ? getCodexHudStatus(false, owner.signal) : getClaudeHudStatus(false, owner.signal));
          if (!cancelled && !owner.signal.aborted && providerStateRef.current.activity[provider] === "idle") {
            setProviderResults((previous) => ({ ...previous, [provider]: result }));
            providerStateRef.current.results = { ...providerStateRef.current.results, [provider]: result };
          }
        } catch { /* Preserve the current result while transport recovers. */ }
        finally {
          clearTimeout(timeout);
          if (owners[provider] === owner) delete owners[provider];
        }
      };
      try {
        const due = (["codex", "claude"] as const).filter(
          (provider) => providerStateRef.current.activity[provider] === "idle" && (all || unresolved(provider)),
        );
        await Promise.all(due.map(read));
      } finally {
        running = false;
        schedule();
      }
    };
    const schedule = () => {
      clearTimeout(timer);
      timer = undefined;
      if (!cancelled && !running && needsPolling()) timer = setTimeout(() => void refresh(false), 5000);
    };
    const cancelRead = (provider: CodingAgentProvider) => {
      owners[provider]?.abort();
    };
    scheduleProviderPollRef.current = schedule;
    cancelProviderReadRef.current = cancelRead;
    const onFocus = () => { void refresh(); };
    window.addEventListener("focus", onFocus);
    const unsubscribe = subscribe((event) => {
      if ((event.type === "agent.completion" || event.type === "agent.sessions") && needsPolling() && !running && timer === undefined) {
        timer = setTimeout(() => void refresh(false), 5000);
      }
    });
    void refresh();
    return () => {
      cancelled = true;
      for (const owner of Object.values(owners)) owner?.abort();
      clearTimeout(timer);
      unsubscribe();
      window.removeEventListener("focus", onFocus);
      if (scheduleProviderPollRef.current === schedule) scheduleProviderPollRef.current = () => {};
      if (cancelProviderReadRef.current === cancelRead) cancelProviderReadRef.current = () => {};
    };
  }, [connected, subscribe]);

  useEffect(() => {
    scheduleProviderPollRef.current();
  }, [config.codex_hud_enabled, config.claude_hud_enabled, config.agents_enabled, providerActivity.codex, providerActivity.claude]);

  const markSpeechDownloadFailed = useCallback((engine: SpeechEngine) => {
    downloadQueueRef.current = [];
    setSpeechStatus((previous) =>
      previous
        ? {
            ...previous,
            current_download: null,
            downloading: false,
            engines: {
              ...previous.engines,
              [engine]: {
                ...previous.engines[engine],
                downloading: false,
              },
            },
          }
        : previous,
    );
  }, []);

  const clearSpeechDownload = useCallback((engine: SpeechEngine) => {
    downloadQueueRef.current = [];
    setSpeechStatus((previous) =>
      previous
        ? {
            ...previous,
            current_download: null,
            downloading: false,
            engines: {
              ...previous.engines,
              [engine]: {
                ...previous.engines[engine],
                downloading: false,
              },
            },
          }
        : previous,
    );
  }, []);

  const launchNextDownload = useCallback(async (): Promise<void> => {
    const engine = downloadQueueRef.current[0];
    if (!engine) return;
    setSpeechStatus((previous) =>
      previous
        ? {
            ...previous,
            current_download: engine,
            downloading: true,
            engines: {
              ...previous.engines,
              [engine]: {
                ...previous.engines[engine],
                downloading: true,
              },
            },
          }
        : previous,
    );
    try {
      await startSpeechModelDownload(engine);
    } catch (error) {
      // A just-completed archive may hold the shared mutex for a final event
      // loop tick. Retry that queue transition once the worker has unwound.
      if (error instanceof Error && error.message.startsWith("409")) {
        await new Promise<void>((resolve) =>
          window.setTimeout(resolve, 300),
        );
        if (downloadQueueRef.current[0] !== engine) return;
        try {
          await startSpeechModelDownload(engine);
          return;
        } catch (retryError) {
          markSpeechDownloadFailed(engine);
          throw retryError;
        }
      }
      markSpeechDownloadFailed(engine);
      throw error;
    }
  }, [markSpeechDownloadFailed]);

  const beginSpeechDownloads = useCallback(
    (engines: SpeechEngine[]): Promise<void> => {
      const ordered = Array.from(new Set(engines)).sort(
        (left, right) =>
          (left === "supertonic" ? 0 : 1) -
          (right === "supertonic" ? 0 : 1),
      );
      if (downloadQueueRef.current.length > 0) {
        downloadQueueRef.current = Array.from(
          new Set([...downloadQueueRef.current, ...ordered]),
        );
        return Promise.resolve();
      }
      downloadQueueRef.current = ordered;
      return launchNextDownload();
    },
    [launchNextDownload],
  );

  useEffect(() => {
    return subscribeToDownloadEvents(
      subscribe,
      (event) => {
        const engine =
          event.engine ?? speechStatus?.current_download ?? "supertonic";
        if (event.type === "download.progress") {
          setSpeechStatus((previous) =>
            previous
              ? {
                  ...previous,
                  current_download: engine,
                  downloading: true,
                  engines: {
                    ...previous.engines,
                    [engine]: {
                      ...previous.engines[engine],
                      downloading: true,
                    },
                  },
                }
              : previous,
          );
        } else if (event.type === "download.complete") {
          setSpeechStatus((previous) =>
            previous
              ? {
                  ...previous,
                  current_download: null,
                  downloading: false,
                  recommended_downloads:
                    previous.recommended_downloads.filter(
                      (pending) => pending !== engine,
                    ),
                  engines: {
                    ...previous.engines,
                    [engine]: {
                      ...previous.engines[engine],
                      installed: true,
                      downloading: false,
                    },
                  },
                }
              : previous,
          );
          downloadQueueRef.current = downloadQueueRef.current.filter(
            (pending) => pending !== engine,
          );
          void refreshSpeechStatus();
          if (downloadQueueRef.current.length > 0) {
            window.setTimeout(() => {
              void launchNextDownload().catch(() => {});
            }, 300);
          }
        } else if (event.type === "download.error") {
          markSpeechDownloadFailed(engine);
          void refreshSpeechStatus();
        } else if (event.type === "download.cancelled") {
          clearSpeechDownload(engine);
          void refreshSpeechStatus();
        }
      },
      "tts",
    );
  }, [
    launchNextDownload,
    markSpeechDownloadFailed,
    clearSpeechDownload,
    refreshSpeechStatus,
    speechStatus?.current_download,
    subscribe,
  ]);

  useEffect(() => {
    return subscribe((event) => {
      if (event.type === "speech.state") {
        const speaking = event.state === "speaking";
        const concreteEngine =
          event.engine === "kokoro" || event.engine === "supertonic"
            ? event.engine
            : null;
        setSpeechPlaying(speaking);
        setActiveSpeechEngine(speaking ? concreteEngine : null);
        setSpeechStatus((previous) => {
          if (!previous) return previous;
          if (!speaking) {
            return { ...previous, active_engine: null };
          }
          return concreteEngine
            ? { ...previous, active_engine: concreteEngine }
            : previous;
        });
        if (concreteEngine || event.state === "idle") {
          void refreshSpeechStatus();
        }
      } else if (event.type === "speech.engine_health") {
        void refreshSpeechStatus();
      }
    });
  }, [refreshSpeechStatus, subscribe]);

  function handleSpeechEnabledChange(checked: boolean) {
    onConfigChange({ agent_speech_enabled: checked });
  }

  const setProviderActivity = (provider: CodingAgentProvider, activity: ProviderActivity) => {
    if (activity !== "idle") cancelProviderReadRef.current(provider);
    setProviderActivityMap((previous) => ({ ...previous, [provider]: activity }));
  };

  const mergeResult = (
    previous: AgentProviderResultMap,
    provider: CodingAgentProvider,
    result: AgentProviderResultMap[CodingAgentProvider],
  ): AgentProviderResultMap => ({
    ...previous,
    // Lifecycle receipts may omit the independent installation probe. Keep
    // that evidence so Disconnect still leaves a usable Connect action.
    [provider]: result ? { ...result,
      compatibility: result.compatibility ?? previous[provider]?.compatibility,
    } : null,
  });

  const setProviderResult = (
    provider: CodingAgentProvider,
    result: AgentProviderResultMap[CodingAgentProvider],
  ) => {
    providerStateRef.current.results = mergeResult(providerStateRef.current.results, provider, result);
    setProviderResults((previous) => mergeResult(previous, provider, result));
  };

  const providerConfigPatch = (
    provider: CodingAgentProvider,
    enabled: boolean,
  ): Partial<AppConfig> =>
    provider === "codex"
      ? { codex_hud_enabled: enabled }
      : { claude_hud_enabled: enabled };

  async function commitProviderConfig(patch: Partial<AppConfig>) {
    if (onCommitConfig) await onCommitConfig(patch);
    else { await saveConfig(patch); onConfigChange(patch); }
  }

  const enableProvider = (provider: CodingAgentProvider, force: boolean) =>
    provider === "codex" ? enableCodexHud({ force }) : enableClaudeHud({ force });
  const readProviderStatus = (provider: CodingAgentProvider) =>
    provider === "codex" ? getCodexHudStatus() : getClaudeHudStatus();

  // A connected row no longer needs the "Installed." receipt. Dropping it
  // here keeps a later re-trust (after an update) on the neutral line.
  const rows = agentRows(config, providerResults, providerActivity, providerLastConnect);
  useEffect(() => {
    const codexDone = rows.codex.state === "connected" && Boolean(providerLastConnect.codex);
    const claudeDone = rows.claude.state === "connected" && Boolean(providerLastConnect.claude);
    if (codexDone || claudeDone) {
      setProviderLastConnect((previous) => ({
        codex: codexDone ? null : previous.codex,
        claude: claudeDone ? null : previous.claude,
      }));
    }
  }, [rows.codex.state, rows.claude.state, providerLastConnect.codex, providerLastConnect.claude]);

  /** Turning Agents on resumes every remembered provider without a forced
   *  reinstall, then reports whether any of them now needs the user. */
  async function resumeAgents(): Promise<{ needsAction: boolean }> {
    const remembered = (["codex", "claude"] as const).filter((provider) =>
      provider === "codex" ? Boolean(config.codex_hud_enabled) : config.claude_hud_enabled);
    setAgentsActivity("resuming");
    setProviderLastConnect({});
    remembered.forEach((provider) => setProviderActivity(provider, "resuming"));
    try {
      await commitProviderConfig({ agents_enabled: true });
      await Promise.allSettled(remembered.map(async (provider) => {
        setProviderResult(provider, await enableProvider(provider, false));
      }));
      const statuses = await Promise.allSettled([readProviderStatus("codex"), readProviderStatus("claude")]);
      statuses.forEach((outcome, index) => {
        if (outcome.status === "fulfilled") setProviderResult(index === 0 ? "codex" : "claude", outcome.value);
      });
      const settled = agentRows({ ...config, agents_enabled: true }, providerStateRef.current.results);
      return { needsAction: remembered.some((provider) => rowNeedsUser(settled[provider])) };
    } catch {
      onShowStatus("Couldn't turn on Agents. Try again.", true);
      return { needsAction: false };
    } finally {
      remembered.forEach((provider) => setProviderActivity(provider, "idle"));
      setAgentsActivity("idle");
    }
  }

  async function handleAgentsToggle(checked: boolean): Promise<{ needsAction: boolean }> {
    if (checked) return resumeAgents();
    setAgentsActivity("pausing");
    try {
      await commitProviderConfig({ agents_enabled: false });
      setProviderLastConnect({});
    } catch {
      onShowStatus("Couldn't pause Agents. Try again.", true);
    } finally { setAgentsActivity("idle"); }
    return { needsAction: false };
  }

  async function handleProviderToggle(provider: CodingAgentProvider, checked: boolean) {
    const label = AGENT_PROVIDER_LABEL[provider];
    if (checked) {
      // A connected but paused agent would be a contradiction, so Connect
      // while paused turns Agents on first.
      if (!readAgentsEnabled(config)) await resumeAgents();
      setProviderActivity(provider, "connecting");
      try {
        // Connect always installs fresh; startup and resume never force.
        const result = await enableProvider(provider, true);
        setProviderResult(provider, result);
        const active = result.ok && result.status !== "failed";
        setProviderLastConnect((previous) => ({ ...previous, [provider]: active ? "installed" : "failed" }));
        if (active) await commitProviderConfig(providerConfigPatch(provider, true));
        // Enable returns an install receipt. Read authoritative connection state.
        setProviderResult(provider, await readProviderStatus(provider));
      } catch {
        onShowStatus(`Couldn't connect ${label}. Try again.`, true);
      } finally { setProviderActivity(provider, "idle"); }
      return;
    }
    setProviderActivity(provider, "disconnecting");
    try {
      // Disconnect fully uninstalls; the recovery path is Disconnect, then Connect.
      const result = await (provider === "codex" ? disableCodexHud() : disableClaudeHud());
      await commitProviderConfig(providerConfigPatch(provider, false));
      setProviderResult(provider, result);
      setProviderLastConnect((previous) => ({ ...previous, [provider]: null }));
    } catch {
      onShowStatus(`Couldn't disconnect ${label}. Try again.`, true);
    } finally { setProviderActivity(provider, "idle"); }
  }

  async function handleProviderRecheck() {
    cancelProviderReadRef.current("codex");
    cancelProviderReadRef.current("claude");
    setProviderActivityMap({ codex: "checking", claude: "checking" });
    const outcomes = await Promise.allSettled([getCodexHudStatus(true), getClaudeHudStatus(true)]);
    outcomes.forEach((outcome, index) => {
      if (outcome.status === "fulfilled") setProviderResult(index === 0 ? "codex" : "claude", outcome.value);
    });
    const failed = outcomes.some((outcome) => outcome.status === "rejected");
    onShowStatus(failed ? "Couldn't check every connection. Try again." : "Connections checked.", failed);
    setProviderActivityMap({ codex: "idle", claude: "idle" });
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-[18px] font-medium text-slate mb-1">Agents</h2>
        <p className="text-[12px] text-steel leading-relaxed">Follow your coding agents and reply by voice or text.</p>
      </div>
      {!config.agent_speak_setup_completed ? (
        <section
          aria-labelledby={`${introId}-heading`}
          className={`rounded-[var(--radius-row)] border px-5 shadow-glass-sm ${hasConfiguredProvider ? "py-4" : "py-5"}`}
          style={{
            background: "var(--surface-control)",
            borderColor: "var(--border-emphasis)",
          }}
        >
          <div className={hasConfiguredProvider ? "flex flex-wrap items-center justify-between gap-4" : ""}>
            <div className="min-w-0">
              <div className="flex items-center gap-2.5">
                {!hasConfiguredProvider && (
                  <svg
                    aria-hidden="true"
                    className="h-5 w-5 shrink-0 text-frost-deep"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M4 4h11a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H9l-4 3v-3H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z" />
                    <path d="M11 18h5l4 3v-3a2 2 0 0 0 2-2V9" />
                  </svg>
                )}
                <h3
                  id={`${introId}-heading`}
                  className={`font-medium text-slate ${hasConfiguredProvider ? "text-[13px]" : "text-[15px]"}`}
                >
                  {hasConfiguredProvider ? "Continue setup" : "Read, listen, and reply"}
                </h3>
              </div>
              <p className={`text-[12px] leading-relaxed text-steel ${hasConfiguredProvider ? "mt-1" : "mt-2"}`}>
                {hasConfiguredProvider
                  ? "Review your connections and finish setting up Agents."
                  : "Hear or read responses from Claude Code and Codex while you work in other apps. Speak or type to reply."}
              </p>
            </div>
            <div className={`flex flex-wrap items-center gap-3 ${hasConfiguredProvider ? "" : "mt-4"}`}>
              <PrimaryButton
                type="button"
                className="inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap px-4 py-2 text-[12px]"
                disabled={!connected}
                aria-describedby={!connected ? `${introId}-offline` : !hasConfiguredProvider ? `${introId}-helper` : undefined}
                onClick={() => setSetupOpen(true)}
              >
                {hasConfiguredProvider ? "Continue setup" : "Enable Agents"}
                <svg aria-hidden="true" width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d="m6 3 5 5-5 5" />
                </svg>
              </PrimaryButton>
              {!hasConfiguredProvider && (
                <p id={`${introId}-helper`} className="text-[11px] leading-relaxed text-steel">
                  Choose your agents, voice, and reply shortcut.
                </p>
              )}
            </div>
          </div>
          {!connected && (
            <p id={`${introId}-offline`} role="status" className="mt-3 text-[11px] leading-relaxed text-steel">
              Whisperstream is reconnecting. Setup will be available shortly.
            </p>
          )}
        </section>
      ) : (
        <HudConfigModal
          open={true}
          embedded
          onClose={() => {}}
          config={config}
          onConfigChange={onConfigChange}
          connected={connected}
          send={send}
          onProviderToggle={handleProviderToggle}
          onProviderRecheck={handleProviderRecheck}
          providerActivity={providerActivity}
          providerLastConnect={providerLastConnect}
          providerResults={providerResults}
          onAgentsToggle={handleAgentsToggle}
          agentsActivity={agentsActivity}
          speechStatus={speechStatus}
          activeSpeechEngine={activeSpeechEngine}
          speechPlaying={speechPlaying}
          subscribe={subscribe}
          onSpeechEnabledChange={handleSpeechEnabledChange}
          onRefreshSpeechStatus={refreshSpeechStatus}
          onDownload={beginSpeechDownloads}
          onCancelDownload={async () => {
            await cancelDownload();
          }}
          />
      )}

      {/* The modal saves completion before onFinish. Failed saves leave it open;
          an unfinished close leaves the introduction as the only page content. */}
      <AgentSpeakSetupModal
        open={setupOpen}
        onClose={() => setSetupOpen(false)}
        onFinish={(saved) => {
          setSetupOpen(false);
          onConfigChange({
            agent_speak_setup_completed: saved.agent_speak_setup_completed,
            agent_summary_text_enabled: saved.agent_summary_text_enabled,
            agent_speech_enabled: saved.agent_speech_enabled,
            claude_reply_grace_seconds: saved.claude_reply_grace_seconds,
          });
        }}
        config={config}
        providerActivity={providerActivity}
        providerLastConnect={providerLastConnect}
        providerResults={providerResults}
        onProviderToggle={handleProviderToggle}
        onResumeAgents={() => void resumeAgents()}
        onConfigChange={onConfigChange}
        connected={connected}
        send={send}
        subscribe={subscribe}
        speechStatus={speechStatus}
        activeSpeechEngine={activeSpeechEngine}
        speechPlaying={speechPlaying}
        onRefreshSpeechStatus={refreshSpeechStatus}
        onDownload={beginSpeechDownloads}
        onCancelDownload={async () => {
          await cancelDownload();
        }}
      />

    </div>
  );
}
