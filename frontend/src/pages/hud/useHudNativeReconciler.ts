import { useLayoutEffect, useRef, type Dispatch, type SetStateAction } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { emit } from "@tauri-apps/api/event";
import { LogicalSize, LogicalPosition } from "@tauri-apps/api/dpi";
import { applyWindowLifecycle } from "../../lib/windowLifecycle";
import { HUD_MAIN_VISIBILITY_EVENT } from "../../lib/recordingControl";
import { HIDE_ANIMATION_MS, type derivePresentation } from "./presentation";
import { hudSurfaceForMode } from "./surfaceState";
import { NATIVE_MUTATION_STEP_TIMEOUT_MS, createNativeMutationRunner, type NativeMutationRunner } from "./nativeMutationChain";
import { type ConsoleSizeRequest, type HudGeometryMode, type HudWindowRegion, type MeasuredClaudeCard, PREVIEW_CARD_H, PREVIEW_CARD_W, isHudControlFamilyMode, resolveHudGeometry, hudWindowRegionForMode } from "./geometry";

interface AppliedWindowGeometry {
  width: number;
  height: number;
  x: number | null;
  y: number | null;
  visible: boolean;
}

type HudWindowPresentationOwner =
  | "clean_tauri"
  | "native_borderless"
  | "native_region";

interface NativeBorderlessWindowState {
  visible: boolean;
  style: string;
  exstyle: string;
  nonclient_policy: string;
}

interface AppliedHudWindowRegion extends HudWindowRegion {
  restricted: boolean;
}

function hudWindowRegionsEqual(
  left: AppliedHudWindowRegion | undefined,
  right: AppliedHudWindowRegion,
): boolean {
  if (left === undefined) return false;
  return (
    left.restricted === right.restricted &&
    left.x === right.x &&
    left.y === right.y &&
    left.width === right.width &&
    left.height === right.height &&
    left.cornerRadius === right.cornerRadius
  );
}

interface HudNativeReconcilerInputs {
  presentation: Pick<ReturnType<typeof derivePresentation>, "mode">;
  managedConsoleDismissedToHidden: boolean;
  presentationGeometryMode: HudGeometryMode;
  cleanGeometryPresentation: boolean;
  permanentControlPresentation: boolean;
  parkedRecordingControl: boolean;
  previewExpanded: boolean;
  previewNaturalTextHeight: number;
  measuredCardSize: MeasuredClaudeCard | null;
  responseExpanded: boolean;
  /** A live console resize, or null to use the size remembered per display. */
  consoleSizeRequest: ConsoleSizeRequest | null;
  dedicatedRecordingControlWindow: boolean;
  dedicatedArmingParkingEnabled: boolean;
  setExpandedPreviewCardSize: Dispatch<SetStateAction<{ width: number; height: number }>>;
  setAppliedGeometryMode: Dispatch<SetStateAction<HudGeometryMode | null>>;
}

/** One mutable native owner. Acknowledgements project applied state back to paint;
 * the coordinator's presentation, focus and drag authorities remain independent. */
