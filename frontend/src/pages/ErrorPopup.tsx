import { useState, useEffect, useCallback, useRef } from "react";
import { motion } from "framer-motion";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { listen, emit } from "@tauri-apps/api/event";
import { PopupShell } from "../components/PopupShell";
import { flushLogSync } from "../lib/logCapture";
import { type ErrorEmitPayload } from "../lib/api/errors";
import { applyWindowLifecycle, publishPopupVisibility } from "../lib/windowLifecycle";

async function hideAndFlush() {
  try {
    await flushLogSync();
  } catch {
    /* ignore */
  }
  if (!(await applyWindowLifecycle("error", "hide"))) {
    await getCurrentWindow().hide();
    await publishPopupVisibility("popup-error", false);
  }
}

function ErrorContent({ payload }: { payload: ErrorEmitPayload }) {
  const primaryRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    primaryRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        console.log("[Popup] error dismiss esc code=" + payload.code);
        hideAndFlush();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [payload.code]);

  const handleDismiss = useCallback(() => {
    console.log(`[Popup] error dismiss code=${payload.code}`);
    hideAndFlush();
  }, [payload.code]);

  return (
    <div className="relative">
      <motion.h2
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.12, type: "spring", stiffness: 400, damping: 30 }}
        className="font-display text-[18px] font-normal text-slate text-center mb-1.5"
      >
        {payload.title}
      </motion.h2>

      <motion.p
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.14, type: "spring", stiffness: 400, damping: 30 }}
        className="text-[13px] text-steel text-center mb-5"
      >
        {payload.message}
      </motion.p>

      <motion.div
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.18, type: "spring", stiffness: 400, damping: 30 }}
        className="flex flex-col items-center"
      >
        <button
          ref={primaryRef}
          onClick={handleDismiss}
          className="relative overflow-hidden px-8 py-2 rounded-full text-[13px] font-medium text-white transition-all duration-300 cursor-pointer group"
          style={{
            background: "var(--accent-fill)",
            border: "1px solid var(--border-accent)",
            boxShadow:
              "0 4px 20px -4px rgba(126, 174, 212, 0.40), inset 0 1px 0 rgb(255 255 255 / calc(0.45 * var(--rim-scale)))",
          }}
        >
          <span
            className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-200 pointer-events-none"
            style={{
              background:
                "linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.30) 50%, transparent 100%)",
            }}
          />
          <span className="relative z-10">Okay</span>
        </button>
      </motion.div>
    </div>
  );
}

const DEV_COPY: Record<string, ErrorEmitPayload> = {
  no_input_device: {
    code: "no_input_device",
    title: "No microphone found",
    message: "Plug in a microphone to start recording.",
  },
  backend_disconnect: {
    code: "backend_disconnect",
    title: "Whisperstream stopped responding",
    message:
      "Restart Whisperstream to recover. Antivirus software can sometimes interrupt it.",
  },
  model_load_failed: {
    code: "model_load_failed",
    title: "Couldn't load the speech model",
    message:
      "Restart Whisperstream to try again. You can also re-download the model from settings.",
  },
  stt_engine_failed: {
    code: "stt_engine_failed",
    title: "Speech model stopped",
    message:
      "Whisperstream is still running, but the speech model couldn't recover. Reload the model to try again.",
  },
  audio_capture_failed: {
    code: "audio_capture_failed",
    title: "Couldn't start recording",
    message: "Check that your microphone is connected, then try again.",
  },
};

export function ErrorPopup() {
  const devCodePayload = import.meta.env.DEV
    ? DEV_COPY[new URLSearchParams(window.location.search).get("devCode") ?? ""] ?? null
    : null;

  const [payload, setPayload] = useState<ErrorEmitPayload | null>(devCodePayload);

  useEffect(() => {
    let unlistenFn: (() => void) | null = null;
    (async () => {
      try {
        const w = getCurrentWindow();
        const u = await w.listen<ErrorEmitPayload>("popup-error:data", (e) => {
          console.log(`[Popup] error data received code=${e.payload.code}`);
          setPayload(e.payload);
        });
        unlistenFn = () => u();
        // Ask the main window to (re-)send the latest payload now that our
        // listener is active. Recovers from the IPC race where the original
        // :data emit landed before the popup's listener registered. Mirrors
        // EulaPopup's getEulaStatus() and the update popup's candidate fallback.
        await emit("popup-error:fetch");
        console.log("[Popup] error fetch requested");
      } catch {
        /* outside Tauri / pure-Vite dev — no-op */
      }
    })();
    return () => {
      if (unlistenFn) unlistenFn();
    };
  }, []);

  const [mountKey, setMountKey] = useState(0);
  useEffect(() => {
    const unlisten = listen("popup-error:show", () => {
      console.log("[Popup] error show mountKey reset");
      setMountKey((k) => k + 1);
    });
    return () => {
      unlisten.then((fn) => fn());
    };
  }, []);

  if (!payload) {
    console.warn("[Popup] error rendered without payload mountKey=" + mountKey);
  }

  return (
    <PopupShell width={380}>
      {payload ? <ErrorContent key={mountKey} payload={payload} /> : null}
    </PopupShell>
  );
}
