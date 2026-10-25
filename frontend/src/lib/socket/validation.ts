

export const AGENT_ID_RE = /^[A-Za-z0-9_-]{1,512}$/;



export const AGENT_FORBIDDEN_FIELDS = new Set([
  "agent_session_id",
  "cwd",
  "hwnd",
  "internal_route",
  "prompt_id",
  "provider_session_id",
  "raw_id",
  "raw_session_id",
  "route_key",
  "session_id",
  "source_id",
  "source_key",
  "tool_input",
  "turn_id",
  "working_directory",
]);



export function containsForbiddenAgentField(value: unknown): boolean {
  if (Array.isArray(value)) {
    return value.some(containsForbiddenAgentField);
  }
  if (!isPlainRecord(value)) return false;
  for (const [key, child] of Object.entries(value)) {
    const normalized = key.trim().toLowerCase().replace(/-/g, "_");
    if (AGENT_FORBIDDEN_FIELDS.has(normalized)) return true;
    if (containsForbiddenAgentField(child)) return true;
  }
  return false;
}


export function hasOnlyKeys(
  value: Record<string, unknown>,
  allowed: string[],
): boolean {
  const allowedSet = new Set(allowed);
  return Object.keys(value).every((key) => allowedSet.has(key));
}


export function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    (Object.getPrototypeOf(value) === Object.prototype ||
      Object.getPrototypeOf(value) === null)
  );
}


export function cloneJsonRecord(
  value: Record<string, unknown>,
): Record<string, unknown> {
  return JSON.parse(JSON.stringify(value)) as Record<string, unknown>;
}


export function isOpaqueAgentId(value: unknown): value is string {
  return typeof value === "string" && AGENT_ID_RE.test(value);
}


export function isSequence(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) >= 0;
}


export function isPositiveSequence(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}


export function isNonNegativeFinite(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    value >= 0
  );
}


export function isSafeSourceLabel(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 120 &&
    !value.includes("/") &&
    !value.includes("\\") &&
    [...value].every((character) => character >= " ")
  );
}


export function isBoundedNonBlankString(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.trim().length > 0 &&
    value.length <= 500_000
  );
}


export function isBoundedString(value: unknown): value is string {
  return typeof value === "string" && value.length <= 500_000;
}
