import { useCallback, useEffect, useRef, useState } from "react";
import { motion, AnimatePresence, MotionConfig } from "framer-motion";
import type { AppConfig } from "../../lib/api/config";
import type { SttArtifactId } from "../../lib/api/models";
import type { AppEvent } from "../../lib/socket/events";
import { getModelsDiskFree, getSttStatus, startDownload } from "../../lib/api/models";
import { saveConfig } from "../../lib/api/config";
import { LowDiskSpaceDialog } from "../LowDiskSpaceDialog";
import { getModelsForLanguage } from "../../lib/languages";
import { planOnboardingSttRouting } from "./onboardingSttRouting";
import { StepIndicator } from "./StepIndicator";
import { StepWelcome } from "./StepWelcome";
import { StepShortcut } from "./StepShortcut";
import { StepTest } from "./StepTest";
import { StepPermissions } from "./StepPermissions";
import { StepEnhance, type FooterSpec } from "./StepEnhance";
import { usePermissionRecovery } from "../../lib/usePermissionRecovery";
import { ContinueButton, GhostLink } from "./buttons";
import { SecondaryButton } from "../SecondaryButton";
import { TitleBar } from "../TitleBar";
import { errorType } from "../../lib/logCapture";
import {
  capabilityDecision,
  capabilityExplanation,
  type DailyUseCapabilityReport,
} from "../../lib/dailyUseCapabilities";

// Onboarding overlay — short-circuits the normal App shell when
// `config.setup_completed === false`.
//
// Locked design decisions (design.md §Design Decisions):
//   - Shared mesh gradient background, same animation as App.tsx
//   - `MotionConfig reducedMotion="user"` wraps the entire overlay
//   - Language has no card chrome; later steps use the shared glass surface
//   - Step transitions: tween 220ms ease [0.2, 0, 0, 1],
//     x: 24*dir → 0 → -24*dir, AnimatePresence mode="wait"
//   - Skip & Finish hidden on step 1, visible from step 2 onward
//   - Step 1 Continue saves a complete Automatic routing state, then fires the
//     idempotent prepare/activate operation when the exact target is not loaded

// Mesh gradient is assembled in JS so the blobs can re-resolve per theme at
// render time. The base canvas and blob tints read from CSS variables set by
// the active [data-theme] block, so swapping themes repaints this without any
// JS changes.
function getMeshGradient() {
  const style = getComputedStyle(document.documentElement);
  const canvas = style.getPropertyValue("--color-canvas").trim() || "#EDF4FA";
  const m1 = style.getPropertyValue("--frost-mesh-1").trim() || "rgba(127,178,222,0.24)";
  const m2 = style.getPropertyValue("--frost-mesh-2").trim() || "rgba(150,195,232,0.18)";
  const m3 = style.getPropertyValue("--frost-mesh-3").trim() || "rgba(127,178,222,0.15)";
  return `radial-gradient(ellipse at 20% 50%, ${m1} 0%, transparent 50%), radial-gradient(ellipse at 80% 20%, ${m2} 0%, transparent 45%), radial-gradient(ellipse at 45% 80%, ${m3} 0%, transparent 50%), ${canvas}`;
}

interface OnboardingOverlayProps {
  config: AppConfig;
  modelLoaded: boolean;
  send: (msg: Record<string, unknown>) => boolean;
  subscribe: (handler: (event: AppEvent) => void) => () => void;
  connected: boolean;
  onConfigChange: (patch: Partial<AppConfig>) => void;
  onFinish: () => void;
  capabilities: DailyUseCapabilityReport | null;
}

type StepId = "language" | "permissions" | "shortcut" | "test" | "enhance";

