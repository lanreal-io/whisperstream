import { ModelDeleteConfirmation, useModelDeleteConfirmation } from "./ModelDeleteConfirmation";
import { type ProviderValue, providerDataTooltip, PROVIDERS, providerStaysLocal } from "./providerMetadata";
import { type StatusState } from "./connectionStatus";
import { useRef, Fragment } from "react";
import { DownloadMiniIcon, ProviderDataGlyph } from "./Glyphs";
import { motion, AnimatePresence } from "framer-motion";
import { AnchoredPopover } from "../../components/AnchoredPopover";
import { HoverTooltip } from "../../components/HoverTooltip";


type ProviderDropdownProps = {
  open: boolean;
  setOpen: (v: boolean) => void;
  currentLabel: string;
  currentValue: string;
  /** Called with the newly-picked provider value. The parent owns both
   *  the DataLeavesDialog gate and recipient review; this prop is the
   *  single seam between dropdown and parent.
   *  Widened to include "local" per scope.md "Provider model" — picking
   *  Local is a dialog-free direct write (parent handler in Task 2.9). */
  onProviderPicked: (newProvider: ProviderValue) => void;
  setStatus: (s: StatusState) => void;
  /** True when the current local model GGUF is on disk. Drives the
   *  Decision 3 state machine on the Local row. */
  localModelInstalled: boolean;
  /** False for developer-owned candidate artifacts. Their files never expose
   *  Whisperstream's managed download or delete affordances. */
  localModelManaged: boolean;
  /** ID of the local model the user is "intended" to use (config value
   *  with bundled-catalog fallback). Passed to onDeleteLocalModel so the
   *  parent knows which GGUF to remove. */
  currentLocalModelId: string;
  /** Invoked after the inline `Delete?` confirm. Parent owns the API call
   *  and the local-catalog refetch. */
  onDeleteLocalModel: (modelId: string) => void;
  /** Invoked when the Local row's download glyph is clicked (states 2 and
   *  4 of the Decision 3 state machine). Parent closes the dropdown and
   *  calls requestLocalDownload(currentLocalModel) — existing disk + RAM
   *  gates fire after this; the new EnableLocalConfirmDialog is skipped
   *  because the user is being explicit. */
  onDownloadLocal: () => void;
  /** When true (lockdown mode), cloud and Ollama rows are rendered disabled
   *  (opacity-40, aria-disabled) with a "Turned off by Lockdown Mode."
   *  HoverTooltip. The Local row stays fully enabled and force-selected.
   *  No destructive writes; stored provider and keys persist for restore-on-off. */
  lockdown?: boolean;
  /** Hostname derived from enhancement_custom_base_url (e.g. "openrouter.ai").
   *  Passed to the trigger glyph's tooltip so the Custom tooltip names the
   *  configured host rather than the generic fallback. Menu rows keep the
   *  host-free fallback (they render before configuration). */
  customHost?: string;
  /** Configured Ollama endpoint. Drives local/remote copy and glyphs. */
  ollamaEndpoint?: string;
  disabledProviders?: Record<string, string>;
  providerExplanations?: Record<string, string>;
};

/* Inline affordance shown on the Local row of the provider dropdown.
 * Implements the Decision 3 state machine (design.md + tasks.md Task 2.3).
 * Lives inside its own component so the deletePending sub-state and the
 * check-button focus ref don't leak into the parent dropdown render. */
function LocalRowAffordance({
  provider,
  installed,
  modelId,
  onDelete,
  onDownload,
}: {
  provider: string;
  installed: boolean;
  modelId: string;
  onDelete: (modelId: string) => void;
  onDownload: () => void;
}) {
  const { deletePending, setDeletePending, confirmBtnRef } = useModelDeleteConfirmation(installed, provider);

  const isLocal = provider === "local";

  // State 1: provider == local AND installed -> render nothing.
  if (isLocal && installed) return null;

  // State 2 + 4: download glyph (provider == local + not installed, OR
  // provider != local + not installed). Same button, same aria-label.
  if (!installed) {
    return (
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onDownload();
        }}
        aria-label="Download local model"
        className="w-6 h-6 rounded-full flex items-center justify-center text-steel/70 hover:text-frost-deep opacity-0 group-hover/row:opacity-100 group-focus-within/row:opacity-100 transition-opacity cursor-pointer"
      >
        <DownloadMiniIcon aria-hidden="true" />
      </button>
    );
  }

  // State 3: provider != local AND installed -> trash, with inline
  // Delete? confirm sub-state.
  if (deletePending) {
    return <ModelDeleteConfirmation confirmBtnRef={confirmBtnRef} setDeletePending={setDeletePending} onDelete={onDelete} modelId={modelId} />;
  }

  return (
    <button
      type="button"
      onClick={(e) => {
        e.stopPropagation();
        setDeletePending(true);
      }}
      aria-label="Delete local model"
      className="w-6 h-6 rounded-full flex items-center justify-center text-steel/70 hover:text-rose opacity-0 group-hover/row:opacity-100 group-focus-within/row:opacity-100 transition-opacity cursor-pointer"
    >
      <svg
        viewBox="0 0 12 12"
        width="12"
        height="12"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="M2 3.5h8" />
        <path d="M3 3.5v6.5a1 1 0 001 1h4a1 1 0 001-1V3.5" />
        <path d="M5 3.5v-1a1 1 0 011-1h0a1 1 0 011 1v1" />
        <path d="M5 5.5v3M7 5.5v3" />
      </svg>
    </button>
  );
}

