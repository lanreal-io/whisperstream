import {
  agentRows, agentsEnabled, aggregateAgentsLight, IDLE_ACTIVITY,
  type AgentProviderActivityMap, type AgentProviderLastConnectMap,
} from "./agentCompatibility";
import { Fragment, useCallback, useEffect, useId, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { revealItemInDir } from "@tauri-apps/plugin-opener";

import { SecondaryButton } from "../SecondaryButton";
import { StatusLight } from "../StatusLight";
import {
  AGENT_PROVIDER_LABEL,
  AGENT_PROVIDER_ORDER,
  AgentProviderInstallList,
  type AgentProviderResultMap,
} from "./AgentProviderInstallList";
import { SetupStep } from "./SetupStep";
import { getSetupCodexPluginDetail, getSetupPluginDetail, type CodingAgentProvider, type SetupPluginDetail } from "../../lib/api/agents";
import type { SetupStepProps } from "./AgentSpeakSetupModal";

const STEEL_BORDER = "1px solid rgba(107,130,153,0.28)";
const HAIRLINE =
  "color-mix(in srgb, var(--color-steel) 22%, transparent)";

const HEADING = "Connect your coding agents";
const LEDE = "Connect Claude Code, Codex, or both to see and hear their replies.";
const REVEAL_LABEL = "Show in Explorer";
const DISCLOSURE_LABEL = "What Whisperstream added";
const DISABLED_REASON = "Available after a coding agent is connected.";

const EMPTY_DETAIL: SetupPluginDetail = {
  plugin_dir: null,
  hook_definition: null,
};

type SetupPluginDetailMap = Record<CodingAgentProvider, SetupPluginDetail>;

const ChevronGlyph = () => (
  <svg
    width="10"
    height="10"
    viewBox="0 0 10 10"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.5"
    strokeLinecap="round"
  >
    <path d="M3.5 2.5l3 2.5-3 2.5" />
  </svg>
);

/**
 * The reveal-in-Explorer affordance. A folder glyph replaces the former
 * "Show in Explorer" text pill: it is a fraction of the width, which is what
 * lets the real plugin path own the row instead of being collapsed to a useless
 * middle ellipsis. Matches `AddAppModal`'s `FolderGlyph` so the app keeps one
 * folder mark. The button still carries "Show in Explorer" as its accessible
 * name and hover title, so the icon is never mystery meat.
 */
const FolderGlyph = () => (
  <svg
    width="14"
    height="14"
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
 * Count the hook events in a setup-only receipt. The hook names are the keys of
 * the top-level `hooks` object; a Codex receipt also carries a sibling
 * `description`, so the root object is never counted. Returns null when the
 * receipt is missing, unparseable, or empty so the caller shows just the
 * provider name instead of ever printing "Added 0 hooks".
 */
function countHooks(hookDefinition: string | null): number | null {
  if (!hookDefinition) return null;
  try {
    const parsed: unknown = JSON.parse(hookDefinition);
    const hooks =
      parsed && typeof parsed === "object"
        ? (parsed as { hooks?: unknown }).hooks
        : undefined;
    if (!hooks || typeof hooks !== "object") return null;
    const count = Object.keys(hooks as Record<string, unknown>).length;
    return count > 0 ? count : null;
  } catch {
    return null;
  }
}

/**
 * The compact per-provider descriptor: what was added, count-led and
 * pluralized. It sits inline after the provider name so each provider reads as
 * one tight stanza. Returns null when the receipt is missing or unparseable so
 * the row shows just the provider name rather than a padded sentence or
 * "Added 0 hooks".
 */
function addedDescriptor(count: number | null): string | null {
  if (count === null) return null;
  const unit = count === 1 ? "hook" : "hooks";
  return `Added ${count} ${unit}`;
}

export interface SetupStepPluginProps extends SetupStepProps {
  providerActivity?: AgentProviderActivityMap;
  providerLastConnect?: AgentProviderLastConnectMap;
  providerResults: AgentProviderResultMap;
  onProviderToggle: (provider: CodingAgentProvider, checked: boolean) => void;
  /** A paused row's Resume turns Agents back on. */
  onResumeAgents?: () => void;
}

/**
 * Step 1 is one transparent connection rack on the modal's existing ground.
 * Each provider owns its status slot and actions; the small aggregate light
 * only answers whether the setup can continue.
 *
 * The "What Whisperstream added" disclosure is a compact transparency receipt:
 * one tight stanza per provider (name + "Added N hooks" + the real plugin
 * folder path + a folder-icon reveal). The full path IS shown, small, and wraps
 * rather than truncating, because it is the user's own path on their own
 * machine. The one privacy invariant is that the path is never LOGGED (see
 * `handleReveal`); it is not "never shown" (it is also the reveal button's
 * accessible name). Keeping every provider to one short stanza is also what
 * keeps the expanded step under the modal's no-scroll body ceiling, so the
 * receipt can no longer bleed over the Continue button.
 */
export function SetupStepPlugin({
  config,
  onCanAdvanceChange,
  onChildDialogOpenChange,
  providerActivity = IDLE_ACTIVITY,
  providerLastConnect = {},
  providerResults,
  onProviderToggle,
  onResumeAgents,
}: SetupStepPluginProps) {
  const rows = agentRows(config, providerResults, providerActivity, providerLastConnect);
  const providerConnected = {
    codex: rows.codex.state === "connected",
    claude: rows.claude.state === "connected",
  };
  const anyConnected = AGENT_PROVIDER_ORDER.some(
    (provider) => providerConnected[provider],
  );
  const [details, setDetails] = useState<SetupPluginDetailMap>({
    codex: EMPTY_DETAIL,
    claude: EMPTY_DETAIL,
  });
  const detailsRef = useRef(details);
  const [hooksOpen, setHooksOpen] = useState(false);
  const reasonId = `${useId()}-reveal-reason`;
  const revealId = `${useId()}-plugin-reveal`;

  useEffect(() => {
    onChildDialogOpenChange(false);
  }, [onChildDialogOpenChange]);

  useEffect(() => {
    onCanAdvanceChange(anyConnected);
  }, [anyConnected, onCanAdvanceChange]);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      getSetupCodexPluginDetail().catch(() => EMPTY_DETAIL),
      getSetupPluginDetail().catch(() => EMPTY_DETAIL),
    ]).then(([codex, claude]) => {
      if (cancelled) return;
      const previous = detailsRef.current;
      if (
        previous.codex.plugin_dir === codex.plugin_dir &&
        previous.codex.hook_definition === codex.hook_definition &&
        previous.claude.plugin_dir === claude.plugin_dir &&
        previous.claude.hook_definition === claude.hook_definition
      ) {
        return;
      }
      const next = { codex, claude };
      detailsRef.current = next;
      setDetails(next);
    });
    return () => {
      cancelled = true;
    };
  }, [
    providerConnected.codex,
    providerConnected.claude,
    providerResults.codex,
    providerResults.claude,
  ]);

  const handleReveal = useCallback(
    (provider: CodingAgentProvider) => {
      const pluginDir = details[provider].plugin_dir;
      if (!pluginDir) return;
      try {
        void revealItemInDir(pluginDir).catch(() => {
          // Never log paths from the privacy-sensitive setup receipt.
        });
      } catch {
        // Outside Tauri the opener bridge is absent.
      }
    },
    [details],
  );

  // The setup light is never a button: the rows are already in view.
  const light = aggregateAgentsLight(agentsEnabled(config), rows);

  return (
    <SetupStep
      heading={HEADING}
      lede={LEDE}
      headingAction={<StatusLight state={light.state} ariaLabel={light.ariaLabel} />}
    >
      <div className="flex flex-1 flex-col justify-center">
        <AgentProviderInstallList
          rows={rows}
          activity={providerActivity}
          connected={providerConnected}
          onToggle={onProviderToggle}
          onResume={onResumeAgents}
          density="setup"
        />
      </div>

      <span id={reasonId} className="sr-only">
        {DISABLED_REASON}
      </span>

      <div className="mt-4">
        <button
          type="button"
          aria-expanded={hooksOpen}
          aria-controls={revealId}
          onClick={() => setHooksOpen((value) => !value)}
          onMouseEnter={(event) => {
            event.currentTarget.style.background = "var(--surface-hover)";
          }}
          onMouseLeave={(event) => {
            event.currentTarget.style.background = "var(--surface-control)";
          }}
          className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-[var(--radius-input)] px-3 py-2 text-left text-[12px] text-slate transition-colors"
          style={{
            background: "var(--surface-control)",
            border: "1px solid var(--border-hairline)",
          }}
        >
          <span>{DISCLOSURE_LABEL}</span>
          <motion.span
            animate={{ rotate: hooksOpen ? 90 : 0 }}
            transition={{ duration: 0.15, ease: [0.2, 0, 0, 1] }}
            className="flex items-center text-steel"
            aria-hidden="true"
          >
            <ChevronGlyph />
          </motion.span>
        </button>

        <AnimatePresence initial={false}>
          {hooksOpen ? (
            <motion.div
              id={revealId}
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.18, ease: [0.2, 0, 0, 1] }}
            >
              <div
                className="mt-2 h-px"
                data-setup-hook-hairline
                style={{ background: HAIRLINE }}
              />

              <div className="mt-2.5 space-y-2.5">
                {AGENT_PROVIDER_ORDER.map((provider, index) => {
                  const detail = details[provider];
                  const label = AGENT_PROVIDER_LABEL[provider];
                  const pluginDir = detail.plugin_dir;
                  const disabled = !pluginDir;
                  const connected = providerConnected[provider];
                  const descriptor = connected
                    ? addedDescriptor(countHooks(detail.hook_definition))
                    : null;
                  return (
                    <Fragment key={provider}>
                      {index > 0 ? (
                        <div
                          className="h-px"
                          style={{ background: HAIRLINE }}
                          aria-hidden="true"
                        />
                      ) : null}
                      <div
                        data-setup-plugin-detail={provider}
                        className="flex items-start justify-between gap-3"
                      >
                        <div className="min-w-0">
                          <div className="text-[12px] leading-snug">
                            <span className="font-medium text-slate">
                              {label}
                            </span>
                            {descriptor ? (
                              <span className="text-steel">
                                {` · ${descriptor}`}
                              </span>
                            ) : null}
                          </div>
                          {pluginDir ? (
                            <div className="mt-0.5 break-all text-[11px] leading-snug text-steel">
                              {pluginDir}
                            </div>
                          ) : null}
                        </div>
                        <SecondaryButton
                          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-slate disabled:opacity-45"
                          style={{ border: STEEL_BORDER }}
                          disabled={disabled}
                          title={disabled ? DISABLED_REASON : REVEAL_LABEL}
                          aria-label={
                            pluginDir
                              ? `${REVEAL_LABEL} for ${label}: ${pluginDir}`
                              : `${REVEAL_LABEL} for ${label}`
                          }
                          aria-describedby={disabled ? reasonId : undefined}
                          onClick={() => handleReveal(provider)}
                        >
                          <FolderGlyph />
                        </SecondaryButton>
                      </div>
                    </Fragment>
                  );
                })}
              </div>
            </motion.div>
          ) : null}
        </AnimatePresence>
      </div>
    </SetupStep>
  );
}