export function OnboardingOverlay({
  config,
  send,
  subscribe,
  connected,
  onConfigChange,
  onFinish,
  capabilities,
}: OnboardingOverlayProps) {
  const [step, setStep] = useState<StepId>("language");
  const [direction, setDirection] = useState<1 | -1>(1);
  const modelLifecycle = capabilityDecision(capabilities, "model-lifecycle");
  const qwenStt = capabilityDecision(capabilities, "qwen-stt");
  const platform = capabilities?.platform;
  // The optional Enhance step exists only where managed local enhancement is
  // implemented; otherwise Test stays last with Finish.
  const enhanceAvailable = capabilityDecision(capabilities, "managed-local-enhancement").enabled;
  const steps: StepId[] = [
    "language",
    ...(platform === "Darwin" ? (["permissions"] as const) : []),
    "shortcut",
    "test",
    ...(enhanceAvailable ? (["enhance"] as const) : []),
  ];
  const lastStep = steps[steps.length - 1];
  // StepEnhance reports its footer per sub-phase; the overlay renders it.
  const [enhanceFooter, setEnhanceFooter] = useState<FooterSpec | null>(null);
  const stepNumber = steps.indexOf(step) + 1;
  const permissions = usePermissionRecovery(connected && platform === "Darwin" && step === "permissions");
  const finishingRef = useRef(false);
  const advancingRef = useRef(false);
  const [finishing, setFinishing] = useState(false);
  const [finishFailed, setFinishFailed] = useState(false);
  const isLanguageAvailable = useCallback(
    (code: string) =>
      qwenStt.enabled || getModelsForLanguage(code).includes("parakeet"),
    [qwenStt.enabled],
  );
  const selectedLanguageAvailable = isLanguageAvailable(config.language ?? "en");

  // Log step entry on mount (step 1) and whenever step changes.
  useEffect(() => {
    console.log(`[Onboarding] step=${step} entered`);
  }, [step]);
  // hasRecorded gates the Finish button. StepTest flips this true on its
  // first transcription.result event. The flip is the trigger for the
  // single delight moment in the whole flow (opacity 40→100 + scale
  // 0.96→1 on the Finish button) — see ContinueButton's `animated` prop.
  const [hasRecorded, setHasRecorded] = useState(false);
  // Step 3 gates test dictation on this exact target, never on App's generic
  // `modelLoaded` health convenience. Initialize from the current language so
  // a render remains safe even before step 1 commits its routing plan.
  const [testTargetArtifactId, setTestTargetArtifactId] = useState<SttArtifactId>(
    () => planOnboardingSttRouting(config, null).registryId,
  );

  // Low-disk dialog state — mounted when disk pre-check in goNext() step 1
  // finds insufficient space for the recommended speech-model download.
  const [lowDiskOpen, setLowDiskOpen] = useState(false);
  const [lowDiskFreeMb, setLowDiskFreeMb] = useState(0);
  const [lowDiskRequiredMb, setLowDiskRequiredMb] = useState(0);

  const handleFinish = useCallback(async () => {
    if (finishingRef.current) return;
    finishingRef.current = true;
    setFinishing(true);
    setFinishFailed(false);
    try {
      await saveConfig({ setup_completed: true });
      onFinish();
    } catch (err) {
      // Leave the overlay mounted on failure so the user can retry. The
      // log line is captured by the ring buffer for bug-report exports.
      console.error(`[Onboarding] save failed error_type=${errorType(err)}`);
      setFinishFailed(true);
    } finally {
      finishingRef.current = false;
      setFinishing(false);
    }
  }, [onFinish]);

  // RE-TARGET flow for the low-disk dialog "Choose a different folder"
  // action. Nothing has been downloaded yet, so there is nothing to move.
  // We save the new model_storage_dir, dismiss the dialog, re-probe, and
  // start the download if the new drive has room.
  const handleLowDiskChooseFolder = useCallback(async () => {
    setLowDiskOpen(false);
    const { open } = await import("@tauri-apps/plugin-dialog");
    const selected = await open({
      directory: true,
      multiple: false,
      title: "Choose where to store models",
    });
    if (!selected || typeof selected !== "string") return; // cancel = silent no-op
    try {
      await saveConfig({ model_storage_dir: selected });
      onConfigChange({ model_storage_dir: selected });
    } catch (err) {
      console.warn(`[Onboarding] model storage save failed error_type=${errorType(err)}`);
      return;
    }
    // Re-probe the new location. Fail open so a detection error doesn't
    // strand the user — proceed to startDownload.
    try {
      const { free_mb, required_mb, sufficient } = await getModelsDiskFree(selected);
      if (free_mb >= 0 && !sufficient) {
        setLowDiskFreeMb(free_mb);
        setLowDiskRequiredMb(required_mb);
        setLowDiskOpen(true);
        return;
      }
    } catch (err) {
      console.warn(`[Onboarding] disk recheck failed error_type=${errorType(err)} action=continue`);
    }
    startDownload().catch((err) =>
      console.error(`[Onboarding] startDownload (after retarget) failed error_type=${errorType(err)}`)
    );
  }, [onConfigChange]);

  const handleLowDiskDismiss = useCallback(() => {
    // Just close — re-clicking Continue on step 1 will re-probe.
    setLowDiskOpen(false);
  }, []);

  const goNext = useCallback(async () => {
    if (advancingRef.current || finishingRef.current || (step === "permissions" && !permissions.ready)) return;
    advancingRef.current = true;
    try {
      if (step === "language") {
        if (!modelLifecycle.enabled || !selectedLanguageAvailable) return;
        // Onboarding stays language-only and explicitly returns routing to
        // Automatic. An interrupted first run can leave a different recognizer
        // resident, so the exact loaded artifact from /api/stt/status decides
        // whether the unified idempotent prepare/activate operation is needed.
        //
        // patchConfig in App.tsx debounces PUT /api/config by 500ms, so we
        // ALSO call saveConfig synchronously here to guarantee the backend
        // has seen the new selection before startDownload() races ahead.
        let status = null;
        try {
          status = await getSttStatus();
        } catch (err) {
          // Fail toward prepare/activate. Existing complete files make the
          // operation cheap, while skipping could leave the wrong recognizer.
          console.warn(`[Onboarding] stt status failed error_type=${errorType(err)} action=prepare`);
        }

        const routing = planOnboardingSttRouting(config, status);
        setTestTargetArtifactId(routing.registryId);
        try {
          await saveConfig(routing.patch);
          onConfigChange(routing.patch);
        } catch (err) {
          // Do not start a worker against routing the backend did not accept.
          // Leaving step 1 mounted preserves the existing retry behavior.
          console.error(`[Onboarding] stt selection save failed error_type=${errorType(err)}`);
          return;
        }

        if (routing.needsActivation) {
          // Disk pre-check: probe free space on the models drive before
          if (!routing.targetDownloaded && platform !== "Darwin") {
            try {
              const currentStorageDir = config.model_storage_dir || config.data_dir || undefined;
              const { free_mb, required_mb, sufficient } = await getModelsDiskFree(currentStorageDir);
              if (free_mb >= 0 && !sufficient) {
                setLowDiskFreeMb(free_mb);
                setLowDiskRequiredMb(required_mb);
                setLowDiskOpen(true);
                return; // do NOT advance the step or start the download
              }
            } catch (err) {
              console.warn(`[Onboarding] disk check failed error_type=${errorType(err)} action=continue`);
            }
          }
          // Fire-and-forget — the backend broadcasts progress over WS, and
          // StepTest (Task 3) reads that stream.
          startDownload().catch((err) =>
            console.error(`[Onboarding] startDownload failed error_type=${errorType(err)}`)
          );
        }
      }
      if (step === "enhance") {
        // Enter on Enhance acts as the step's primary footer action.
        if (enhanceFooter && !enhanceFooter.primary.disabled) enhanceFooter.primary.onClick();
        return;
      }
      if (step === "test" && !hasRecorded) return;
      if (step !== lastStep) {
        console.log(`[Onboarding] step=${step} completed`);
        setDirection(1);
        setStep(steps[stepNumber]);
      } else {
        // Test → Finish requires the user to have recorded at least once.
        // Skip & finish bypasses this gate (it calls handleFinish directly).
        if (!hasRecorded) return;
        console.log(`[Onboarding] step=${step} completed`);
        await handleFinish();
      }
    } finally {
      advancingRef.current = false;
    }
  }, [step, stepNumber, lastStep, enhanceFooter, permissions.ready, hasRecorded, handleFinish, config, onConfigChange, platform, modelLifecycle.enabled, selectedLanguageAvailable]);

  // Suppress text injection for the lifetime of Test so transcriptions
  // land in the test input via React state rather than being typed into
  // the focused OS field. Task 1 wired the backend side; this is the
  // frontend half. (Task 2 sets it up here ahead of Task 3 swapping in the
  // real StepTest.)
  // Suppression spans Test and Enhance as one window (no release between
  // them), so try-it dictations never type into the focused app.
  const suppressInjection = step === "test" || step === "enhance";
  useEffect(() => {
    if (!suppressInjection) return;
    send({ type: "inject.suppress", suppressed: true });
    return () => {
      send({ type: "inject.suppress", suppressed: false });
    };
  }, [suppressInjection, send]);

  // Global Enter advances to the next step. Ignored when focus is in a
  // text-editing field so the user can type into the Test textarea
  // without accidentally finishing the flow.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || e.shiftKey || e.ctrlKey || e.metaKey) return;
      const target = e.target as HTMLElement | null;
      if (
        target instanceof HTMLElement &&
        (target.tagName === "TEXTAREA" ||
          target.tagName === "INPUT" ||
          target.isContentEditable ||
          target.closest("button, a, select, [role=button], [role=combobox]"))
      ) {
        return;
      }
      e.preventDefault();
      goNext();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [goNext]);

  return (
    <MotionConfig reducedMotion="user">
      <div
        className="fixed inset-0 w-full h-screen overflow-hidden flex flex-col"
        style={{
          background: getMeshGradient(),
          backgroundSize: "200% 200%",
          backgroundPosition: "30% 30%",
        }}
      >
        {/* Window chrome — minimize/close. Drag region handled inside. */}
        <TitleBar />

        {/* Step indicator */}
        <div className="pt-10 pb-2 flex justify-center shrink-0">
          <StepIndicator current={stepNumber} total={steps.length} />
        </div>

        {/* Step content */}
        <div className="flex-1 min-h-0 overflow-y-auto flex flex-col px-8 py-6 relative">
          <AnimatePresence mode="wait" custom={direction}>
            <motion.div
              key={step}
              custom={direction}
              initial={{ opacity: 0, x: 24 * direction }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -24 * direction }}
              transition={{ duration: 0.22, ease: [0.2, 0, 0, 1] }}
              className="w-full max-w-[600px] m-auto shrink-0"
            >
              {step === "language" && (
                <>
                  <StepWelcome
                    language={config.language}
                    onLanguageChange={(language) => onConfigChange({ language })}
                    isLanguageAvailable={isLanguageAvailable}
                    unavailableReason={capabilityExplanation(qwenStt)}
                  />
                  {(!modelLifecycle.enabled || !selectedLanguageAvailable) && (
                    <div
                      role="status"
                      className="mt-4 mx-auto max-w-md rounded-[var(--radius-row)] px-4 py-3 text-[12px] text-steel text-center"
                      style={{
                        background: "var(--surface-control)",
                        border: "1px solid var(--border-standard)",
                      }}
                    >
                      {capabilityExplanation(
                        !modelLifecycle.enabled ? modelLifecycle : qwenStt,
                      )}
                    </div>
                  )}
                </>
              )}
              {step === "permissions" && <StepPermissions recovery={permissions} connected={connected} />}
              {step === "shortcut" && (
                <StepShortcut
                  config={config}
                  onConfigChange={onConfigChange}
                  send={send}
                  subscribe={subscribe}
                  connected={connected}
                />
              )}
              {step === "test" && (
                <StepTest
                  config={config}
                  targetArtifactId={testTargetArtifactId}
                  subscribe={subscribe}
                  send={send}
                  onConfigChange={onConfigChange}
                  onHasRecordedChange={setHasRecorded}
                />
              )}
              {step === "enhance" && (
                <StepEnhance
                  config={config}
                  subscribe={subscribe}
                  onConfigChange={onConfigChange}
                  onFinish={handleFinish}
                  finishing={finishing}
                  onFooterChange={setEnhanceFooter}
                />
              )}
            </motion.div>
          </AnimatePresence>
        </div>

        {/* Footer */}
        <footer className="px-8 pb-7 pt-2 shrink-0 flex flex-wrap items-center justify-end gap-x-5 gap-y-2">
          {(step === "permissions" || finishFailed) && <div className="mr-auto text-[11px] text-steel" role={finishFailed ? "alert" : undefined}>
            {finishFailed ? <>Couldn't finish setup. Try again. <button onClick={handleFinish} disabled={finishing} className="ml-2 underline">Try again</button></> : "Finish setup on Home before dictating."}
          </div>}
          {step === "enhance" ? (
            enhanceFooter && (
              <>
                {enhanceFooter.ghost && (
                  <GhostLink onClick={enhanceFooter.ghost.onClick} disabled={enhanceFooter.ghost.disabled}>
                    {finishing ? "Saving..." : enhanceFooter.ghost.label}
                  </GhostLink>
                )}
                {enhanceFooter.secondary && (
                  <SecondaryButton
                    onClick={enhanceFooter.secondary.onClick}
                    disabled={enhanceFooter.secondary.disabled}
                    className="px-[22px] py-2.5 rounded-full text-[13px] font-medium text-slate"
                  >
                    {enhanceFooter.secondary.label}
                  </SecondaryButton>
                )}
                <ContinueButton
                  label={enhanceFooter.primary.label}
                  onClick={enhanceFooter.primary.onClick}
                  disabled={enhanceFooter.primary.disabled}
                />
              </>
            )
          ) : (
          <>
          {step !== "language" && (
            <GhostLink onClick={handleFinish} disabled={finishing}>{finishing ? "Saving..." : "Skip & finish"}</GhostLink>
          )}
          <ContinueButton
            label={step === lastStep ? "Finish" : "Continue"}
            onClick={goNext}
            // On Test the Finish button is disabled (opacity-40) until the
            // user records once. The flip from disabled→enabled is the only
            // spring/scale moment in the entire onboarding flow.
            disabled={
              finishing || (step === "permissions" && !permissions.ready) ||
              (step === "language" && (!modelLifecycle.enabled || !selectedLanguageAvailable)) ||
              (step === "test" && !hasRecorded)
            }
            animated={step === "test"}
          />
          </>
          )}
        </footer>

        {/* Low-disk space gate (step 1 only). Speech has no cloud fallback so
            showCloudOption is false — the cloud button and caption are
            suppressed. Dismissing re-enables the Continue button so the user
            can retry after freeing space. */}
        <LowDiskSpaceDialog
          open={lowDiskOpen}
          freeDiskGb={Math.round((lowDiskFreeMb / 1024) * 10) / 10}
          requiredDiskGb={Math.ceil(lowDiskRequiredMb / 1024)}
          onChooseFolder={handleLowDiskChooseFolder}
          onSwitchToCloud={() => {}}
          onDismiss={handleLowDiskDismiss}
          showCloudOption={false}
        />
      </div>
    </MotionConfig>
  );
}
