import {
  type AgentMarkVariant,
  AGENT_MARK_VARIANTS,
  DEFAULT_AGENT_MARK_VARIANT,
  ClaudeIdentityRow,
} from "./components/AgentIdentity";
import { CLAUDE_PILL_H } from "./geometry";
import { agentProviderLabel } from "../../lib/sessionName";

export const AGENT_MARK_COMPARISON_FIXTURE_ID = "agent_mark_comparison";

const AGENT_MARK_VARIANT_LABELS: Record<AgentMarkVariant, string> = {
  "prompt-cursor": "Prompt + cursor",
  "code-chevrons": "Code chevrons",
  "braces-cursor": "Braces + cursor",
  "terminal-frame": "Terminal frame",
};

/** Deterministic development-only review sheet. It renders the real compact
 *  identity component for both providers, but is never selected by normal HUD
 *  presentation or added to the cross-window forced-state controls. */
export function AgentMarkComparisonSurface() {
  return (
    <main
      className="flex h-full w-full items-center justify-center p-5"
      data-testid="agent-mark-comparison"
      data-ui-qa-root="hud.agent-mark-comparison"
      data-ui-qa-state="hud.agent-mark-comparison"
      data-ui-qa-ready="true"
      data-ui-qa-surface="hud"
      style={{ background: "var(--color-canvas)" }}
    >
      <section
        className="w-full rounded-[var(--radius-row)] p-4 shadow-glass"
        aria-labelledby="agent-mark-comparison-title"
        style={{
          maxWidth: 560,
          background: "var(--surface-card)",
          border: "1px solid var(--border-emphasis)",
        }}
      >
        <div className="mb-3 flex items-end justify-between gap-4">
          <div>
            <h1
              id="agent-mark-comparison-title"
              className="text-[13px] font-medium text-slate"
            >
              Neutral coding-agent marks
            </h1>
            <p className="mt-0.5 text-[11px] text-steel">
              Provider text carries identity. The mark stays shared.
            </p>
          </div>
          <span className="shrink-0 text-[10px] font-medium text-steel">
            12px artwork
          </span>
        </div>

        <div className="grid grid-cols-4 gap-2">
          {AGENT_MARK_VARIANTS.map((variant) => {
            const recommended = variant === DEFAULT_AGENT_MARK_VARIANT;
            return (
              <article
                key={variant}
                className="rounded-[var(--radius-input)] p-2"
                data-agent-mark-comparison-variant={variant}
                style={{
                  background: "var(--surface-control)",
                  border: recommended
                    ? "1px solid var(--border-accent)"
                    : "1px solid var(--border-hairline)",
                }}
              >
                <div className="mb-2 h-7">
                  <div className="truncate text-[10px] font-medium text-slate">
                    {AGENT_MARK_VARIANT_LABELS[variant]}
                  </div>
                  {recommended && (
                    <div
                      className="mt-0.5 text-[8px] font-semibold uppercase tracking-[0.08em]"
                      style={{ color: "var(--color-frost-deep)" }}
                    >
                      Recommended
                    </div>
                  )}
                </div>

                {(["claude", "codex"] as const).map((provider) => (
                  <div
                    key={provider}
                    className="mb-1 last:mb-0 rounded-full"
                    data-agent-mark-comparison-provider={provider}
                    style={{
                      height: CLAUDE_PILL_H,
                      background: "var(--surface-overlay)",
                      border: "1px solid var(--border-emphasis)",
                      boxShadow: "var(--shadow-glass-sm)",
                    }}
                  >
                    <ClaudeIdentityRow
                      variant="compact"
                      provider={provider}
                      sourceName={agentProviderLabel(provider)}
                      markVariant={variant}
                    />
                  </div>
                ))}
              </article>
            );
          })}
        </div>
      </section>
    </main>
  );
}
