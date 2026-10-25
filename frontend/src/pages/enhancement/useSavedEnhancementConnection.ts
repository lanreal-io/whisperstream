import { useEffect, useRef, useState } from "react";
import { getConfig, type AppConfig } from "../../lib/api/config";
import { getEnhancementThinking, type EnhancementThinking } from "../../lib/api/enhancement";
import { getKeyFingerprintForProvider, getStoredKeyForProvider } from "./providerMetadata";

/** Comparison only, never provider identity/capability authority. No content is logged. */
export function enhancementConnectionSnapshot(config: AppConfig): string {
  const provider = config.enhancement_provider || "local";
  return JSON.stringify({
    provider,
    enabled: !!config.enhancement_enabled,
    lockdown: !!config.lockdown_mode,
    key: getStoredKeyForProvider(config, provider),
    fingerprint: getKeyFingerprintForProvider(config, provider),
    model: config[`enhancement_${provider}_model` as keyof AppConfig] || "",
    endpoint: provider === "custom" ? config.enhancement_custom_base_url
      : provider === "ollama" ? config.enhancement_ollama_endpoint
        : provider === "local" ? config.enhancement_local_endpoint : "",
    localModel: config.lockdown_mode ? config.enhancement_local_model : undefined,
    workspace: provider === "anthropic" ? config.enhancement_anthropic_workspace_id || "" : "",
    thinking: Object.entries(config.enhancement_thinking_levels || {}).sort(([a], [b]) => a.localeCompare(b)),
    timeout: config.enhancement_timeout_seconds ?? 5,
  });
}

function modelSnapshot(config: AppConfig): string {
  const provider = config.lockdown_mode ? "local" : config.enhancement_provider || "local";
  return JSON.stringify([
    provider, config[`enhancement_${provider}_model` as keyof AppConfig] || "",
    provider === "custom" ? config.enhancement_custom_base_url
      : provider === "ollama" ? config.enhancement_ollama_endpoint
        : provider === "local" ? config.enhancement_local_endpoint : "",
  ]);
}

/** App's save revision can belong to an older queued write. Confirm the saved
 * connection before probing it or consuming a descriptor for its identity. */
export function useSavedEnhancementConnection(config: AppConfig, configSaveRevision: number) {
  const signature = enhancementConnectionSnapshot(config);
  const modelSignature = modelSnapshot(config);
  const signatureRef = useRef(signature);
  signatureRef.current = signature;
  const identityRevisionRef = useRef({ signature, revision: 0 });
  if (identityRevisionRef.current.signature !== signature) {
    identityRevisionRef.current = { signature, revision: identityRevisionRef.current.revision + 1 };
  }
  const [saved, setSaved] = useState<{ signature: string; revision: number } | null>(null);
  const [thinkingResult, setThinkingResult] = useState<{ model: string; value: EnhancementThinking } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const requestedSignature = signatureRef.current;
    const requestedRevision = identityRevisionRef.current.revision;
    getConfig().then((persisted) => {
      if (!cancelled && signatureRef.current === requestedSignature && identityRevisionRef.current.revision === requestedRevision) {
        setSaved({ signature: enhancementConnectionSnapshot(persisted), revision: requestedRevision });
      }
    }).catch(() => { if (!cancelled) setSaved(null); });
    return () => { cancelled = true; };
  }, [configSaveRevision]);

  const confirmed = saved?.signature === signature && saved.revision === identityRevisionRef.current.revision;
  useEffect(() => {
    if (!confirmed) return;
    let cancelled = false;
    const requestedSignature = signature;
    getEnhancementThinking().then((value) => {
      if (cancelled || signatureRef.current !== requestedSignature) return;
      // A missing/older endpoint cannot produce an invented Off control.
      if (!["configurable", "mandatory", "unknown", "fixed"].includes(value.kind)
          || !Array.isArray(value.options) || !value.options.every((option) => typeof option === "string")
          || typeof value.preference_key !== "string" || typeof value.default !== "string"
          || typeof value.effective !== "string") return;
      setThinkingResult({ model: modelSignature, value });
    }).catch(() => { if (!cancelled) setThinkingResult(null); });
    return () => { cancelled = true; };
  }, [confirmed, signature, modelSignature, configSaveRevision]);

  return {
    signature,
    confirmed,
    thinking: thinkingResult?.model === modelSignature ? thinkingResult.value : null,
  };
}