export function useHudNativeReconciler({
  presentation,
  managedConsoleDismissedToHidden,
  presentationGeometryMode,
  cleanGeometryPresentation,
  permanentControlPresentation,
  parkedRecordingControl,
  previewExpanded,
  previewNaturalTextHeight,
  measuredCardSize,
  responseExpanded,
  consoleSizeRequest,
  dedicatedRecordingControlWindow,
  dedicatedArmingParkingEnabled,
  setExpandedPreviewCardSize,
  setAppliedGeometryMode,
}: HudNativeReconcilerInputs): void {
  const appliedGeometryModeRef = useRef<HudGeometryMode | null>(null);
  const windowGenerationRef = useRef(0);
  const appliedWindowGeometryRef = useRef<AppliedWindowGeometry | null>(null);
  const appliedWindowRegionRef = useRef<AppliedHudWindowRegion | undefined>(
    undefined,
  );
  const appliedWindowPresentationOwnerRef =
    useRef<HudWindowPresentationOwner | null>(null);
  // Content-free label of the outstanding step, for the timeout warning.
  const outstandingStepLabelRef = useRef("");
  // One outstanding native step at most; newer requests coalesce. A timed-out
  // step puts the runner in recovery: nothing else is issued until it settles,
  // then `recover` forces and records a known hidden state and the newest
  // desired state is applied from it, so a late native completion is never
  // trusted as current bookkeeping.
  const mutationRunnerRef = useRef<NativeMutationRunner | null>(null);
  if (mutationRunnerRef.current === null) {
    mutationRunnerRef.current = createNativeMutationRunner({
      timeoutMs: NATIVE_MUTATION_STEP_TIMEOUT_MS,
      onTimeout: () => {
        console.warn(
          `[HUD] native mutation timed out ${outstandingStepLabelRef.current} timeout_ms=${NATIVE_MUTATION_STEP_TIMEOUT_MS} recovering=true`,
        );
      },
      // After an uncertain completion nothing the reconciler believes about
      // the window can be trusted. Force a known state with idempotent hides
      // through every owner (borderless, native region, then Tauri so tao's
      // cache agrees), record exactly that, and let the newest desired step
      // reapply from it.
      recover: async () => {
        console.warn(
          `[HUD] native mutation recovery after ${outstandingStepLabelRef.current}`,
        );
        appliedWindowGeometryRef.current = null;
        appliedWindowRegionRef.current = undefined;
        appliedWindowPresentationOwnerRef.current = null;
        appliedGeometryModeRef.current = null;
        const nativeHide = async (command: string, args?: Record<string, unknown>) => {
          try {
            await invoke(command, args);
          } catch (error) {
            // Browser development and jsdom have no native bridge.
            if (isTauri()) throw error;
          }
        };
        await nativeHide("set_hud_window_borderless_visibility", {
          show: false,
          hide: true,
        });
        await nativeHide("hide_and_clear_hud_window_region");
        if (!(await applyWindowLifecycle("hud", "hide"))) {
          await getCurrentWindow().hide();
        }
        appliedWindowPresentationOwnerRef.current = "clean_tauri";
        appliedWindowRegionRef.current = {
          restricted: false,
          x: 0,
          y: 0,
          width: 0,
          height: 0,
          cornerRadius: 0,
        };
        appliedWindowGeometryRef.current = null;
      },
    });
  }
  // The only owner of HUD presentation mutations. Mutations are serialized so
  // an already-started operation finishes before a newer generation applies
  // its final geometry. Generation checks cancel stale fade tails and prevent
  // old position/show/hide work from continuing after priority changes.
  // Primitives, so an identical request object does not re-run the effect.
  const consoleRequestKey = consoleSizeRequest?.displayKey ?? null;
  const consoleRequestW = consoleSizeRequest?.width ?? null;
  const consoleRequestH = consoleSizeRequest?.height ?? null;
  useLayoutEffect(() => {
    const generation = ++windowGenerationRef.current;
    const consoleRequest: ConsoleSizeRequest | null =
      consoleRequestKey === null ||
      consoleRequestW === null ||
      consoleRequestH === null
        ? null
        : {
            displayKey: consoleRequestKey,
            width: consoleRequestW,
            height: consoleRequestH,
          };
    let hideTimeout: number | null = null;
    const targetMode = presentation.mode;

    const reconcile = () => {
      mutationRunnerRef.current!.request(async (control) => {
          // A timed-out step stops issuing native work once its hung invoke
          // settles. Any bookkeeping it records is discarded by recovery.
          const isCurrent = () =>
            !control.abandoned && windowGenerationRef.current === generation;
          if (!isCurrent()) return;
          outstandingStepLabelRef.current = `gen=${generation} target=${targetMode}`;
          let windowMutationSucceeded = true;
          try {
            if (
              dedicatedRecordingControlWindow &&
              targetMode !== "hidden" &&
              !(
                appliedWindowGeometryRef.current?.visible === true &&
                appliedGeometryModeRef.current === "arming"
              )
            ) {
              try {
                await invoke("hide_recording_control_window");
                await emit(HUD_MAIN_VISIBILITY_EVENT, true).catch((error) => {
                  console.warn(
                    `[HUD] main visibility event failed visible=true reason=${
                      error instanceof Error ? error.message : String(error)
                    }`,
                  );
                });
              } catch (error) {
                if (isTauri()) throw error;
              }
              if (!isCurrent()) return;
            }
            const previouslyApplied = appliedWindowGeometryRef.current;
            const geometry =
              targetMode === "hidden" &&
              !parkedRecordingControl &&
              previouslyApplied !== null
                ? {
                    size: new LogicalSize(
                      previouslyApplied.width,
                      previouslyApplied.height,
                    ),
                    position:
                      previouslyApplied.x !== null &&
                      previouslyApplied.y !== null
                        ? new LogicalPosition(
                            previouslyApplied.x,
                            previouslyApplied.y,
                          )
                        : null,
                  }
                : await resolveHudGeometry(
                    presentationGeometryMode,
                    targetMode === "preview" && previewExpanded,
                    previewNaturalTextHeight,
                    measuredCardSize,
                    consoleRequest,
                  );
            if (!isCurrent()) return;
            if (geometry.previewCardSize) {
              setExpandedPreviewCardSize((current) =>
                current.width === geometry.previewCardSize?.width &&
                current.height === geometry.previewCardSize?.height
                  ? current
                  : geometry.previewCardSize!,
              );
            }
            const hudWindow = getCurrentWindow();
            const nextApplied: AppliedWindowGeometry = {
              width: geometry.size.width,
              height: geometry.size.height,
              x: geometry.position?.x ?? null,
              y: geometry.position?.y ?? null,
              visible: targetMode !== "hidden",
            };
            const applied = appliedWindowGeometryRef.current;
            const geometryChanged =
              applied === null ||
              applied.width !== nextApplied.width ||
              applied.height !== nextApplied.height ||
              applied.x !== nextApplied.x ||
              applied.y !== nextApplied.y;
            const visibilityChanged =
              applied === null || applied.visible !== nextApplied.visible;
            const appliedPresentationOwner =
              appliedWindowPresentationOwnerRef.current;
            const targetUsesCleanGeometry =
              cleanGeometryPresentation ||
              (targetMode === "hidden" &&
                !parkedRecordingControl &&
                appliedPresentationOwner !== "native_region");
            const targetPresentationOwner: HudWindowPresentationOwner =
              permanentControlPresentation ||
              (targetMode === "hidden" &&
                appliedPresentationOwner === "native_borderless")
                ? "native_borderless"
                : targetUsesCleanGeometry
                  ? "clean_tauri"
                  : "native_region";
            const unrestrictedRegion: AppliedHudWindowRegion = {
              restricted: false,
              x: 0,
              y: 0,
              width: geometry.size.width,
              height: geometry.size.height,
              cornerRadius: 0,
            };

            // Clean recording and Preview deliberately skip region construction.
            // A stale Claude measurement still clips live shaped UI, so only the
            // native family reuses the region already applied for this mode.
            let targetRegion = unrestrictedRegion;
            if (targetPresentationOwner === "native_region") {
              const appliedRegion = appliedWindowRegionRef.current;
              const previousRegion =
                appliedRegion !== undefined &&
                appliedRegion.restricted &&
                appliedGeometryModeRef.current === presentationGeometryMode
                  ? {
                      x: appliedRegion.x,
                      y: appliedRegion.y,
                      width: appliedRegion.width,
                      height: appliedRegion.height,
                      cornerRadius: appliedRegion.cornerRadius,
                    }
                  : null;
              const compactRegion =
                targetMode === "hidden" && !parkedRecordingControl
                  ? null
                  : hudWindowRegionForMode(
                      presentationGeometryMode,
                      measuredCardSize,
                      previousRegion,
                      presentationGeometryMode === "preview"
                        ? {
                            windowW: geometry.size.width,
                            windowH: geometry.size.height,
                            cardW:
                              geometry.previewCardSize?.width ??
                              PREVIEW_CARD_W,
                            cardH:
                              geometry.previewCardSize?.height ??
                              PREVIEW_CARD_H,
                          }
                        : null,
                    );
              targetRegion =
                compactRegion === null
                  ? unrestrictedRegion
                  : { restricted: true, ...compactRegion };
            }
            const regionChanged =
              appliedPresentationOwner !== targetPresentationOwner ||
              !hudWindowRegionsEqual(
                appliedWindowRegionRef.current,
                targetRegion,
              );
            const becomingVisible =
              nextApplied.visible && applied?.visible !== true;
            let nativeToCleanTransitionHidden = false;
            let borderlessOpeningHidden = false;
            let cleanRecordingOpeningHidden = false;
            // The single most useful line when the HUD goes invisible: it says
            // what the reconciler BELIEVES the window's visibility is. Both
            // wedges so far looked identical from outside (card painting, no
            // window) and differed only here.
            if (visibilityChanged || geometryChanged || regionChanged) {
              console.log(
                `[HUD] reconcile gen=${generation} target=${targetMode} mode=${presentationGeometryMode} owner=${targetPresentationOwner} was_visible=${
                  applied?.visible ?? "none"
                } next_visible=${nextApplied.visible} becoming_visible=${becomingVisible} geometry_changed=${geometryChanged} region_changed=${regionChanged}`,
              );
            }

            const applyTargetRegion = async (
              show = false,
              hide = false,
            ) => {
              // The reconciler used to log nothing at all, which is why two
              // rounds of invisible-window reports had to be diagnosed from
              // source rather than from a user's log bundle. Content-free:
              // geometry numbers only.
              console.log(
                `[HUD] region apply gen=${generation} mode=${presentationGeometryMode} show=${show} hide=${hide} restricted=${targetRegion.restricted} x=${targetRegion.x} y=${targetRegion.y} w=${targetRegion.width} h=${targetRegion.height} r=${targetRegion.cornerRadius}`,
              );
              try {
                await invoke("set_hud_window_region", {
                  show,
                  hide,
                  restricted: targetRegion.restricted,
                  x: targetRegion.x,
                  y: targetRegion.y,
                  width: targetRegion.width,
                  height: targetRegion.height,
                  cornerRadius: targetRegion.cornerRadius,
                });
              } catch (error) {
                // Browser development and jsdom have no native region bridge.
                // A real Tauri failure must hold the presentation gate closed.
                if (isTauri()) throw error;
              }
              // Record what the native window now IS, even when a newer
              // generation has already superseded this one. A stale generation
              // must stop further work; it must never drop the bookkeeping for
              // a mutation that already landed, or the next reconcile compares
              // against a state the window left behind.
              appliedWindowRegionRef.current = targetRegion;
              appliedWindowPresentationOwnerRef.current = "native_region";
              if (show) {
                // The bounds for this mode are already applied by the time a
                // show runs, so the full target is the truth.
                appliedWindowGeometryRef.current = {
                  ...nextApplied,
                  visible: true,
                };
              } else if (hide) {
                appliedWindowGeometryRef.current = {
                  ...(appliedWindowGeometryRef.current ?? nextApplied),
                  visible: false,
                };
              }
              if (!isCurrent()) return;
            };

            const recordCleanVisibility = (visible: boolean) => {
              appliedWindowPresentationOwnerRef.current = "clean_tauri";
              appliedWindowRegionRef.current = unrestrictedRegion;
              appliedWindowGeometryRef.current = {
                ...(appliedWindowGeometryRef.current ?? nextApplied),
                visible,
              };
            };

            const applyCleanVisibility = async (
              action: "show" | "hide",
            ) => {
              const handled = await applyWindowLifecycle("hud", action);
              if (!handled && action === "show") {
                await hudWindow.show();
                recordCleanVisibility(true);
              } else if (!handled) {
                await hudWindow.hide();
                recordCleanVisibility(false);
              } else {
                recordCleanVisibility(action === "show");
              }
              // Visibility already landed. It is recorded before the caller's
              // generation check so the next reconcile still owes the right
              // show or hide.
            };

            const recordBorderlessVisibility = (visible: boolean) => {
              appliedWindowPresentationOwnerRef.current =
                "native_borderless";
              appliedWindowRegionRef.current = unrestrictedRegion;
              appliedWindowGeometryRef.current = {
                ...(appliedWindowGeometryRef.current ?? nextApplied),
                visible,
              };
            };

            const applyBorderlessVisibility = async (
              action: "show" | "hide",
            ) => {
              console.log(
                `[HUD] borderless visibility gen=${generation} action=${action} mode=${presentationGeometryMode}`,
              );
              try {
                const nativeState =
                  await invoke<NativeBorderlessWindowState>(
                    "set_hud_window_borderless_visibility",
                    {
                      show: action === "show",
                      hide: action === "hide",
                    },
                  );
                if (nativeState) {
                  console.log(
                    `[HUD] borderless visibility applied gen=${generation} action=${action} mode=${presentationGeometryMode} visible=${nativeState.visible} style=${nativeState.style} exstyle=${nativeState.exstyle} nonclient_policy=${nativeState.nonclient_policy}`,
                  );
                }
              } catch (error) {
                // Browser development and jsdom have no native visibility
                // bridge. A real Tauri failure must hold the paint gate closed.
                if (isTauri()) {
                  console.error(
                    `[HUD] borderless visibility failed gen=${generation} action=${action} mode=${presentationGeometryMode} reason=${
                      error instanceof Error ? error.message : String(error)
                    }`,
                  );
                  throw error;
                }
              }
              recordBorderlessVisibility(action === "show");
            };

            // A dedicated-control handback parks the physically hidden main
            // HUD at the next arming rectangle. Keep the closing surface at
            // its visible bounds through the hide, then prepare the hidden
            // WebView viewport before the sibling is allowed to return. This
            // also initializes a cold hidden HUD at 132x52, so the next start
            // is show-only and cannot expose a one-frame child-layout catchup.
            if (
              dedicatedArmingParkingEnabled &&
              targetMode === "hidden"
            ) {
              if (appliedPresentationOwner === "native_region") {
                try {
                  await invoke("hide_and_clear_hud_window_region");
                } catch (error) {
                  if (isTauri()) throw error;
                }
              }
              await applyCleanVisibility("hide");
              if (!isCurrent()) return;

              const parkedGeometry = await resolveHudGeometry(
                "arming",
                false,
                0,
              );
              if (!isCurrent()) return;
              const parkedPosition = parkedGeometry.position;
              const hiddenGeometry = appliedWindowGeometryRef.current;
              const parkedGeometryChanged =
                hiddenGeometry === null ||
                hiddenGeometry.width !== parkedGeometry.size.width ||
                hiddenGeometry.height !== parkedGeometry.size.height ||
                hiddenGeometry.x !== (parkedPosition?.x ?? null) ||
                hiddenGeometry.y !== (parkedPosition?.y ?? null);
              if (parkedGeometryChanged) {
                await hudWindow.setSize(parkedGeometry.size);
                if (!isCurrent()) return;
                if (parkedPosition !== null) {
                  await hudWindow.setPosition(parkedPosition);
                  if (!isCurrent()) return;
                }
              }

              appliedWindowGeometryRef.current = {
                width: parkedGeometry.size.width,
                height: parkedGeometry.size.height,
                x: parkedPosition?.x ?? null,
                y: parkedPosition?.y ?? null,
                visible: false,
              };
              appliedWindowRegionRef.current = {
                restricted: false,
                x: 0,
                y: 0,
                width: parkedGeometry.size.width,
                height: parkedGeometry.size.height,
                cornerRadius: 0,
              };
              appliedWindowPresentationOwnerRef.current = "clean_tauri";
              appliedGeometryModeRef.current = null;
              setAppliedGeometryMode(null);
              await emit(HUD_MAIN_VISIBILITY_EVENT, false).catch((error) => {
                console.warn(
                  `[HUD] main visibility event failed visible=false reason=${
                    error instanceof Error ? error.message : String(error)
                  }`,
                );
              });
              return;
            }

            // Both native owners bypass tao's cached VISIBLE bit. Before Tauri
            // takes visibility back, hide physically through the current owner,
            // clear an HRGN when necessary, then enter the ordinary Tauri
            // hide/geometry/show sequence. Its final show reclaims the visible
            // state for the clean owner even when the preceding hide was an
            // empty cache diff.
            if (
              targetPresentationOwner === "clean_tauri" &&
              (appliedPresentationOwner === "native_region" ||
                (appliedPresentationOwner === "native_borderless" &&
                  applied?.visible === true))
            ) {
              console.log(
                `[HUD] presentation handoff gen=${generation} mode=${presentationGeometryMode} from=${appliedPresentationOwner} to=clean_tauri`,
              );
              if (appliedPresentationOwner === "native_region") {
                try {
                  await invoke("hide_and_clear_hud_window_region");
                } catch (error) {
                  // Browser development and jsdom have no native handoff bridge.
                  // A real failure must keep the clean presentation hidden.
                  if (isTauri()) throw error;
                }
              } else {
                await applyBorderlessVisibility("hide");
              }
              if (!(await applyWindowLifecycle("hud", "hide"))) {
                await hudWindow.hide();
              }
              recordCleanVisibility(false);
              nativeToCleanTransitionHidden = true;
              if (!isCurrent()) return;
            }

            // Returning to the permanent family crosses in the other direction.
            // Hide the currently visible family before its DOM is replaced or
            // its native bounds are reduced. A native-region source must also
            // drop its HRGN; a clean Tauri source can use its normal hide.
            if (
              targetPresentationOwner === "native_borderless" &&
              applied?.visible === true &&
              appliedPresentationOwner !== "native_borderless"
            ) {
              console.log(
                `[HUD] presentation handoff gen=${generation} mode=${presentationGeometryMode} from=${appliedPresentationOwner ?? "none"} to=native_borderless`,
              );
              if (appliedPresentationOwner === "native_region") {
                try {
                  await invoke("hide_and_clear_hud_window_region");
                } catch (error) {
                  if (isTauri()) throw error;
                }
                appliedWindowGeometryRef.current = {
                  ...(appliedWindowGeometryRef.current ?? nextApplied),
                  visible: false,
                };
                appliedWindowRegionRef.current = unrestrictedRegion;
              } else {
                await applyCleanVisibility("hide");
              }
              borderlessOpeningHidden = true;
              if (!isCurrent()) return;
            }

            // A lifecycle can briefly present the ordinary recording pill
            // before the correlated Preview state arrives. A visible permanent
            // control also still owns a 132x52 backing when a global shortcut
            // commits either clean recording surface. Hide those specific
            // cross-family openings first, apply final size and position while
            // hidden, then show once below. Steady-state recording and
            // Preview-to-Preview geometry changes stay visible.
            const appliedGeometryMode = appliedGeometryModeRef.current;

            // A visible surface handing over to another window family (a
            // dictation pill or transient over a reply card, a question or
            // permission over it, a card returning after them) hides first,
            // takes its geometry while hidden, then shows once below. The
            // window is never resized while visible across families. The
            // card and the console keep their one atomic move (the incoming
            // card stays unpainted until it lands), and the permanent-control
            // owner keeps its own handoffs above.
            const appliedSurface =
              appliedGeometryMode === null ? null : hudSurfaceForMode(appliedGeometryMode);
            const targetSurface = hudSurfaceForMode(presentationGeometryMode);
            const replyWindowFamily = (surface: string | null) =>
              surface === "card" || surface === "console";
            let familyHandoffHidden = false;
            if (
              targetMode !== "hidden" &&
              applied?.visible === true &&
              geometryChanged &&
              appliedSurface !== null &&
              appliedSurface !== targetSurface &&
              (replyWindowFamily(appliedSurface) || replyWindowFamily(targetSurface)) &&
              !(replyWindowFamily(appliedSurface) && replyWindowFamily(targetSurface)) &&
              targetPresentationOwner !== "native_borderless" &&
              appliedPresentationOwner !== "native_borderless" &&
              !nativeToCleanTransitionHidden
            ) {
              console.log(
                `[HUD] family handoff gen=${generation} from=${appliedGeometryMode} to=${presentationGeometryMode} owner=${appliedPresentationOwner ?? "none"}->${targetPresentationOwner}`,
              );
              if (appliedPresentationOwner === "native_region") {
                await applyTargetRegion(false, true);
              } else {
                await applyCleanVisibility("hide");
              }
              familyHandoffHidden = true;
              if (!isCurrent()) return;
            }

            const openingCleanRecordingFromPermanent =
              appliedGeometryMode !== null &&
              isHudControlFamilyMode(appliedGeometryMode) &&
              (presentationGeometryMode === "pill" ||
                presentationGeometryMode === "preview");
            const openingPreviewFromCompactRecording =
              targetMode === "preview" &&
              appliedGeometryMode !== "preview";
            if (
              targetPresentationOwner === "clean_tauri" &&
              targetMode !== "hidden" &&
              applied?.visible === true &&
              (openingCleanRecordingFromPermanent ||
                openingPreviewFromCompactRecording) &&
              !nativeToCleanTransitionHidden &&
              !familyHandoffHidden
            ) {
              console.log(
                `[HUD] clean recording prepare gen=${generation} from=${appliedGeometryMode ?? "none"} to=${presentationGeometryMode}`,
              );
              await applyCleanVisibility("hide");
              cleanRecordingOpeningHidden = true;
              if (!isCurrent()) return;
            }

            // Measured Claude/Agent Speak surfaces retain their native HRGN
            // owner. The fixed permanent family is intentionally excluded: it
            // uses clean geometry with a separate unshaped borderless owner.
            if (
              targetPresentationOwner === "native_region" &&
              regionChanged &&
              !becomingVisible &&
              !parkedRecordingControl &&
              targetMode !== "hidden"
            ) {
              await applyTargetRegion();
            }

            if (geometryChanged) {
              // Two separate IPC calls leave a presentable frame where the
              // window sits at the new origin while still carrying the old
              // size, which is what made the bottom-anchored seed jump before
              // expanding. When a mutation changes both, hand the whole
              // rectangle to one native SetWindowPos instead. The centered
              // console moves and resizes together on every resize step, so
              // it takes the same single command in the clean family.
              const targetPosition = geometry.position;
              let appliedAtomically = false;
              if (
                (targetPresentationOwner === "native_region" ||
                  (targetPresentationOwner === "clean_tauri" &&
                    (presentationGeometryMode === "response" ||
                      presentationGeometryMode === "panel" ||
                      presentationGeometryMode === "summary"))) &&
                targetPosition !== null
              ) {
                try {
                  await invoke("set_hud_bounds", {
                    x: targetPosition.x,
                    y: targetPosition.y,
                    width: geometry.size.width,
                    height: geometry.size.height,
                  });
                  appliedAtomically = true;
                } catch {
                  // Browser development and jsdom have no Tauri command bridge.
                  // Fall back to the sequenced pair below.
                  appliedAtomically = false;
                }
                if (!isCurrent()) return;
              }

              if (!appliedAtomically) {
                // Clean recording/Preview follows the known-good #315 contract:
                // resize once, move once, then refresh the visible surface.
                await hudWindow.setSize(geometry.size);
                if (!isCurrent()) return;
                if (targetPosition !== null) {
                  await hudWindow.setPosition(targetPosition);
                  if (!isCurrent()) return;
                }
              }
            }
            if (targetPresentationOwner === "clean_tauri") {
              if (targetMode === "hidden") {
                if (visibilityChanged) {
                  await applyCleanVisibility("hide");
                }
              } else if (
                geometryChanged ||
                becomingVisible ||
                nativeToCleanTransitionHidden ||
                cleanRecordingOpeningHidden ||
                familyHandoffHidden
              ) {
                await applyCleanVisibility("show");
              }
            } else if (targetPresentationOwner === "native_borderless") {
              if (targetMode === "hidden") {
                if (
                  visibilityChanged &&
                  applied?.visible === true &&
                  !borderlessOpeningHidden
                ) {
                  await applyBorderlessVisibility("hide");
                }
              } else if (
                geometryChanged ||
                becomingVisible ||
                borderlessOpeningHidden ||
                appliedPresentationOwner !== "native_borderless"
              ) {
                await applyBorderlessVisibility("show");
              }
            } else if (targetMode === "hidden") {
              if (visibilityChanged) {
                await applyTargetRegion(false, true);
              }
            } else if (becomingVisible || familyHandoffHidden) {
              await applyTargetRegion(true);
            }
            if (!isCurrent()) return;
            appliedWindowGeometryRef.current = nextApplied;
            appliedWindowRegionRef.current = targetRegion;
            appliedWindowPresentationOwnerRef.current =
              targetPresentationOwner;
            if (
              dedicatedRecordingControlWindow &&
              targetMode === "hidden" &&
              nextApplied.visible === false
            ) {
              await emit(HUD_MAIN_VISIBILITY_EVENT, false).catch((error) => {
                console.warn(
                  `[HUD] main visibility event failed visible=false reason=${
                    error instanceof Error ? error.message : String(error)
                  }`,
                );
              });
            }
          } catch {
            // Browser development has no Tauri window. In a real shell, keep
            // the applied-mode gate closed so a failed native mutation cannot
            // reveal a card outside its current region/bounds.
            if (isTauri()) windowMutationSucceeded = false;
          }

          // "Geometry applied" signal for the hover-expansion sequencing. It has
          // to live AFTER the catch: browser development and jsdom have no real
          // Tauri window, so the try block throws and the card would never be
          // allowed to expand if this sat inside it.
          if (
            isCurrent() &&
            windowMutationSucceeded
          ) {
            const nextAppliedMode =
              targetMode === "hidden" ? null : presentationGeometryMode;
            appliedGeometryModeRef.current = nextAppliedMode;
            setAppliedGeometryMode((current) =>
              current === nextAppliedMode ? current : nextAppliedMode,
            );
          }

      });
    };

    if (
      presentation.mode === "hidden" &&
      !managedConsoleDismissedToHidden &&
      !parkedRecordingControl &&
      !(
        dedicatedArmingParkingEnabled &&
        appliedWindowGeometryRef.current?.visible !== true
      )
    ) {
      hideTimeout = window.setTimeout(reconcile, HIDE_ANIMATION_MS);
    } else {
      reconcile();
    }

    return () => {
      if (hideTimeout !== null) window.clearTimeout(hideTimeout);
      if (windowGenerationRef.current === generation) {
        windowGenerationRef.current += 1;
      }
    };
  }, [
    presentation.mode,
    managedConsoleDismissedToHidden,
    presentationGeometryMode,
    cleanGeometryPresentation,
    permanentControlPresentation,
    parkedRecordingControl,
    previewExpanded,
    previewNaturalTextHeight,
    measuredCardSize,
    responseExpanded,
    consoleRequestKey,
    consoleRequestW,
    consoleRequestH,
    dedicatedRecordingControlWindow,
    dedicatedArmingParkingEnabled,
  ]);

}
