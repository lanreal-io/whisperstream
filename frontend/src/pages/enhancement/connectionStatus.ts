export type StatusState =
  | { kind: "idle" }
  | { kind: "not-checked" }
  | { kind: "checking" }
  | { kind: "connected" }
  | { kind: "error"; message: string }
  | { kind: "ollama-loading" }
  | { kind: "local-standby"; model?: string; port?: number; message?: string }
  | { kind: "local-loading"; model?: string; port?: number; message?: string }
  | { kind: "local-ready";   model?: string; port?: number; message?: string };

export function labelFor(s: StatusState): string {
  switch (s.kind) {
    case "not-checked":   return "Not checked";
    case "checking":      return "Checking…";
    case "connected":     return "Connected";
    case "error":         return s.message;
    case "ollama-loading": return "Loading model...";
    case "local-standby": return "Standby";
    case "local-loading": return "Loading";
    case "local-ready":   return "Ready";
    default:              return "";
  }
}

export function dotClassFor(s: StatusState): string {
  if (s.kind === "not-checked" || s.kind === "checking") return "bg-steel/50";
  if (s.kind === "connected" || s.kind === "local-ready") return "bg-mint";
  if (s.kind === "ollama-loading" || s.kind === "local-standby" || s.kind === "local-loading") return "bg-gold";
  return "bg-rose"; // error
}

export function labelClassFor(s: StatusState): string {
  if (s.kind === "not-checked" || s.kind === "checking") return "text-steel";
  if (s.kind === "connected" || s.kind === "local-ready") return "text-mint";
  if (s.kind === "ollama-loading" || s.kind === "local-standby" || s.kind === "local-loading") return "text-gold-deep";
  return "text-rose"; // error
}

export function ariaLabelFor(s: StatusState): string | undefined {
  switch (s.kind) {
    case "not-checked":   return "Connection not checked";
    case "checking":      return "Checking connection";
    case "connected":     return "Cloud provider connected";
    case "error":         return `Error: ${s.message}`;
    case "ollama-loading": return "Ollama model loading";
    case "local-standby": return "Local model standby";
    case "local-loading": return "Local model loading";
    case "local-ready":   return "Local model ready";
    default:              return undefined;
  }
}

/** Plain-language hover copy for the top-right status pill. Deliberately drops
 *  the model id + port — the user never needs those; they want to know whether
 *  the local model will clean their next dictation. `providerName` supplies the
 *  cloud provider's display name for the connected state. */
export function tooltipFor(s: StatusState, providerName?: string): string | undefined {
  switch (s.kind) {
    case "local-ready":
      return "Ready to clean up your next dictation.";
    case "ollama-loading":
      return "Enhancement is unavailable while the model loads.";
    case "local-loading":
      return "Getting ready. This only takes a moment.";
    case "local-standby":
      return "Resting to save memory. Loads again when you start recording.";
    case "connected":
      return providerName ? `Connected to ${providerName}.` : "Connected.";
    case "error":
      return s.message;
    default:
      return undefined;
  }
}

/** Map a raw supervisor state payload (from REST seed or WS event) to the
 *  badge StatusState. This is the LOCKED mapping from design.md
 *  "Notes for the execute phase". Do not reinterpret. */
export function mapLlamaStateToStatus(s: {
  state: string;
  model?: string;
  port?: number | null;
  message?: string;
}): StatusState {
  if (s.state === "absent") return { kind: "idle" };
  if (s.state === "ready") {
    return { kind: "local-ready", model: s.model, port: s.port ?? undefined, message: s.message };
  }
  if (s.state === "spawning" || s.state === "health_ok" || s.state === "warming") {
    return { kind: "local-loading", model: s.model, port: s.port ?? undefined, message: s.message };
  }
  if (s.state === "dormant" || s.state === "stopping") {
    return { kind: "local-standby", model: s.model, port: s.port ?? undefined, message: s.message };
  }
  if (s.state === "error") {
    return { kind: "error", message: s.message || "Local model error" };
  }
  return { kind: "idle" };
}
