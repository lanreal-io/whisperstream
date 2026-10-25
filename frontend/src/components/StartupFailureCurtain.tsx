import { useState } from "react";
import { isTauri } from "@tauri-apps/api/core";
import { relaunch } from "@tauri-apps/plugin-process";
import { LoadingCurtain } from "./LoadingCurtain";
import { PrimaryButton } from "./PrimaryButton";
import { SecondaryButton } from "./SecondaryButton";

export function StartupFailureCurtain({
  onRetry = () => window.location.reload(),
}: {
  onRetry?: () => void;
}) {
  const [restarting, setRestarting] = useState(false);
  const [restartFailed, setRestartFailed] = useState(false);
  const restart = async () => {
    setRestarting(true);
    setRestartFailed(false);
    try {
      await relaunch();
    } catch {
      setRestartFailed(true);
      setRestarting(false);
    }
  };
  return (
    <LoadingCurtain label="Whisperstream couldn't finish starting.">
      <div data-testid="startup-failure-curtain" className="max-w-sm px-6">
        <p className="mt-3 text-sm text-steel">
          The background service isn't responding. Finish any system prompt, then try again or restart Whisperstream.
        </p>
        <div className="mt-5 flex justify-center gap-3">
          <SecondaryButton className="rounded-full px-4 py-2 text-sm text-steel" disabled={restarting} onClick={onRetry}>Try again</SecondaryButton>
          {isTauri() && <PrimaryButton className="rounded-full px-4 py-2 text-sm" disabled={restarting} onClick={restart}>
            {restarting ? "Restarting…" : "Restart app"}
          </PrimaryButton>}
        </div>
        {restartFailed && <p role="alert" className="mt-3 text-sm text-steel">Couldn't restart. Quit Whisperstream and open it again.</p>}
      </div>
    </LoadingCurtain>
  );
}
