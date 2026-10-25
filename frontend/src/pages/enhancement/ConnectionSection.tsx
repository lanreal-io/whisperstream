import { motion, AnimatePresence } from "framer-motion";
import { providerDataTooltip } from "./providerMetadata";
import { ProviderDataGlyph } from "./Glyphs";


/* ---------- Connection block (issue 1) ----------
 * Stateful collapsible card that owns the Provider / API key / Model controls.
 * Three shapes, driven by `state` + `expanded`:
 *
 *   connected (collapsed)   one-line summary: `● <Provider> · Connected   <Model>  ⌄`
 *                           (Local: `● Local · Ready   <model>  ⌄`). Click the
 *                           row or caret to expand the controls inline.
 *   connected (expanded)    the full controls + a "Done" affordance to re-collapse.
 *   unconfigured            guided setup card: title "Connect a cleanup model",
 *                           description, and the provider/key controls. Always
 *                           expanded (the parent forces `expanded`); never a modal.
 *   failed                  same as connected-collapsed but the dot is rose and
 *                           the label reads "Connection failed"; click expands to
 *                           the key field.
 *
 * Frost-glass tokens: Level-3 control surface
 * (white/0.30, border white/60, rounded-3xl), mint dot for connected/ready,
 * rose dot for failed, frost-gradient primary action. The controls themselves
 * (provider dropdown + key/Ollama fields + model picker) are threaded in via the
 * `controls` prop unchanged from the old always-expanded layout.
 */
export function ConnectionSection({
  index,
  state,
  provider,
  providerLabel,
  summaryModelLabel,
  reduceMotion,
  expanded,
  onToggleExpanded,
  controls,
  customHost,
  ollamaEndpoint,
}: {
  index: number;
  state: "connected" | "unconfigured" | "failed";
  provider: string;
  providerLabel: string;
  summaryModelLabel: string;
  reduceMotion: boolean;
  expanded: boolean;
  onToggleExpanded: () => void;
  controls: React.ReactNode;
  customHost?: string;
  ollamaEndpoint?: string;
}) {
  // Collapsed-summary label. The connection STATUS (Connected / Connection
  // failed / Ready) lives in the top-right page-heading pill again, so the
  // summary row carries only the provider name + its data-handling glyph (and,
  // for cloud, the model name). The glyph is the shared ProviderDataGlyph
  // (Shield for Local, Cloud for external providers) so it carries the same
  // data-handling hover tooltip as the dropdown rows, not just a bare icon.

  // Surface tokens (Level-3 control surface from system.md).
  const cardStyle: React.CSSProperties = {
    background: "var(--surface-control)",
    border: "1px solid var(--border-standard)",
    boxShadow: "inset 0 1px 0 rgb(255 255 255 / calc(0.8 * var(--rim-scale)))",
  };

  // Collapsed-summary detail. For Custom the "Custom" label names no destination,
  // so the summary shows the configured host alongside the model. Round 03 renders
  // these as DISCRETE segments (host primary, model dimmer, quiet separator with
  // air) instead of a tight joined string — see the summary span below. Other
  // providers show just the model.

  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.03, type: "spring", stiffness: 400, damping: 30 }}
    >
      {state === "unconfigured" ? (
        // ---- Guided setup card (first-run / no key) ----
        <div className="rounded-[var(--radius-card)] p-5" style={cardStyle}>
          <div className="text-[13px] font-medium text-slate">Connect a cleanup model</div>
          <div className="text-[11px] text-steel mt-0.5 mb-3">
            Pick a provider and add its API key.
          </div>
          {controls}
        </div>
      ) : (
        // ---- Connected / failed: collapsible summary ----
        // No `overflow-hidden` on the card: the data-handling HoverTooltip on the
        // summary glyph must escape downward. The expand body has its own
        // `overflow: hidden` (below) and the provider dropdown is portalled, so
        // the card's rounded corners (from its own bg/border radius) are enough.
        <div className="rounded-[var(--radius-card)]" style={cardStyle}>
          <button
            type="button"
            onClick={onToggleExpanded}
            aria-expanded={expanded}
            aria-label={`${providerLabel}${
              summaryModelLabel ? `, ${summaryModelLabel}` : ""
            }. ${providerDataTooltip(provider, customHost, ollamaEndpoint)}`}
            className="w-full flex items-center gap-2 px-5 h-12 text-left cursor-pointer"
          >
            <ProviderDataGlyph
              value={provider}
              customHost={customHost}
              ollamaEndpoint={ollamaEndpoint}
              className="shrink-0"
            />
            <span className="text-[13px] font-medium text-slate truncate">
              {providerLabel}
            </span>
            <span className="flex-1" />
            {!expanded && provider === "custom" && (customHost || summaryModelLabel) && (
              <span className="flex items-center gap-1.5 text-[12px] max-w-[60%] min-w-0">
                {customHost && (
                  <span className="text-steel shrink-0 truncate max-w-[55%]">{customHost}</span>
                )}
                {customHost && summaryModelLabel && (
                  <span className="text-steel/30 shrink-0" aria-hidden="true">·</span>
                )}
                {summaryModelLabel && (
                  <span className="text-steel/55 min-w-0 truncate">{summaryModelLabel}</span>
                )}
              </span>
            )}
            {!expanded && provider !== "custom" && summaryModelLabel && (
              <span className="text-[12px] text-steel/80 truncate max-w-[40%]">
                {summaryModelLabel}
              </span>
            )}
            <motion.svg
              animate={{ rotate: expanded ? 180 : 0 }}
              transition={{ duration: 0.2 }}
              width="12"
              height="12"
              viewBox="0 0 12 12"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              className="text-steel shrink-0"
              aria-hidden="true"
            >
              <path d="M3 4.5l3 3 3-3" />
            </motion.svg>
          </button>

          <AnimatePresence initial={false}>
            {expanded && (
              <motion.div
                key="connection-body"
                initial={reduceMotion ? false : { height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={reduceMotion ? { opacity: 0 } : { height: 0, opacity: 0 }}
                transition={
                  reduceMotion
                    ? { duration: 0 }
                    : { type: "spring", stiffness: 400, damping: 30 }
                }
                style={{ overflow: "hidden" }}
              >
                <div className="px-5 pb-5 pt-1">
                  {controls}
                  <div className="flex justify-end mt-3">
                    <button
                      type="button"
                      onClick={onToggleExpanded}
                      className="inline-flex items-center gap-1 text-[11px] font-medium text-frost-deep hover:underline cursor-pointer"
                    >
                      Done
                    </button>
                  </div>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}
    </motion.div>
  );
}
