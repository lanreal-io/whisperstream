import { useEffect, useRef, useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { useReducedMotion } from "framer-motion";
import { PrimaryButton } from "../PrimaryButton";
import { SecondaryButton } from "../SecondaryButton";
import { StatusBead, type BeadTone } from "../StatusLight";
import type { ClaudePluginActionResult, CodexPluginActionResult, CodingAgentProvider, ManagedCliActionResult } from "../../lib/api/agents";
import { getManagedCliStatus, installManagedCli, uninstallManagedCli } from "../../lib/api/agents";
import type {
  AgentProviderActivityMap, AgentProviderRowMap, ProviderActivity, ProviderFailure, ProviderRowModel, ProviderRowState,
} from "./agentCompatibility";

export const AGENT_PROVIDER_ORDER = ["codex", "claude"] as const satisfies readonly CodingAgentProvider[];

export const AGENT_PROVIDER_LABEL: Record<CodingAgentProvider, string> = {
  codex: "Codex",
  claude: "Claude Code",
};

export interface AgentProviderConnectionMap {
  codex: boolean;
  claude: boolean;
}

export interface AgentProviderResultMap {
  codex: CodexPluginActionResult | null;
  claude: ClaudePluginActionResult | null;
}

/** One connected provider is enough to continue setup. */
export function setupPluginBlockReason(rows: AgentProviderRowMap): string | null {
  const values = AGENT_PROVIDER_ORDER.map((provider) => rows[provider]);
  if (values.some((row) => row.state === "connected")) return null;
  return values.some((row) => row.state === "waiting" || row.state === "connecting" || row.state === "resuming")
    ? "Finish connecting one agent to continue."
    : "Connect one agent to continue.";
}

const STEEL_BORDER = "1px solid rgba(107,130,153,0.28)";
const BUTTON = "inline-flex items-center gap-[7px] whitespace-nowrap rounded-full px-3.5 py-1 text-[12px]";
const SECONDARY = `${BUTTON} font-medium text-slate disabled:opacity-45`;

const TONE: Record<ProviderRowState, BeadTone> = {
  not_connected: "idle", paused: "idle", connecting: "busy", resuming: "busy",
  waiting: "attention", update_needed: "attention", connected: "ready", failed: "error",
};

function stateWord(provider: CodingAgentProvider, state: ProviderRowState): string | null {
  switch (state) {
    case "not_connected": return null;
    case "connecting": return "Connecting";
    case "waiting": return provider === "claude" ? "Almost ready" : "Trust needed";
    case "connected": return "Connected";
    case "paused": return "Paused";
    case "resuming": return "Resuming";
    case "update_needed": return "Update needed";
    case "failed": return "Couldn't connect";
  }
}

const FAILURE_LINE: Record<CodingAgentProvider, Record<ProviderFailure, string>> = {
  claude: {
    cliUnavailable: "Claude Code isn't installed. Install it, then try again.",
    listFailed: "Claude Code didn't respond. Open it once, then try again.",
    installFailed: "The plugin didn't install. Check Claude Code's plugin settings, then try again.",
    unsupported: "Couldn't reach Claude Code. Make sure it's installed, then try again.",
    other: "Couldn't reach Claude Code. Make sure it's installed, then try again.",
  },
  codex: {
    cliUnavailable: "Codex isn't installed. Install it, then try again.",
    listFailed: "Codex didn't accept the plugin. Check its plugin settings, then try again.",
    installFailed: "Codex didn't accept the plugin. Check its plugin settings, then try again.",
    unsupported: "This Codex version can't add plugins. Update Codex, then try again.",
    other: "Codex didn't accept the plugin. Check its plugin settings, then try again.",
  },
};

function RowSpinner() {
  const reduce = useReducedMotion();
  return (
    <svg width="11" height="11" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.75"
      strokeLinecap="round" aria-hidden="true" className={reduce ? undefined : "animate-spin"}>
      <path d="M7 1.5a5.5 5.5 0 015.5 5.5" />
    </svg>
  );
}

/** A text-only pill that steps back behind the row's instruction. */
function QuietButton({ className = "", ...rest }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      {...rest}
      className={`inline-flex cursor-pointer items-center gap-[7px] whitespace-nowrap rounded-full border border-transparent px-2.5 py-1 text-[12px] text-steel transition-colors duration-150 hover:bg-[var(--surface-hover)] hover:text-slate disabled:cursor-default disabled:opacity-45 disabled:hover:bg-transparent disabled:hover:text-steel ${className}`}
    />
  );
}

/** An inline command with a Copy action. The visible command stays usable
 *  when no clipboard bridge exists. */
