import { useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";

interface PromptFullscreenOverlayProps {
  value: string;
  onChange: (v: string) => void;
  onClose: () => void;
}

// Mounted at App root (not inside <main>) so it escapes the backdrop-blur-xl
// containing block. Always receives the current prompt value via props so the
// inline textarea and this overlay stay in sync through shared React state.
export function PromptFullscreenOverlay({
  value,
  onChange,
  onClose,
}: PromptFullscreenOverlayProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Focus textarea on mount.
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  // Esc key closes the overlay. Cleaned up on unmount to avoid listener leaks.
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose]);

  return (
    <AnimatePresence>
      {/* Backdrop */}
      <motion.div
        key="prompt-overlay-backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        className="fixed inset-0 z-50"
        style={{
          background: `
            radial-gradient(ellipse at 20% 50%, var(--frost-mesh-1) 0%, transparent 50%),
            radial-gradient(ellipse at 80% 20%, var(--frost-mesh-2) 0%, transparent 45%),
            radial-gradient(ellipse at 45% 80%, var(--frost-mesh-3) 0%, transparent 50%),
            var(--backdrop-strong)
          `,
          backdropFilter: "blur(8px)",
        }}
      >
        {/* Panel */}
        <motion.div
          key="prompt-overlay-panel"
          initial={{ opacity: 0, scale: 0.97 }}
          animate={{ opacity: 1, scale: 1 }}
          exit={{ opacity: 0, scale: 0.97 }}
          transition={{ duration: 0.2 }}
          className="absolute inset-0 flex items-center justify-center p-8"
        >
          <div
            className="relative flex flex-col rounded-[var(--radius-card)] w-full"
            style={{
              maxWidth: "640px",
              height: "80vh",
              background: "var(--surface-modal)",
              border: "1px solid var(--border-emphasis)",
              boxShadow: "var(--shadow-glass)",
            }}
          >
            {/* Header */}
            <div
              className="flex items-center justify-between px-6 py-4 shrink-0"
              style={{ borderBottom: "1px solid var(--divider)" }}
            >
              <span
                className="text-[14px] font-medium"
                style={{ color: "var(--color-slate)" }}
              >
                Edit prompt
              </span>
              <button
                onClick={onClose}
                className="h-8 px-4 rounded-full text-[13px] font-medium cursor-pointer"
                style={{
                  background: "var(--accent-fill)",
                  color: "#fff",
                  boxShadow: "var(--shadow-frost-glow)",
                  border: "none",
                }}
              >
                Done
              </button>
            </div>

            {/* Textarea */}
            <textarea
              ref={textareaRef}
              value={value}
              onChange={(e) => onChange(e.target.value)}
              className="flex-1 resize-none outline-none min-h-0 px-6 py-4 rounded-b-3xl"
              style={{
                fontFamily:
                  "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
                fontSize: "13px",
                lineHeight: 1.6,
                color: "var(--color-slate)",
                background: "var(--surface-control)",
                border: "none",
              }}
              spellCheck={false}
            />
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}
