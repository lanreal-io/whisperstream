import { BASE, fetchJSON } from "./client";


// ---------------------------------------------------------------------------
// Dev-tools discovery, latency harness and HUD simulator endpoints
// ---------------------------------------------------------------------------

export async function getDevEnabled(): Promise<{ enabled: boolean }> {
  // Resilient: returns { enabled: false } on network failure OR 404. The dev endpoint
  // always returns 200; a 404 or fetch error means the backend doesn't know about dev tools.
  try {
    const r = await fetch(`${BASE}/api/dev/enabled`);
    if (!r.ok) return { enabled: false };
    return await r.json();
  } catch {
    return { enabled: false };
  }
}


/** Get the current dev-only simulated local-cleanup speed (tok/s).
 *  `null` means Off (real speed, no padding). In-memory on the backend only --
 *  never persisted to config.toml, so a forgotten throttle can't survive a
 *  restart. 404s when `WHISPERSTREAM_DEV_TOOLS` is unset; only call this from
 *  behind the `devTabVisible` gate. */
export async function getSimulatedTps(): Promise<number | null> {
  const { tps } = await fetchJSON<{ tps: number | null }>(
    `${BASE}/api/dev/simulated-tps`,
  );
  return tps;
}


/** Set the dev-only simulated local-cleanup speed (tok/s). Pass `null` (or a
 *  value <= 0, which the backend also treats as Off) to disable padding.
 *  `llm_enhancer.enhance()` sleeps to pad real cleanup calls down to this
 *  words/sec so the benchmark speed thresholds can be exercised on fast
 *  hardware. In-memory only -- never persisted. */
export async function setSimulatedTps(tps: number | null): Promise<void> {
  await fetchJSON(`${BASE}/api/dev/simulated-tps`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tps }),
  });
}


// ---------------------------------------------------------------------------
// Dev-only Agent Speak HUD simulator (WHISPERSTREAM_DEV_TOOLS=1).
//
// Every call here 404s in a normal build; only call them from behind the
// `devTabVisible` gate. Scenarios fire through the REAL /api/claude-hook path
// on the backend, so what the HUD renders is what a live Claude session
// produces.
// ---------------------------------------------------------------------------

/** One catalogued HUD state, as returned by `/api/dev/hud/scenarios`. */
export interface HudScenario {
  id: string;
  group: string;
  label: string;
  note: string;
}


export async function getHudScenarios(): Promise<HudScenario[]> {
  const { scenarios } = await fetchJSON<{ scenarios: HudScenario[] }>(
    `${BASE}/api/dev/hud/scenarios`,
  );
  return scenarios;
}


/** Fire one scenario. Returns as soon as the backend has accepted it: the hook
 *  itself blocks for its whole grace window, so the backend drives it in the
 *  background. */
export async function fireHudScenario(
  scenario: string,
): Promise<{ ok: boolean; detail?: string }> {
  return fetchJSON(`${BASE}/api/dev/hud/fire`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scenario }),
  });
}


/** Clear retention, dedupe, and every live interaction so the HUD returns to
 *  idle. Required between firings: completions are content-deduplicated, so
 *  re-firing the same scenario without this silently does nothing. */
export async function resetHudState(): Promise<{ ok: boolean }> {
  return fetchJSON(`${BASE}/api/dev/hud/reset`, { method: "POST" });
}


/** Send the `View full response` command the global shortcut would send. */
export async function toggleHudView(): Promise<{ ok: boolean }> {
  return fetchJSON(`${BASE}/api/dev/hud/view`, { method: "POST" });
}


/** Open a reply route the way push-to-talk would (real lease, real recording). */
export async function hudComposeStart(
  mode: "push_to_talk" | "toggle" = "push_to_talk",
): Promise<{ ok: boolean; detail?: string; phase?: string }> {
  return fetchJSON(`${BASE}/api/dev/hud/compose/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode }),
  });
}


/** Inject synthetic interim speech into the open reply route (no microphone). */
export async function hudComposePreview(
  text: string,
  revision: number,
): Promise<{ ok: boolean; detail?: string }> {
  return fetchJSON(`${BASE}/api/dev/hud/compose/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, revision }),
  });
}


/** Deliver the authoritative reply draft for the open reply route. */
export async function hudComposeFinal(
  text: string,
): Promise<{ ok: boolean; detail?: string }> {
  return fetchJSON(`${BASE}/api/dev/hud/compose/final`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
}


export async function hudComposeCancel(): Promise<{ ok: boolean; detail?: string }> {
  return fetchJSON(`${BASE}/api/dev/hud/compose/cancel`, { method: "POST" });
}


/** Content-free snapshot of the dictation / lease / question / response chain. */
export async function getHudDevState(): Promise<Record<string, unknown>> {
  return fetchJSON(`${BASE}/api/dev/hud/state`);
}


export async function getExperimentalEnabled(): Promise<{ enabled: boolean }> {
  // Discovery probe for the agent-speech experimental surface. Same resilient
  // shape as getDevEnabled: { enabled: false } on 404 or fetch error, so the
  // frontend can hide gated UI without special-casing a missing backend.
  try {
    const r = await fetch(`${BASE}/api/experimental/enabled`);
    if (!r.ok) return { enabled: false };
    return await r.json();
  } catch {
    return { enabled: false };
  }
}
