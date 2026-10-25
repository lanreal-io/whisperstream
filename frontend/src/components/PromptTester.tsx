import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { previewEnhancement, type PreviewError } from "../lib/api/enhancement";
import { enhancementErrorMessage } from "../lib/enhancementConsent";

export type PromptTesterGate = "ready" | "warming" | "disconnected" | "disabled" | "consent";

/* ---------- Per-profile "Try it" prompt tester ----------
 * A collapsed `▸ Try it` disclosure inside a profile's editor. Expanded: a
 * Sample-input field + a Run button. Run sends the editor prompt and profile id
 * to POST /api/enhancement/preview. Editable-prompt models use the former;
 * fixed-prompt local models resolve the latter through the registry contract.
 *
 * Gating is driven by *gate* (computed once in the parent from the connection
 * status, shared with the Connection summary row):
 *   "ready"        — Run enabled.
 *   "warming"      — Local model not warm yet; Run disabled + warming hint.
 *   "disconnected" — no model connected; Run disabled + connect hint.
 *
 * State is local and resets when the editor closes because the host re-keys this
 * component on the profile id (so a fresh mount clears sample + output).
 *
 * Round 04 (issue 12b): a lighter, more compact inline variant. The expanded
 * body is a single inline row (flush sample input + fused `Run`) followed by the
 * output well (`max-h-[96px]`, self-scroll). The legacy stacked layout (labelled
 * "Sample input" field over a separate Run button, taller output) stays the
 * default so any other caller is unaffected; the editor opts into `compact`.
 */
