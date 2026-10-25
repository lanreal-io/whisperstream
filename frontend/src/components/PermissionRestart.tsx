import { useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { relaunch } from "@tauri-apps/plugin-process";
import { SecondaryButton } from "./SecondaryButton";

// Keep recovery inside the existing frost permission surface: Inter 12px,
// steel copy, slate action, hairline secondary button, and a 4px spacing grid.
// Restart is explicit: a stale native preflight must never imply a grant.
export function PermissionRestart({ disabled = false, compact = false }: { disabled?: boolean; compact?: boolean }) {
  const [restarting, setRestarting] = useState(false);
  const [failed, setFailed] = useState(false);
  const restart = async () => {
    if (restarting || disabled) return;
    setRestarting(true);
    setFailed(false);
    try {
      await relaunch();
    } catch {
      setFailed(true);
      setRestarting(false);
    }
  };
  return <div className={`${compact ? "" : "mt-3 "}text-[12px] leading-relaxed text-steel`}>
    {!compact && <p>Already enabled? Restart Whisperstream to refresh access.</p>}
    {import.meta.env.DEV ? <p className={compact ? "" : "mt-2"} title="Stop the dev launcher, then run scripts/run_dev.sh again.">{compact ? "Restart dev launcher" : "Stop the dev launcher, then run scripts/run_dev.sh again."}</p>
      : isTauri() ? <SecondaryButton type="button" onClick={restart}
        disabled={disabled || restarting} aria-busy={restarting}
        className={`${compact ? "" : "mt-2 "}rounded-full px-3 py-1.5 text-[11.5px] text-slate disabled:opacity-50`}>
        {restarting ? "Restarting..." : "Restart app"}
      </SecondaryButton> : <p className="mt-2">Quit Whisperstream from its app menu, then open it again.</p>}
    {failed && <p role="alert" className="mt-2">Couldn't restart. Quit Whisperstream from its app menu, then open it again.</p>}
  </div>;
}
