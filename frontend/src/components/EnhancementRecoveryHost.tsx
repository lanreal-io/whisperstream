import { useCallback, useEffect, useRef, useState } from "react";
import { cancelLocalLLMDownload, getLocalRecovery, startLocalLLMDownload, type MissingEnhancementModel } from "../lib/api/enhancement";
import type { AppEvent } from "../lib/socket/events";
import { subscribeToDownloadEvents } from "../lib/socket/downloads";
import { DownloadDialog } from "./DownloadDialog";
import { EnhancementRecoveryDialog } from "./EnhancementRecoveryDialog";

export function EnhancementRecoveryHost({ enabled, connected, blocked, subscribe, onOpenChange }: {
  enabled: boolean;
  connected: boolean;
  blocked: boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  onOpenChange?: (open: boolean) => void;
}) {
  const [model, setModel] = useState<MissingEnhancementModel | null>(null);
  const [downloading, setDownloading] = useState(false);
  const dismissed = useRef<string | null>(null);
  const request = useRef(0);
  const startPromise = useRef<Promise<void> | null>(null);
  const started = useRef(false);
  useEffect(() => {
    onOpenChange?.(downloading || !!model);
    return () => onOpenChange?.(false);
  }, [downloading, model, onOpenChange]);
  useEffect(() => {
    if (!enabled || !connected || blocked) { setModel(null); return; }
    let alive = true;
    const check = async () => {
      const generation = ++request.current;
      try {
        const status = await getLocalRecovery();
        if (!alive || generation !== request.current) return;
        if (downloading) {
          // Reconcile a terminal event missed during a socket disconnect.
          if (started.current && !status.busy) {
            started.current = false;
            startPromise.current = null;
            setDownloading(false);
          } else return;
        }
        if (!status.model) dismissed.current = null;
        setModel(!status.busy && status.model?.id !== dismissed.current ? status.model : null);
      } catch { /* A failed probe is not evidence of a missing file. */ }
    };
    if (!downloading || started.current) void check();
    const onVisible = () => { if (document.visibilityState === "visible") void check(); };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    const unsubscribe = subscribe((event) => {
      if (event.type === "enhancement.error" || event.type === "download.complete" ||
          event.type === "download.cancelled" || event.type === "llama.status") void check();
    });
    return () => {
      alive = false;
      unsubscribe();
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [enabled, connected, blocked, downloading, subscribe]);

  // Keep the exact accepted target through the download. Recovery never selects it.
  const target = useRef<MissingEnhancementModel | null>(null);
  const dismiss = useCallback(() => {
    dismissed.current = target.current?.id ?? model?.id ?? null;
    startPromise.current = null;
    started.current = false;
    setDownloading(false);
    setModel(null);
  }, [model]);
  const localEvents = useCallback((handler: (event: AppEvent) => void) =>
    subscribeToDownloadEvents(subscribe, (event) => {
      if (event.model_id !== target.current?.id) return;
      if (event.type === "download.error" || event.type === "download.cancelled") {
        startPromise.current = null;
        started.current = false;
      }
      handler(event.type === "download.error" && event.reason !== "model_benchmark_required"
        ? { ...event, message: "The model download was interrupted. Check your connection and free disk space, then retry." }
        : event);
    }, "local_llm"), [subscribe]);

  if (downloading && target.current) {
    return <DownloadDialog title="Restoring local enhancement"
      subtitle="You can keep dictating while the model downloads."
      subscribe={localEvents}
      startFn={async () => {
        // StrictMode may run the dialog's mount effect twice. Own one request.
        if (!startPromise.current) {
          startPromise.current = startLocalLLMDownload(target.current!.id, true)
            .then(() => { started.current = true; })
            .catch(() => {
              startPromise.current = null;
              throw new Error("The download could not start. Check your connection and free disk space, then retry. If the model is still in use, restart Whisperstream first.");
            });
        }
        await startPromise.current;
      }}
      cancelFn={async () => {
        // Do not cancel another transfer before our own start is acknowledged.
        try { await startPromise.current; } catch { dismiss(); return; }
        await cancelLocalLLMDownload();
        // Cancellation can finish while disconnected, with no WS event to close us.
        dismiss();
      }}
      onCancel={async () => dismiss()}
      onComplete={() => { started.current = false; startPromise.current = null; dismissed.current = null; setDownloading(false); setModel(null); }} />;
  }
  if (!enabled || !connected || blocked || !model) return null;
  return <EnhancementRecoveryDialog model={model} onDismiss={dismiss}
    onDownload={() => { target.current = model; setDownloading(true); }} />;
}
