import { useEffect, useRef, useState } from "react";
import type { AppConfig } from "../../lib/api/config";
import type { WorkspaceErrorCode } from "../../lib/api/enhancement";

export const WORKSPACE_ERRORS: Record<WorkspaceErrorCode, string> = {
  workspace_required: "This key needs a workspace. Add its Workspace ID, or use a workspace-scoped API key.",
  workspace_invalid: "This Workspace ID has the wrong format. Copy it from Anthropic Console and try again.",
  workspace_unavailable: "This key cannot use that workspace. Check the Workspace ID, or replace the API key.",
};

export function isWorkspaceError(code?: string): code is WorkspaceErrorCode {
  return code !== undefined && Object.prototype.hasOwnProperty.call(WORKSPACE_ERRORS, code);
}

/** Recovery belongs to one credential context. A saved ID alone never reveals it. */
export function useAnthropicWorkspace({ config, credentialContext, active, onConfigChange }: {
  config: AppConfig;
  credentialContext: string;
  active: boolean;
  onConfigChange: (patch: Partial<AppConfig>) => void;
}) {
  const saved = config.enhancement_anthropic_workspace_id || "";
  const [draft, setDraft] = useState(saved);
  const [revealedContext, setRevealedContext] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const submittedRef = useRef<string | null>(null);
  const previousSavedRef = useRef(saved);
  const contextRef = useRef(credentialContext);
  contextRef.current = credentialContext;

  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    setDraft(saved);
    setRevealedContext(null);
  }, [credentialContext]);
  useEffect(() => {
    if (draftRef.current === previousSavedRef.current || draftRef.current === submittedRef.current) {
      setDraft(saved);
      submittedRef.current = null;
    }
    previousSavedRef.current = saved;
  }, [saved]);
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const flush = (value = draft) => {
    if (timer.current) clearTimeout(timer.current);
    if (active && value !== saved) {
      submittedRef.current = value;
      onConfigChange({ enhancement_anthropic_workspace_id: value });
    }
  };
  const change = (value: string) => {
    setDraft(value);
    if (timer.current) clearTimeout(timer.current);
    const context = credentialContext;
    timer.current = setTimeout(() => {
      if (contextRef.current === context) flush(value);
    }, 1000);
  };
  const reset = () => {
    if (timer.current) clearTimeout(timer.current);
    setRevealedContext(null);
    setDraft("");
  };

  return {
    draft,
    pending: active && revealedContext === credentialContext && draft !== saved,
    visible: active && revealedContext === credentialContext,
    reveal: () => setRevealedContext(credentialContext),
    reset,
    change,
    flush,
    clear: () => { setDraft(""); flush(""); },
  };
}