export function PromptTester({
  systemPrompt,
  profileId,
  gate,
  insetStyle,
  reducedMotion,
  compact = false,
}: {
  systemPrompt: string;
  /** Lets fixed-prompt local models resolve the selected profile's registry mode. */
  profileId?: string;
  gate: PromptTesterGate;
  insetStyle: React.CSSProperties;
  reducedMotion: boolean;
  /** Round 04 lighter inline variant for the single-column editor. */
  compact?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [sample, setSample] = useState("");
  const [output, setOutput] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [admissionBlocked, setAdmissionBlocked] = useState(false);
  useEffect(() => { setAdmissionBlocked(false); setError(null); }, [gate]);

  const canRun = gate === "ready" && sample.trim().length > 0 && !running && !admissionBlocked;
  // Hint shown under the Run button when the gate blocks running. Verbatim
  // copy-review strings (no em dashes).
  const gateHint =
    gate === "disabled"
      ? "Turn on Enhancement to test prompts or check the connection."
      : gate === "consent"
        ? "Review the privacy notice to test prompts with this connection."
      : gate === "warming"
      ? "Warming up the local model…"
      : gate === "disconnected"
        ? "Connect a model to test prompts."
        : null;

  const run = async () => {
    if (!canRun) return;
    setRunning(true);
    setError(null);
    setOutput(null);
    try {
      const result = profileId
        ? await previewEnhancement(sample, systemPrompt, profileId)
        : await previewEnhancement(sample, systemPrompt);
      setOutput(result);
    } catch (err) {
      const code = (err as PreviewError)?.errorCode;
      if (["consent_required", "consent_changed", "enhancement_disabled"].includes(code ?? "")) setAdmissionBlocked(true);
      // not_configured is the same "no model" situation the gate guards; show
      // the connect hint. Any other failure is a provider/network fault.
      setError(
        code === "not_configured"
          ? "Connect a model to test prompts."
          : enhancementErrorMessage(err, "Could not run the test. Check your connection."),
      );
    } finally {
      setRunning(false);
    }
  };

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-[11px] text-steel/70 hover:text-slate transition-colors cursor-pointer"
      >
        <motion.svg
          animate={{ rotate: open ? 90 : 0 }}
          transition={{ duration: 0.15 }}
          width="10"
          height="10"
          viewBox="0 0 10 10"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          aria-hidden="true"
        >
          <path d="M3.5 2.5l3 2.5-3 2.5" />
        </motion.svg>
        Try it
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="tester-body"
            initial={reducedMotion ? false : { height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={reducedMotion ? { opacity: 0 } : { height: 0, opacity: 0 }}
            transition={
              reducedMotion
                ? { duration: 0 }
                : { type: "spring", stiffness: 400, damping: 30 }
            }
            style={{ overflow: "hidden" }}
          >
            <p className="text-[11px] text-steel mt-2">Sends your sample and these instructions to the selected connection.</p>
            {compact ? (
              // ---- Lighter inline variant (round 04) ----
              <div className="mt-2 space-y-2">
                <div
                  className="flex items-stretch rounded-full overflow-hidden"
                  style={insetStyle}
                >
                  <input
                    value={sample}
                    onChange={(e) => setSample(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && canRun) {
                        e.preventDefault();
                        run();
                      }
                    }}
                    placeholder="Try a sample..."
                    aria-label="Sample input"
                    className="flex-1 min-w-0 bg-transparent px-4 h-9 text-[12px] font-body text-slate placeholder:text-steel/40 outline-none"
                  />
                  <button
                    type="button"
                    onClick={run}
                    disabled={!canRun}
                    aria-label="Run prompt test"
                    className="shrink-0 inline-flex items-center justify-center h-9 px-4 text-[12px] font-medium cursor-pointer disabled:cursor-default transition-all duration-150"
                    style={
                      canRun
                        ? {
                            background: "var(--accent-fill)",
                            color: "#fff",
                          }
                        : {
                            background: "rgba(255,255,255,0.2)",
                            color: "rgba(107,130,153,0.5)",
                          }
                    }
                  >
                    {running ? "Running…" : "Run"}
                  </button>
                </div>

                {gateHint && <p className="text-[11px] text-steel/70">{gateHint}</p>}
                {error && <p role="alert" className="text-[11px] text-rose/90">{error}</p>}

                {output !== null && !error && (
                  <div
                    className="rounded-2xl px-4 py-2.5 text-[12px] leading-relaxed font-body text-slate whitespace-pre-wrap max-h-[96px] overflow-y-auto"
                    style={{
                      background: "var(--surface-control)",
                      border: "1px solid var(--border-hairline)",
                    }}
                  >
                    {output}
                  </div>
                )}
              </div>
            ) : (
              // ---- Legacy stacked variant (default) ----
              <div className="mt-2 space-y-2">
                <div className="text-[11px] text-steel font-medium">Sample input</div>
                <div className="flex items-start gap-2">
                  <input
                    value={sample}
                    onChange={(e) => setSample(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && canRun) {
                        e.preventDefault();
                        run();
                      }
                    }}
                    placeholder="Type a sample to clean up"
                    aria-label="Sample input"
                    className="flex-1 min-w-0 rounded-full px-4 h-9 text-[12px] font-body text-slate placeholder:text-steel/40 outline-none transition-all duration-200"
                    style={insetStyle}
                  />
                  <button
                    type="button"
                    onClick={run}
                    disabled={!canRun}
                    aria-label="Run prompt test"
                    className="shrink-0 inline-flex items-center justify-center h-9 px-4 rounded-full text-[12px] font-medium cursor-pointer disabled:cursor-default transition-all duration-150"
                    style={
                      canRun
                        ? {
                            background: "var(--accent-fill)",
                            color: "#fff",
                            border: "1px solid var(--border-accent)",
                            boxShadow: "var(--shadow-frost-glow)",
                          }
                        : {
                            background: "rgba(255,255,255,0.06)",
                            color: "rgba(107,130,153,0.5)",
                            border: "1px solid rgba(255,255,255,0.2)",
                          }
                    }
                  >
                    {running ? "Running…" : "Run"}
                  </button>
                </div>

                {gateHint && <p className="text-[11px] text-steel/70">{gateHint}</p>}

                {error && <p role="alert" className="text-[11px] text-rose/90">{error}</p>}

                {output !== null && !error && (
                  <div
                    className="rounded-2xl px-4 py-3 text-[12px] leading-relaxed font-body text-slate whitespace-pre-wrap max-h-[120px] overflow-y-auto"
                    style={{
                      background: "var(--surface-control)",
                      border: "1px solid var(--border-emphasis)",
                      boxShadow: "inset 0 2px 4px rgba(0,0,0,0.02)",
                    }}
                  >
                    {output}
                  </div>
                )}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
