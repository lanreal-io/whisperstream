import { type EventHandler, type AppEvent } from "./events";


// ---------------------------------------------------------------------------
// Local-LLM download subscription helper
// ---------------------------------------------------------------------------

/** Download event types that carry an optional ``kind`` discriminator. */
export type DownloadEvent =
  | { type: "download.progress"; downloaded: number; total: number; speed_bps: number; kind?: string; model_id?: string; engine?: "kokoro" | "supertonic" }
  | { type: "download.complete"; kind?: string; model_id?: string; engine?: "kokoro" | "supertonic" }
  | { type: "download.error"; message: string; reason?: string; restored?: boolean; kind?: string; model_id?: string; engine?: "kokoro" | "supertonic" }
  | { type: "download.cancelled"; kind?: string; model_id?: string; engine?: "kokoro" | "supertonic" };


/**
 * Wraps the raw ``subscribe`` function returned by ``useAppSocket`` and filters
 * the ``download.*`` event stream to a specific ``kind``.
 *
 * - Pass ``"local_llm"`` from the Enhancement tab so it only sees local GGUF
 *   download events and ignores Parakeet progress events.
 * - Pass ``undefined`` (or omit the argument) to preserve legacy Parakeet
 *   behaviour: only events where ``kind`` is absent are forwarded, matching
 *   how the backend tags its existing Parakeet events (no ``kind`` field).
 *
 * Returns an unsubscribe function, matching the raw ``subscribe`` contract.
 */
export function subscribeToDownloadEvents(
  subscribe: (handler: EventHandler) => () => void,
  handler: (event: DownloadEvent) => void,
  kind: string | undefined,
): () => void {
  return subscribe((event: AppEvent) => {
    if (
      event.type !== "download.progress" &&
      event.type !== "download.complete" &&
      event.type !== "download.error" &&
      event.type !== "download.cancelled"
    ) {
      return;
    }
    const eventKind = (event as DownloadEvent).kind;
    if (kind === undefined) {
      // Legacy Parakeet path: forward only events without a kind tag.
      if (eventKind !== undefined) return;
    } else {
      // Named-kind path: forward only events matching the requested kind.
      if (eventKind !== kind) return;
    }
    handler(event as DownloadEvent);
  });
}