function CommandChip({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const copyCommand = async () => {
    try { await navigator.clipboard.writeText(command); } catch { /* leave the visible command */ }
    setCopied(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 1400);
  };
  return (
    <span
      className="inline-flex items-center gap-0.5 whitespace-nowrap rounded-full py-px pl-2.5 pr-[3px] align-[1px] font-mono text-[11.5px] leading-[18px] text-slate"
      // The steel edge keeps the chip legible on the white setup modal, where
      // a hairline over --surface-control composites to nothing.
      style={{ background: "var(--surface-control)", border: STEEL_BORDER }}
    >
      {command}
      <button
        type="button"
        onClick={() => void copyCommand()}
        aria-label={`Copy ${command}`}
        className="cursor-pointer rounded-full px-2 font-body text-[11px] leading-[18px] text-steel transition-colors hover:bg-[var(--surface-hover)] hover:text-slate"
      >
        {copied ? "Copied" : "Copy"}
      </button>
      <span className="sr-only" role="status" aria-live="polite">{copied ? "Copied" : ""}</span>
    </span>
  );
}

/** "Settings > Hooks > Whisperstream" reads as "greater than" in some readers. */
function TrustPath() {
  return (
    <span className="whitespace-nowrap font-medium text-slate" aria-label="Settings, Hooks, Whisperstream">
      Settings &gt; Hooks &gt; Whisperstream
    </span>
  );
}

function prefixed(row: ProviderRowModel, text: ReactNode): ReactNode {
  return row.installedPrefix ? <>Installed. {text}</> : text;
}

function rowSentence(provider: CodingAgentProvider, row: ProviderRowModel): ReactNode {
  const name = AGENT_PROVIDER_LABEL[provider];
  switch (row.state) {
    case "not_connected":
      if (row.detectionFailed) return `Couldn't check ${name} installations. Check again.`;
      if (row.missing) return `${name} isn't installed. Install it, then check again.`;
      return `See and hear ${name} replies in the overlay.`;
    case "connecting":
      return "Installing the Whisperstream plugin...";
    case "waiting":
      if (provider === "claude") return prefixed(row, "Start a new Claude Code session, or run this in an open one:");
      if (row.codexRoutes === "app") {
        return prefixed(row, <>In the Codex app, open <TrustPath />, then <span className="whitespace-nowrap font-medium text-slate">Trust all</span>.</>);
      }
      if (row.codexRoutes === "cli") {
        return prefixed(row, <>In the Codex CLI, run <CommandChip command="/hooks" />, then trust Whisperstream.</>);
      }
      return prefixed(row, "Trust Whisperstream once, in either place:");
    case "connected":
      return `${name} replies show in the overlay, from the app or terminal.`;
    case "paused":
      return provider === "claude"
        ? "Paused with Agents. Your connection is kept."
        : "Paused with Agents. Your connection and hook trust are kept.";
    case "resuming":
      return `Resuming ${name}...`;
    case "update_needed": {
      const found = <span className="font-medium text-slate">{name} {row.blocking?.version}</span>;
      return provider === "claude"
        ? <>{found} is too old. Update to {row.blocking?.minimum} or later, then start a new session.</>
        : <>{found} is too old. Update to {row.blocking?.minimum} or later, then reopen Codex.</>;
    }
    case "failed":
      return FAILURE_LINE[provider][row.failure ?? "other"];
  }
}

function NextStep({ provider, row }: { provider: CodingAgentProvider; row: ProviderRowModel }) {
  return (
    <>
      {provider === "claude" && (
        <div className="mt-1.5 flex flex-wrap items-center gap-2">
          <CommandChip command="/reload-plugins" />
        </div>
      )}
      {provider === "codex" && row.codexRoutes === "both" && (
        <div className="mt-1.5 grid max-w-[60ch] grid-cols-[auto_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1.5">
          <span className="whitespace-nowrap text-[11px] font-medium text-steel">Codex app</span>
          <span className="min-w-0 text-[12px] leading-relaxed text-steel">
            Open <TrustPath />, then <span className="whitespace-nowrap font-medium text-slate">Trust all</span>.
          </span>
          <span className="whitespace-nowrap text-[11px] font-medium text-steel">Codex CLI</span>
          <span className="min-w-0 text-[12px] leading-relaxed text-steel">
            Run <CommandChip command="/hooks" />, then trust Whisperstream.
          </span>
        </div>
      )}
      <p className="mt-1.5 text-[11px] text-steel/80">This row updates on its own.</p>
    </>
  );
}

interface RowHandlers {
  onConnect: (provider: CodingAgentProvider) => void;
  onDisconnect: (provider: CodingAgentProvider) => void;
  onResume?: () => void;
  onCheck?: () => void;
}

function RowAction({ provider, row, activity, ...on }: { provider: CodingAgentProvider; row: ProviderRowModel; activity: ProviderActivity } & RowHandlers) {
  const name = AGENT_PROVIDER_LABEL[provider];
  const disconnecting = activity === "disconnecting";
  switch (row.state) {
    case "not_connected":
      return (
        <PrimaryButton type="button" className={BUTTON} disabled={!row.connectable || activity !== "idle"}
          aria-label={`Connect ${name}`} onClick={() => on.onConnect(provider)}>Connect</PrimaryButton>
      );
    case "failed":
      return (
        <PrimaryButton type="button" className={BUTTON} disabled={activity !== "idle"}
          aria-label={`Try connecting ${name} again`} onClick={() => on.onConnect(provider)}>Try again</PrimaryButton>
      );
    case "connecting":
      return (
        <PrimaryButton type="button" className={BUTTON} loading aria-label={`Connecting ${name}`}>
          <RowSpinner />Connecting...
        </PrimaryButton>
      );
    case "waiting":
      return (
        <QuietButton aria-label={`Disconnect ${name}`} disabled={disconnecting} onClick={() => on.onDisconnect(provider)}>
          {disconnecting && <RowSpinner />}Disconnect
        </QuietButton>
      );
    case "connected":
      return (
        <SecondaryButton className={SECONDARY} style={{ border: STEEL_BORDER }} disabled={disconnecting}
          aria-label={`Disconnect ${name}`} onClick={() => on.onDisconnect(provider)}>
          {disconnecting && <RowSpinner />}Disconnect
        </SecondaryButton>
      );
    case "paused":
    case "resuming": {
      const resuming = row.state === "resuming";
      return (
        <SecondaryButton className={SECONDARY} style={{ border: STEEL_BORDER }} disabled={resuming || !on.onResume}
          aria-label="Resume Agents" onClick={on.onResume}>
          {resuming && <RowSpinner />}{resuming ? "Resuming..." : "Resume"}
        </SecondaryButton>
      );
    }
    case "update_needed": {
      const checking = activity === "checking";
      return (
        <SecondaryButton className={SECONDARY} style={{ border: STEEL_BORDER }} disabled={checking || !on.onCheck}
          aria-label={`Check ${name} again`} onClick={on.onCheck}>
          {checking && <RowSpinner />}{checking ? "Checking..." : "Check again"}
        </SecondaryButton>
      );
    }
  }
}

function RowFrame({ provider, tone, word, sentence, sentenceRole = "status", next, action, density, dimmed = false }: {
  provider: string;
  tone: BeadTone;
  /** Connected on trust, not yet confirmed this launch: the bead and word
   *  sit at reduced strength until the first check answers. */
  dimmed?: boolean;
  word: string | null;
  sentence: ReactNode;
  sentenceRole?: "status" | "alert";
  next?: ReactNode;
  action: ReactNode;
  density: "setup" | "configure";
}) {
  return (
    <div role="listitem" data-agent-provider={provider}
      className={`grid grid-cols-[18px_minmax(0,1fr)_auto] items-start gap-x-2.5 border-t border-[var(--divider)] first:border-t-0 ${density === "setup" ? "py-3" : "py-3.5"}`}>
      <span data-unconfirmed={dimmed ? "true" : undefined} className="transition-opacity duration-300" style={{ opacity: dimmed ? 0.45 : 1 }}>
        <StatusBead tone={tone} />
      </span>
      <div className="flex min-w-0 flex-col gap-[3px]">
        <div className="flex min-h-[18px] flex-wrap items-baseline gap-x-2.5 gap-y-1">
          <span className="text-[13px] font-medium leading-[18px] text-slate">{AGENT_PROVIDER_DISPLAY[provider] ?? provider}</span>
          {/* The left bead is the row's bullet and carries the tone, so the
              word sits in a dotless hairline pill in secondary text, the same
              family as the section's StatusLight (owner, 2026-10-25). */}
          {word && <span data-provider-word-tone={tone} className="self-center rounded-full border border-[var(--border-hairline-steel)] px-2 py-px text-[10.5px] leading-4 text-steel transition-opacity duration-300" style={{ opacity: dimmed ? 0.55 : 1 }}>{word}</span>}
        </div>
        <p role={sentenceRole} className="max-w-[60ch] text-[12px] leading-relaxed text-steel">{sentence}</p>
        {next}
      </div>
      <div className="flex min-h-[18px] items-center">{action}</div>
    </div>
  );
}

const AGENT_PROVIDER_DISPLAY: Record<string, string> = { ...AGENT_PROVIDER_LABEL, "managed-cli": "Whisperstream CLI" };

function ProviderRow({ provider, row, activity, density, ...handlers }: {
  provider: CodingAgentProvider;
  row: ProviderRowModel;
  activity: ProviderActivity;
  density: "setup" | "configure";
} & RowHandlers) {
  return (
    <RowFrame
      provider={provider}
      tone={TONE[row.state]}
      dimmed={row.unconfirmed === true}
      word={stateWord(provider, row.state)}
      sentence={rowSentence(provider, row)}
      sentenceRole={row.state === "failed" ? "alert" : "status"}
      next={row.state === "waiting" ? <NextStep provider={provider} row={row} /> : undefined}
      action={<RowAction provider={provider} row={row} activity={activity} {...handlers} />}
      density={density}
    />
  );
}

function ManagedCliInstallRow({ connected }: { connected: AgentProviderConnectionMap }) {
  const [result, setResult] = useState<ManagedCliActionResult | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getManagedCliStatus()
      .then((next) => { if (!cancelled) setResult(next); })
      .catch(() => {
        if (!cancelled) setResult({ ok: false, status: "failed", available: false, installed: false });
      });
    return () => { cancelled = true; };
  }, []);

  const installed = result?.installed === true;
  const anyConnected = connected.codex || connected.claude;
  const installedGuidance = connected.codex
    ? connected.claude
      ? "run whisperstream codex or whisperstream claude to start a managed session."
      : "run whisperstream codex to start a managed session."
    : connected.claude
      ? "run whisperstream claude to start a managed session."
      : "connect a coding agent to start a managed session.";
  const failed = result?.status === "failed" || result?.status === "unavailable";
  const sentence = pending
    ? installed ? "Uninstalling the Whisperstream CLI..." : "Installing the Whisperstream CLI..."
    : result?.status === "failed"
      ? "Couldn't update your PATH. Try again."
      : result?.status === "unavailable"
        ? "The CLI isn't available in this build."
        : installed
          ? result?.restart_required
            ? `Open a new terminal, then ${installedGuidance}`
            : `${installedGuidance[0].toUpperCase()}${installedGuidance.slice(1)}`
          : "Install to reply anytime to sessions started from your terminal.";
  const tone: BeadTone = pending ? "busy" : failed ? "error" : installed && anyConnected ? "ready" : "idle";

  async function handleToggle() {
    setPending(true);
    try {
      setResult(installed ? await uninstallManagedCli() : await installManagedCli());
    } catch {
      setResult({ ok: false, status: "failed", available: result?.available ?? false, installed });
    } finally {
      setPending(false);
    }
  }

  return (
    <RowFrame
      provider="managed-cli"
      tone={tone}
      word={installed ? "Installed" : null}
      sentence={sentence}
      sentenceRole={failed && !pending ? "alert" : "status"}
      density="configure"
      action={
        <SecondaryButton className={SECONDARY} style={{ border: STEEL_BORDER }}
          disabled={pending || result === null || result.status === "unavailable"}
          aria-label={`${installed ? "Uninstall" : "Install"} Whisperstream CLI`}
          onClick={() => void handleToggle()}>
          {pending && <RowSpinner />}{installed ? "Uninstall" : "Install"}
        </SecondaryButton>
      }
    />
  );
}

