import { describe, expect, it } from "vitest";

import { sttFailureToErrorPayload } from "./sttFailure";
import type { SttFailureEvent } from "./socket/dictationEvents";

describe("sttFailureToErrorPayload", () => {
  it.each([
    "worker_start_failed",
    "worker_protocol_failed",
    "worker_timeout",
    "worker_python_exception",
    "worker_native_exit",
  ] as const)("maps %s to the specific STT popup", (code) => {
    const event: SttFailureEvent = {
      type: "stt.failure",
      engine: "speech_to_text",
      backend: "parakeet",
      model_id: "parakeet-tdt-0.6b-v3",
      operation: "transcribe_chunk",
      code,
      recovery_attempted: true,
      suppressed: false,
    };

    expect(sttFailureToErrorPayload(event)).toEqual({
      code: "stt_engine_failed",
      title: "Speech model stopped",
      message:
        "Whisperstream is still running, but the speech model couldn't recover. Reload the model to try again.",
      retryAction: "reload_model",
    });
  });
});