export function ProviderDropdown(
  props: ProviderDropdownProps & { ref?: React.Ref<HTMLDivElement> }
) {
  const {
    open,
    setOpen,
    currentLabel,
    currentValue,
    onProviderPicked,
    setStatus,
    localModelInstalled,
    localModelManaged,
    currentLocalModelId,
    onDeleteLocalModel,
    onDownloadLocal,
    lockdown = false,
    customHost,
    ollamaEndpoint,
    disabledProviders = {},
    providerExplanations = {},
    ref,
  } = props;
  // Every provider now shows a data-handling glyph on the trigger (round 04):
  // a Shield for the managed Local provider, a Cloud when it
  // goes off-device. The glyph carries the privacy note as a hover tooltip,
  // replacing the inline "stays on your machine" / "sent to ..." sentences.
  //
  // The dropdown MENU is portalled out via AnchoredPopover so it escapes the
  // ConnectionSection card's `overflow-hidden` (used for the expand/collapse
  // animation). Without the portal the last option (Anthropic) clipped at the
  // card edge and was unclickable. `triggerRef` anchors the fixed-position
  // menu to the trigger button's bounding rect.
  const triggerRef = useRef<HTMLButtonElement>(null);
  return (
    <div ref={ref} className="relative">
      <button
        ref={triggerRef}
        onClick={() => setOpen(!open)}
        aria-label={`${currentLabel}. ${providerDataTooltip(
          currentValue,
          customHost,
          ollamaEndpoint,
        )}`}
        className="w-full h-11 px-5 rounded-full text-[13px] text-left text-slate flex items-center justify-between cursor-pointer transition-all duration-200"
        style={{
          background: "var(--surface-control)",
          border: open
            ? "1px solid var(--border-accent)"
            : "1px solid var(--border-emphasis)",
          boxShadow: "inset 0 2px 4px rgba(0, 0, 0, 0.02)",
        }}
      >
        <span className="truncate">{currentLabel}</span>
        <span className="flex items-center gap-2 shrink-0 ml-2">
          <ProviderDataGlyph
            value={currentValue}
            customHost={customHost}
            ollamaEndpoint={ollamaEndpoint}
          />
          <motion.svg
            animate={{ rotate: open ? 180 : 0 }}
            transition={{ duration: 0.2 }}
            width="12"
            height="12"
            viewBox="0 0 12 12"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            className="text-steel"
          >
            <path d="M3 4.5l3 3 3-3" />
          </motion.svg>
        </span>
      </button>

      <AnimatePresence>
        {open && (
          <AnchoredPopover anchorRef={triggerRef} align="left" gap={8}>
          <motion.div
            initial={{ opacity: 0, y: -4, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4, scale: 0.98 }}
            transition={{ duration: 0.15 }}
            // mousedown is stopped here so the parent's document-level
            // outside-click handler (keyed off the trigger wrapper) does NOT
            // fire on clicks inside this portalled menu — without the portal
            // the menu lived inside the trigger and was naturally exempt.
            onMouseDown={(e) => e.stopPropagation()}
            className="w-full rounded-[var(--radius-row)] py-1.5"
            style={{
              background: "var(--surface-overlay)",
              backdropFilter: "blur(24px)",
              border: "1px solid var(--border-emphasis)",
              boxShadow: "var(--shadow-glass)",
            }}
          >
            {PROVIDERS.map((opt, i) => {
              const isSelected = opt.value === currentValue;
              const isLocalRow = opt.kind === "local";
              // Surface D (Task 5.7): under lockdown, cloud and Ollama rows are
              // disabled (opacity-40, aria-disabled, HoverTooltip D2).
              // The Local row stays fully enabled and force-selected.
              const isDisabledByLockdown = lockdown && opt.kind !== "local";
              const capabilityDisabledReason = disabledProviders[opt.value];
              const isDisabledByCapability = !!capabilityDisabledReason;
              const rowExplanation = capabilityDisabledReason || providerExplanations[opt.value] || "";
              // Group by data-handling (what the glyph shows), not the `kind`
              // field: Ollama can be local or remote based on its endpoint.
              // Custom is its own group (bring-your-own endpoint).
              // A faint hairline marks each group boundary.
              const groupOf = (v: string) =>
                v === "custom"
                  ? "custom"
                  : providerStaysLocal(v, ollamaEndpoint)
                    ? "device"
                    : "cloud";
              const showDivider =
                i > 0 && groupOf(opt.value) !== groupOf(PROVIDERS[i - 1].value);
              return (
                <Fragment key={opt.value}>
                  {showDivider && (
                    <div
                      className="h-px mx-3 my-1"
                      style={{ background: "var(--divider)" }}
                      aria-hidden="true"
                    />
                  )}
                <div
                  className="group/row relative flex items-center"
                  style={
                    isSelected && !isDisabledByLockdown && !isDisabledByCapability
                      ? {
                          background: "color-mix(in srgb, var(--color-frost) 18%, transparent)",
                          borderLeft: "3px solid var(--border-accent)",
                        }
                      : undefined
                  }
                  onMouseEnter={(e) => {
                    if (!isSelected && !isDisabledByLockdown && !isDisabledByCapability)
                      e.currentTarget.style.background = "color-mix(in srgb, var(--color-frost) 15%, transparent)";
                  }}
                  onMouseLeave={(e) => {
                    if (!isSelected) e.currentTarget.style.background = "transparent";
                  }}
                >
                  <HoverTooltip
                    content={isDisabledByLockdown ? "Turned off by Lockdown Mode." : rowExplanation}
                    side="bottom-left"
                    width="w-56"
                    className="flex-1 min-w-0"
                  >
                  <button
                    type="button"
                    disabled={isDisabledByLockdown || isDisabledByCapability}
                    aria-disabled={isDisabledByLockdown || isDisabledByCapability || undefined}
                    onClick={() => {
                      if (isDisabledByLockdown || isDisabledByCapability) return;
                      setOpen(false);
                      setStatus({ kind: "idle" });
                      // No-op when re-selecting the active provider.
                      if (opt.value === currentValue) return;
                      // Hand off to the parent: it owns DataLeavesDialog,
                      // PrivacyModal chain, and config write. The chain fires
                      // BEFORE any config write so Cancel leaves the dropdown
                      // on the previous selection (dropdown value derives
                      // from config.enhancement_provider).
                      onProviderPicked(opt.value as ProviderValue);
                    }}
                    // ProviderDataGlyph renders a visible role="tooltip" bubble
                    // inside this button; aria-label pins the accessible name to
                    // the provider label so the tooltip text doesn't leak into it.
                    aria-label={`${opt.label}. ${providerDataTooltip(
                      opt.value,
                      opt.value === "custom" ? customHost : undefined,
                      ollamaEndpoint,
                    )}`}
                    // pl-5 / pr-10 mirror the trigger's px-5 + chevron column, so
                    // the data glyph lands in the same vertical column as the
                    // trigger's glyph (label at 20px, glyph right edge at 40px).
                    className={`w-full pl-5 pr-10 py-2 text-[13px] text-left flex items-center justify-between gap-2 transition-colors duration-100 ${
                      isDisabledByLockdown || isDisabledByCapability
                        ? "opacity-40 cursor-not-allowed text-steel"
                        : isSelected
                          ? "text-slate font-medium cursor-pointer"
                          : "text-steel hover:text-slate cursor-pointer"
                    }`}
                  >
                    <span className="truncate">{opt.label}</span>
                    {/* Same glyph + tooltip as the trigger, for EVERY provider —
                        shield for managed Local, cloud for external providers. */}
                    <ProviderDataGlyph
                      value={opt.value}
                      customHost={opt.value === "custom" ? customHost : undefined}
                      ollamaEndpoint={ollamaEndpoint}
                      className="shrink-0"
                    />
                  </button>
                  </HoverTooltip>
                  {isLocalRow && localModelManaged && (
                    // Absolute (same far-right spot it had as a pr-2 sibling) so it
                    // sits in the chevron zone to the RIGHT of the data glyph and no
                    // longer shrinks the button / shifts the local row's glyph.
                    <div className="absolute right-2 top-1/2 -translate-y-1/2 flex items-center">
                      <LocalRowAffordance
                        provider={currentValue}
                        installed={localModelInstalled}
                        modelId={currentLocalModelId}
                        onDelete={onDeleteLocalModel}
                        onDownload={() => {
                          setOpen(false);
                          onDownloadLocal();
                        }}
                      />
                    </div>
                  )}
                </div>
                </Fragment>
              );
            })}
          </motion.div>
          </AnchoredPopover>
        )}
      </AnimatePresence>
    </div>
  );
}
