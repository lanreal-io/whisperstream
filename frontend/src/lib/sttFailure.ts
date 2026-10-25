import type { ErrorEmitPayload } from "./api/errors";
import type { SttFailureEvent } from "./socket/dictationEvents";

/** Map the content-free worker contract onto the existing error popup. */
export function sttFailureToErrorPayload(
  _event: SttFailureEvent,
): ErrorEmitPayload {
  return {
    code: "stt_engine_failed",
    title: "Speech model stopped",
    message:
      "Whisperstream is still running, but the speech model couldn't recover. Reload the model to try again.",
    retryAction: "reload_model",
  };
}