export interface AgentProviderInstallListProps {
  rows: AgentProviderRowMap;
  activity: AgentProviderActivityMap;
  /** Connected providers, for the Whisperstream CLI row's guidance. */
  connected: AgentProviderConnectionMap;
  onToggle: (provider: CodingAgentProvider, checked: boolean) => void;
  onResume?: () => void;
  onRecheck?: () => void;
  density?: "setup" | "configure";
}

/** One transparent provider list. It owns no fill, border, radius, or shadow,
 *  so the page or setup modal remains the only visual ground. */
export function AgentProviderInstallList({
  rows,
  activity,
  connected,
  onToggle,
  onResume,
  onRecheck,
  density = "configure",
}: AgentProviderInstallListProps) {
  const checking = AGENT_PROVIDER_ORDER.some((provider) => activity[provider] === "checking");
  const handlers: RowHandlers = {
    onConnect: (provider) => onToggle(provider, true),
    onDisconnect: (provider) => onToggle(provider, false),
    onResume,
    onCheck: onRecheck,
  };
  return (
    <div>
      {/* Only the connection list scrolls in setup. It contains no hover
          tooltips, so the step frame and Continue tooltip stay unclipped. */}
      <div role="list" aria-label="Coding agent connections"
        className={`flex flex-col${density === "setup" ? " max-h-[280px] overflow-y-auto pr-1" : ""}`}>
        {AGENT_PROVIDER_ORDER.map((provider) => (
          <ProviderRow key={provider} provider={provider} row={rows[provider]} activity={activity[provider]}
            density={density} {...handlers} />
        ))}
        {density === "configure" && <ManagedCliInstallRow connected={connected} />}
      </div>
      {density === "configure" && (
        <div className="flex items-center justify-end border-t border-[var(--divider)] pt-2.5">
          <QuietButton disabled={checking || !onRecheck} onClick={onRecheck} aria-label="Check connections again">
            {checking && <RowSpinner />}{checking ? "Checking..." : "Check again"}
          </QuietButton>
        </div>
      )}
    </div>
  );
}
