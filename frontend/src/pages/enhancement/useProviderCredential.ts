import { useEffect, useRef, useState } from "react";
import type { AppConfig } from "../../lib/api/config";
import { removeEnhancementKey } from "../../lib/api/credentials";
import { PROVIDER_KEY_FIELD, getStoredKeyForProvider, getKeyFingerprintForProvider } from "./providerMetadata";
import type { StatusState } from "./connectionStatus";

/** One provider's masked-key input and save/remove lifecycle. Permission stays server-owned. */
export function useProviderCredential({ config, activeProvider, onConfigChange, onConsentChanged, setStatus }: {
  config: AppConfig;
  activeProvider: string;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  onConsentChanged: () => void;
  setStatus: (status: StatusState) => void;
}) {
  const activeKeyField = PROVIDER_KEY_FIELD[activeProvider];
  const storedKey = getStoredKeyForProvider(config, activeProvider);
  const storedKeyFingerprint = getKeyFingerprintForProvider(config, activeProvider);
  // API key local state for debouncing (1000ms).
  const [localApiKey, setLocalApiKey] = useState(storedKey);
  const keyDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draftRef = useRef(localApiKey);
  draftRef.current = localApiKey;
  const submittedRef = useRef<string | null>(null);
  const previousRef = useRef({ provider: activeProvider, key: storedKey });

  // Switching providers refreshes the input to that provider's stored key
  // (empty if none yet). Same effect handles external changes (Remove key,
  // legacy migration arriving from the backend).
  useEffect(() => {
    const previous = previousRef.current;
    previousRef.current = { provider: activeProvider, key: storedKey };
    if (previous.provider !== activeProvider || draftRef.current === previous.key || draftRef.current === submittedRef.current) {
      if (keyDebounceRef.current) clearTimeout(keyDebounceRef.current);
      submittedRef.current = null;
      setLocalApiKey(storedKey);
    }
  }, [activeProvider, storedKey]);

  useEffect(() => () => {
    if (keyDebounceRef.current) clearTimeout(keyDebounceRef.current);
  }, []);

  const keyPatch = (value: string): Partial<AppConfig> => ({
    ...(activeKeyField ? { [activeKeyField]: value } : {}),
    ...(activeProvider === "anthropic" && value !== storedKey
      ? { enhancement_anthropic_workspace_id: "" } : {}),
  });
  const saveKey = (value: string) => {
    if (!activeKeyField || value === storedKey) return;
    submittedRef.current = value;
    onConfigChange(keyPatch(value));
  };

  const handleRemoveKey = async () => {
    if (keyDebounceRef.current) clearTimeout(keyDebounceRef.current);
    setLocalApiKey("");
    setStatus({ kind: "idle" });
    try {
      await removeEnhancementKey();
      setLocalApiKey("");
      setStatus({ kind: "idle" });
      // Clear the active provider's slot AND the legacy mirror so the
      // local state reflects the backend's post-remove config without
      // waiting for the GET that follows.
      const patch: Partial<AppConfig> = {
        enhancement_api_key: "",
        enhancement_enabled: false,
        ...(activeProvider === "anthropic" ? { enhancement_anthropic_workspace_id: "" } : {}),
      };
      if (activeKeyField) {
        (patch as Record<string, unknown>)[activeKeyField] = "";
      }
      onConfigChange(patch);
      onConsentChanged();
    } catch (err) {
      setLocalApiKey(storedKey);
      setStatus({
        kind: "error",
        message: `Remove failed: ${err instanceof Error ? err.message : err}`,
      });
    }
  };

  const handleApiKeyChange = (value: string) => {
    setLocalApiKey(value);
    if (keyDebounceRef.current) clearTimeout(keyDebounceRef.current);
    keyDebounceRef.current = setTimeout(() => {
      saveKey(value);
    }, 1000);
  };

  const handleApiKeyBlur = () => {
    if (keyDebounceRef.current) clearTimeout(keyDebounceRef.current);
    saveKey(localApiKey);
  };

  return { storedKey, storedKeyFingerprint, localApiKey, keyDraftPending: localApiKey !== storedKey, handleRemoveKey, handleApiKeyChange, handleApiKeyBlur };
}
