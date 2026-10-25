import { fetchJSON, BASE } from "./client";


export interface BYOKConsentRecord {
  provider: string;
  consent_given_at: string;
  privacy_policy_version: string;
  eula_version: string;
  software_version: string;
  dialog_content_hash: string;
}


export interface BYOKConsentState {
  acknowledged_providers: string[];
  records: BYOKConsentRecord[];
}


export async function getBYOKConsent(): Promise<BYOKConsentState> {
  return fetchJSON(`${BASE}/api/byok-consent`);
}


export interface BYOKConsentRequest {
  provider: string;
  endpoint?: string;
  model?: string;
}


export interface BYOKConsentContext {
  provider: string;
  endpoint: string;
  model: string;
  recipient_identity: string;
  context_token: string;
  has_consent: boolean;
  matches_config: boolean;
  dialog_body_text: string;
  lines: string[];
  links: { label: string; url: string }[];
  legal_identity: Record<string, string>;
}


export async function getBYOKConsentContext(request: BYOKConsentRequest): Promise<BYOKConsentContext> {
  return fetchJSON(`${BASE}/api/byok-consent/context`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
}


export async function recordBYOKConsent(context: BYOKConsentContext): Promise<BYOKConsentRecord> {
  return fetchJSON(`${BASE}/api/byok-consent`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider: context.provider, endpoint: context.endpoint,
      model: context.model, context_token: context.context_token,
      dialog_body_text: context.dialog_body_text }),
  });
}


export interface AcceptanceRecord {
  eula_version: string;
  eula_accepted_at: string;
  software_version: string;
  eula_sha256: string;
}


export interface OptOutRecord {
  declined_version: string;
  pinned_version: string;
  declined_at: string;
  expires_at: string;
  software_version: string;
}


export type EulaRequirementState =
  | "first_run"
  | "reacknowledgement_required"
  | "substantive_update_required"
  | "configuration_error"
  | "ready";


export interface EulaStatus {
  acceptance: AcceptanceRecord | null;
  state?: EulaRequirementState;
  notice_id?: string | null;
  requirement_kind?:
    | "first_run"
    | "reacknowledgement"
    | "substantive_update"
    | null;
  summary?: string[];
  policy_reason?: string;
  grant_reason?: string;
  bundled_version: string | null;
  bundled_sha256: string | null;
  bundled_text: string | null;
  software_version: string;
  needs_first_run: boolean;
  needs_amendment: boolean;
  needs_reacknowledgement: boolean;
  pinned_version?: string | null;
}


export function resolveEulaRequirementState(
  status: EulaStatus,
): EulaRequirementState {
  if (status.state) return status.state;
  if (status.needs_first_run) return "first_run";
  if (status.needs_amendment) return "substantive_update_required";
  if (status.needs_reacknowledgement) return "reacknowledgement_required";
  return "ready";
}


/** Payload emitted on the `"eula-data"` Tauri event. The popup listens for
 * this to hydrate its render without a second round-trip to the backend. */
export interface EulaEmitPayload {
  mode:
    | "first-run"
    | "amendment"
    | "reacknowledge"
    | "substantive-update"
    | "configuration-error";
  eulaText: string;
  eulaVersion: string;
  eulaSha256: string;
  softwareVersion: string;
  pinnedVersion?: string;
  changeBullets?: string[];
}


export async function getEulaStatus(): Promise<EulaStatus> {
  return fetchJSON(`${BASE}/api/eula/status`, { signal: AbortSignal.timeout(5000) });
}


export async function acceptEula(payload: {
  eula_version: string;
  eula_sha256: string;
}): Promise<AcceptanceRecord> {
  return fetchJSON(`${BASE}/api/eula/accept`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}


export async function declineAmendment(payload: {
  declined_version: string;
  pinned_version: string;
}): Promise<OptOutRecord> {
  return fetchJSON(`${BASE}/api/eula/decline-amendment`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}
