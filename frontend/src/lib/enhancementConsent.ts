import type { AppConfig } from "./api/config";
import type { BYOKConsentContext, BYOKConsentRequest } from "./api/consent";

/** A draft recipient is evidence to review, never dispatch authorization. */
export function consentRequest(config: AppConfig, provider = config.enhancement_provider): BYOKConsentRequest {
  const model = config[`enhancement_${provider}_model` as keyof AppConfig];
  return {
    provider,
    ...(provider === "custom" ? { endpoint: config.enhancement_custom_base_url } : {}),
    ...(provider === "ollama" ? { endpoint: config.enhancement_ollama_endpoint } : {}),
    ...(typeof model === "string" ? { model } : {}),
  };
}

export function consentConfigPatch(context: BYOKConsentContext): Partial<AppConfig> {
  return {
    enhancement_provider: context.provider,
    enhancement_enabled: true,
    [`enhancement_${context.provider}_model`]: context.model,
    ...(context.provider === "custom" ? { enhancement_custom_base_url: context.endpoint } : {}),
    ...(context.provider === "ollama" ? { enhancement_ollama_endpoint: context.endpoint } : {}),
  };
}

export function enhancementErrorMessage(error: unknown, fallback = "Could not load the privacy notice. Try again."): string {
  const code = (error as { errorCode?: string })?.errorCode;
  switch (code) {
    case "consent_changed": return "Connection settings changed. Review the updated privacy notice.";
    case "consent_required": return "Review the privacy notice before using this connection.";
    case "enhancement_disabled": return "Turn on Enhancement to test prompts or check the connection.";
    case "endpoint_https_required": return "Remote endpoints require HTTPS. Update the endpoint to continue.";
    case "invalid_endpoint": return "Enter a valid HTTP or HTTPS endpoint without a username, password, query or fragment.";
    case "local_endpoint_not_loopback": return "Local models require a server on this device. Use Custom for a remote endpoint.";
    case "secure_storage_failed": return "Could not save the key securely. Your saved settings were not changed. Try again.";
    case "consent_store_unavailable": return "Could not read or save permission. Try again. No request was sent.";
    case "consent_documents_unavailable": return "The privacy documents are unavailable. Restore or reinstall the app to review this connection.";
    default: return fallback;
  }
}
