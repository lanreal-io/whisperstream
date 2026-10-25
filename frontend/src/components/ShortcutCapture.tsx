import { useEffect, useRef, useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import type { AppEvent } from "../lib/socket/events";
import { formatShortcut, shortcutDisplayParts } from "../lib/shortcutDisplay";

/**
 * Owner trial (2026-10-25): a bound shortcut rests as one chip per key in an
 * inset field, the same chips the field shows while capturing. "text" restores
 * the raised pill with the shortcut as plain text. Flip this one constant; if
 * it goes back to "text", set `SLIDER_FIELD_STYLE` to "bare" as well.
 */
export const SHORTCUT_DISPLAY_STYLE: "chips" | "text" = "chips";

interface ShortcutCaptureProps {
  value: string;
  onChange: (keys: string) => void;
  send: (msg: Record<string, unknown>) => boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  connected: boolean;
  captureTarget?: string;
  clearable?: boolean; // default true; onboarding passes false
  clearAriaLabel?: string; // default "Clear shortcut"
}

export function ShortcutCapture({
  value,
  onChange,
  send,
  subscribe,
  connected,
  captureTarget,
  clearable = true,
  clearAriaLabel,
}: ShortcutCaptureProps) {
  const [capturing, setCapturing] = useState(false);
  const [liveKeys, setLiveKeys] = useState("");
  const [captureFailed, setCaptureFailed] = useState(false);

  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const capturingRef = useRef(false);

  const startCapture = useCallback(() => {
    if (!connected) return;
    setCaptureFailed(false);
    if (!send({ type: "shortcut.capture.start", target: captureTarget ?? "primary" })) return;
    capturingRef.current = true;
    setCapturing(true);
    setLiveKeys("");
    buttonRef.current?.blur();
    console.log("[Hotkey] focus blurred reason=capture-start");
    console.log("[Hotkey] capture mode entered");
  }, [send, connected, captureTarget]);

  const stopCapture = useCallback(() => {
    capturingRef.current = false;
    setCapturing(false);
    setLiveKeys("");
    send({ type: "shortcut.capture.stop" });
    console.log("[Hotkey] capture mode cancelled");
  }, [send]);

  useEffect(() => {
    capturingRef.current = capturing;
  }, [capturing]);

  const clearShortcut = useCallback((event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
    event.stopPropagation();
    capturingRef.current = false;
    setCapturing(false);
    setLiveKeys("");
    send({ type: "shortcut.capture.stop" });
    onChange("");
    console.log("[Hotkey] capture mode exited reason=clear");
  }, [onChange, send]);

  const targetId = captureTarget ?? "primary";
  useEffect(() => {
    return subscribe((event) => {
      if (!capturingRef.current) return;
      if (event.type === "shortcut.capture.failed") {
        if (event.target !== undefined && event.target !== targetId) return;
        capturingRef.current = false;
        setCapturing(false);
        setLiveKeys("");
        setCaptureFailed(true);
        return;
      }
      // Ignore events meant for a different capture target. The backend echoes
      // the target on both capturing/captured; with multiple ShortcutCapture
      // instances mounted (per-profile + Controls), only the matching one reacts.
      // (`target` may be absent on older backends -> falls through, preserving
      // the single-instance behavior.)
      if (event.type === "shortcut.capturing" || event.type === "shortcut.captured") {
        if (event.target !== undefined && event.target !== targetId) return;
        if (event.type === "shortcut.capturing") {
          setLiveKeys(event.keys);
        } else {
          capturingRef.current = false;
          setCapturing(false);
          setLiveKeys("");
          console.log(`[Hotkey] capture mode exited key=${event.keys}`);
          onChange(event.keys);
        }
      }
    });
  }, [subscribe, onChange, targetId]);

  useEffect(() => {
    if (!connected && capturingRef.current) {
      capturingRef.current = false;
      setCapturing(false);
      setLiveKeys("");
      setCaptureFailed(true);
    }
  }, [connected]);

  useEffect(() => {
    return () => {
      if (capturingRef.current) {
        send({ type: "shortcut.capture.stop" });
      }
    };
  }, [send]);

  // Esc-to-cancel. The button is blurred on capture start, so its onKeyDown
  // never fires here; listen on window instead. Flip the ref synchronously so
  // a late shortcut.captured for "esc" (the OS hook still sees the key) is
  // dropped by the subscribe guard and never binds.
  useEffect(() => {
    if (!capturing) return;
    const onWindowKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        capturingRef.current = false;
        stopCapture();
      }
    };
    window.addEventListener("keydown", onWindowKey, true);
    return () => window.removeEventListener("keydown", onWindowKey, true);
  }, [capturing, stopCapture]);

  const keyPills = liveKeys ? shortcutDisplayParts(liveKeys) : [];
  const chips = SHORTCUT_DISPLAY_STYLE === "chips" && !capturing;
  const showClear = clearable && value !== "" && capturing;

  return (
    <div className="inline-flex flex-col items-stretch">
      <div className="relative inline-flex items-center">
        <button
          ref={buttonRef}
          onClick={capturing ? stopCapture : startCapture}
          onKeyDown={(e) => { if (capturing) { e.preventDefault(); e.stopPropagation(); } }}
          className={`relative min-w-[120px] rounded-xl cursor-pointer flex items-center justify-center gap-1.5 ${chips ? "h-[33.5px] pl-2" : "py-1.5 pl-4"}${showClear ? " pr-10" : chips ? " pr-2" : " pr-4"}`}
          style={{
            background: capturing || chips
              ? "var(--surface-control)"
              : "var(--surface-sidebar)",
            // No backdrop blur: Chromium enabled it only after the tab's fade-in,
            // so a gray patch faded in late on Dark.
            border: capturing
              ? "1px solid var(--border-accent)"
              : chips
                ? "1px solid var(--border-hairline-steel)"
                : "1px solid var(--border-standard)",
            boxShadow: capturing
              ? "var(--shadow-frost-glow), inset 0 1px 0 rgb(255 255 255 / calc(0.8 * var(--rim-scale)))"
              : chips
                ? "none"
                : "var(--shadow-glass-sm)",
            transition: "background 200ms, border 200ms, box-shadow 200ms",
          }}
        >
          {capturing && (
            <motion.div
              className="absolute inset-0 rounded-[12px]"
              initial={{ opacity: 0 }}
              animate={{
                opacity: 1,
                boxShadow: [
                  "0 0 20px var(--color-frost-light)",
                  "var(--shadow-frost-glow)",
                  "0 0 20px var(--color-frost-light)",
                ],
              }}
              exit={{ opacity: 0 }}
              transition={{ duration: 2, repeat: Infinity, ease: "easeInOut" }}
            />
          )}

          <AnimatePresence mode="popLayout">
            {capturing ? (
              keyPills.length > 0 ? (
                <motion.div
                  key="pills"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.1 }}
                  className="relative z-10 flex items-center gap-1"
                >
                  {keyPills.map((key, i) => (
                    <motion.span
                      key={`${key}-${i}`}
                      initial={{ scale: 0.8, opacity: 0 }}
                      animate={{ scale: 1, opacity: 1 }}
                      transition={{ duration: 0.1 }}
                      className="px-2 py-0.5 bg-white/70 rounded-full text-[12px] font-medium text-slate border border-white/80"
                      style={{ boxShadow: "0 2px 8px color-mix(in srgb, var(--color-frost) 20%, transparent)" }}
                    >
                      {key}
                    </motion.span>
                  ))}
                </motion.div>
              ) : (
                <motion.span
                  key="prompt"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.1 }}
                  className="relative z-10 text-[12px] text-steel"
                >
                  Press shortcut
                </motion.span>
              )
            ) : (
              chips && value ? (
                <motion.span
                  key="value"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.1 }}
                  className="relative z-10 flex items-center gap-1"
                >
                  {/* The chips are visual; the button keeps the same spoken
                      name as the text style. */}
                  <span className="sr-only">{formatShortcut(value)}</span>
                  {shortcutDisplayParts(value).map((key, i) => (
                    <span
                      key={`${key}-${i}`}
                      aria-hidden="true"
                      className="rounded-md border border-[var(--border-hairline-steel)] bg-[var(--surface-raised)] px-2 py-px text-[13px] font-medium leading-5 tracking-wide text-slate"
                    >
                      {key}
                    </span>
                  ))}
                </motion.span>
              ) : (
                <motion.span
                  key="value"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.1 }}
                  className="relative z-10 text-[13px] font-medium text-slate tracking-wide"
                >
                  {value ? formatShortcut(value) : "Not set"}
                </motion.span>
              )
            )}
          </AnimatePresence>
        </button>

        {showClear && (
          <button
            type="button"
            onClick={clearShortcut}
            aria-label={clearAriaLabel ?? "Clear shortcut"}
            className="absolute right-1 top-1/2 z-20 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded-lg text-[18px] leading-none text-steel/60 transition-colors hover:text-rose cursor-pointer"
          >
            {"\u00d7"}
          </button>
        )}
      </div>

      {capturing && (
        <span className="mt-1 text-center text-[11px] leading-none text-steel/60">
          Esc to cancel
        </span>
      )}
      {captureFailed && <span role="status" className="mt-2 text-[12px] text-steel">Your shortcut isn't ready. Try again.</span>}
    </div>
  );
}
