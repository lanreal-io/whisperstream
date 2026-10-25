import { useEffect, useRef, useState } from "react";
import { getBYOKConsentContext } from "../../lib/api/consent";
import { enhancementErrorMessage } from "../../lib/enhancementConsent";
import type { StatusState } from "./connectionStatus";

/** Exact recipient lookup. Historical provider names invalidate, never grant permission. */
export function useRecipientPermission({ recipientKey, isLockdown, activeProvider, configSaveRevision, acknowledgedProviders, setStatus }: {
  recipientKey: string;
  isLockdown: boolean;
  activeProvider: string;
  configSaveRevision: number;
  acknowledgedProviders: string[];
  setStatus: (status: StatusState) => void;
}) {
  const [recipientPermission, setRecipientPermission] = useState<{ key: string; allowed: boolean; error?: string } | null>(null);
  const hasRecipientConsent = isLockdown || activeProvider === "local" || (
    recipientPermission?.key === recipientKey && recipientPermission.allowed
  );
  const recipientError = recipientPermission?.key === recipientKey ? recipientPermission.error : undefined;
  const permissionContext = JSON.stringify([recipientKey, isLockdown, activeProvider, [...acknowledgedProviders].sort()]);
  const priorContext = useRef("");

  useEffect(() => {
    if (isLockdown || activeProvider === "local") return;
    let cancelled = false;
    // A profile-only save does not change the recipient or invalidate its
    // connection evidence. Recipient/history changes still fail closed.
    if (priorContext.current !== permissionContext) {
      setRecipientPermission(null);
      setStatus({ kind: "idle" });
    }
    priorContext.current = permissionContext;
    getBYOKConsentContext(JSON.parse(recipientKey)).then((context) => {
      if (!cancelled) setRecipientPermission({ key: recipientKey, allowed: context.has_consent === true && context.matches_config === true });
    }).catch((error) => {
      if (!cancelled) setRecipientPermission({ key: recipientKey, allowed: false, error: enhancementErrorMessage(error) });
    });
    return () => { cancelled = true; };
    // Provider-history refresh is only an invalidation signal, never permission.
  }, [isLockdown, activeProvider, recipientKey, configSaveRevision, permissionContext]);

  return { hasRecipientConsent, recipientError, setRecipientPermission };
}
