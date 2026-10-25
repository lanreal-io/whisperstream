import type { AgentCompatibility } from "../lib/api/agents";

/** Explicit live CLI evidence for older tests whose subject is another control. */
export function readyAgentResult() {
  return {
    ok: true, status: "current" as const,
    compatibility: { surfaces: [{
      surface: "cli", state: "ready", app_version: null, engine_version: "test-version",
      minimum_version: null, reply_verified: true, transport: "codex_queue",
    }] } satisfies AgentCompatibility,
  };
}
