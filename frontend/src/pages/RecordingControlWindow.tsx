import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";
import { invoke, isTauri } from "@tauri-apps/api/core";
import { emit, listen } from "@tauri-apps/api/event";
import {
  currentMonitor,
  getCurrentWindow,
  Window,
} from "@tauri-apps/api/window";
import { useAppSocket } from "../lib/socket/connection";
import { useScreenReaderStatusSync } from "../lib/screenReaderStatus";
import { HUD_MAIN_VISIBILITY_EVENT, RECORDING_CONTROL_HOVER_EVENT, RECORDING_CONTROL_START_PENDING_EVENT, RECORDING_CONTROL_VISIBILITY_EVENT, RECORDING_CONTROL_VISIBILITY_STORAGE_KEY } from "../lib/recordingControl";
import { SCREEN_READER_STATUS_EVENT, SCREEN_READER_STATUS_STORAGE_KEY } from "../lib/accessibility";
import { DEDICATED_RECORDING_CONTROL_CONTENT_PAD, DEDICATED_RECORDING_CONTROL_WINDOW_H, DEDICATED_RECORDING_CONTROL_WINDOW_W, SNAP_THRESHOLD, dedicatedRecordingControlPositionFromPillAnchor, pillAnchorFromDedicatedRecordingControlPosition, pillAnchorFromWindowPosition, resolveHudGeometry } from "./hud/geometry";
import { HUD_DARK_DESKTOP_BORDER, HUD_DARK_DESKTOP_PALETTE, HUD_DARK_PERSISTENT_CONTROL_HIGHLIGHT, HUD_LIGHT_DESKTOP_BORDER, HUD_LIGHT_DESKTOP_PALETTE, HUD_LIGHT_PERSISTENT_CONTROL_HIGHLIGHT } from "./hud/material";
import { PersistentRecordingControl } from "./hud/views/RecordingControls";
import { RECORDING_CONTROL_COLLAPSE_MS, postHudDictationCommand, readRecordingControlVisibility } from "./hud/recordingBridge";

import { createRecordingControlReconciler } from "./hud/recordingControlReconciler";

const START_COMMAND_TIMEOUT_MS = 5000;

interface DragState {
  pointerId: number;
  captureTarget: HTMLDivElement;
  startScreenX: number;
  startScreenY: number;
  startWinX: number;
  startWinY: number;
  lastScreenX: number;
  lastScreenY: number;
  totalMovement: number;
  dragging: boolean;
  captured: boolean;
  guide: Window | null;
  movePromise: Promise<boolean>;
  settledRedrawRequested: boolean;
  scale: number;
  monitorX: number;
  monitorY: number;
  screenW: number;
  screenH: number;
}

/**
 * The idle permanent control has its own HWND and WebView.
 *
 * It never renders recording or Preview content. The main HUD claims those
 * states by physically hiding this HWND first; this window may return only
 * after the main HUD reports that its own hide has completed.
 */
export function RecordingControlWindow() {
  const [theme, setTheme] = useState<"light" | "dark">(() =>
    document.documentElement.getAttribute("data-theme") === "dark"
      ? "dark"
      : "light",
  );
  const [preferenceVisible, setPreferenceVisible] = useState(
    readRecordingControlVisibility,
  );
  const { connected, subscribe } = useAppSocket(preferenceVisible);
  const preferenceRef = useRef(preferenceVisible);
  const [screenReaderDetected, setScreenReaderDetected] = useState<boolean | null>(null);
  // Fail closed until the physical main HUD visibility has been queried.
  const [mainHudVisible, setMainHudVisible] = useState(true);
  const [mainHudKnown, setMainHudKnown] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [startPending, setStartPending] = useState(false);
  const collapseRef = useRef<number | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const releasedDragRef = useRef<DragState | null>(null);
  const pointerSequenceRef = useRef(0);
  const reconcilerRef = useRef<ReturnType<typeof createRecordingControlReconciler> | null>(null);
  const reconcilerMountedRef = useRef(false);
  const nativeVisibleRef = useRef(false);

  const shouldShow =
    preferenceVisible &&
    screenReaderDetected === false &&
    mainHudKnown &&
    !mainHudVisible;

  useEffect(() => {
    document.documentElement.style.setProperty(
      "background",
      "transparent",
      "important",
    );
    document.body.style.setProperty("background", "transparent", "important");
  }, []);

  useEffect(() => {
    const apply = (next: unknown) => {
      if (next !== "light" && next !== "dark") return;
      document.documentElement.setAttribute("data-theme", next);
      setTheme(next);
    };
    try {
      apply(localStorage.getItem("ws-theme"));
    } catch {
      // Keep the inline startup theme.
    }
    let unlisten: (() => void) | undefined;
    listen<string>("theme-changed", (event) => apply(event.payload)).then(
      (dispose) => {
        unlisten = dispose;
      },
      () => {
        // Browser development.
      },
    );
    return () => unlisten?.();
  }, []);

  useEffect(() => {
    const apply = (next: unknown) => {
      if (typeof next !== "boolean") return;
      if (next && !preferenceRef.current) setScreenReaderDetected(null);
      preferenceRef.current = next;
      setPreferenceVisible(next);
      if (!next) {
        setExpanded(false);
        setStartPending(false);
      }
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key !== RECORDING_CONTROL_VISIBILITY_STORAGE_KEY) return;
      apply(event.newValue === "true");
    };
    window.addEventListener("storage", onStorage);
    try {
      apply(
        localStorage.getItem(RECORDING_CONTROL_VISIBILITY_STORAGE_KEY) ===
          "true",
      );
    } catch {
      apply(false);
    }
    let unlisten: (() => void) | undefined;
    listen<boolean>(RECORDING_CONTROL_VISIBILITY_EVENT, (event) => {
      apply(event.payload);
    }).then(
      (dispose) => {
        unlisten = dispose;
      },
      () => {
        // Browser development.
      },
    );
    return () => {
      window.removeEventListener("storage", onStorage);
      unlisten?.();
    };
  }, []);

  const applyScreenReaderStatus = useCallback((next: unknown) => {
    if (typeof next !== "boolean") return;
    setScreenReaderDetected(next);
    try {
      localStorage.setItem(
        SCREEN_READER_STATUS_STORAGE_KEY,
        String(next),
      );
    } catch {
      // localStorage unavailable.
    }
  }, []);

  // Backend-owned status: one fetch, then WS pushes, refetched on reconnect.
  // Only the persistent recording control needs it; hidden means no requests.
  useScreenReaderStatusSync({
    enabled: preferenceVisible,
    connected,
    subscribe,
    apply: applyScreenReaderStatus,
  });

  useEffect(() => {
    let disposed = false;
    const apply = (next: unknown) => {
      if (!disposed) applyScreenReaderStatus(next);
    };
    const onStorage = (event: StorageEvent) => {
      if (event.key !== SCREEN_READER_STATUS_STORAGE_KEY) return;
      apply(event.newValue === "true");
    };
    window.addEventListener("storage", onStorage);

    let unlisten: (() => void) | undefined;
    listen<boolean>(SCREEN_READER_STATUS_EVENT, (event) => {
      apply(event.payload);
    }).then(
      (dispose) => {
        if (disposed) {
          dispose();
        } else {
          unlisten = dispose;
        }
      },
      () => {
        // Browser development.
      },
    );

    return () => {
      disposed = true;
      window.removeEventListener("storage", onStorage);
      unlisten?.();
    };
  }, [applyScreenReaderStatus]);

  useEffect(() => {
    let disposed = false;
    const apply = (next: unknown) => {
      if (disposed || typeof next !== "boolean") return;
      setMainHudVisible(next);
      setMainHudKnown(true);
      if (next) {
        setExpanded(false);
        setStartPending(false);
      }
    };
    let unlisten: (() => void) | undefined;
    // Register first, then query, so a main-window claim cannot race between
    // the physical read and listener setup.
    listen<boolean>(HUD_MAIN_VISIBILITY_EVENT, (event) => {
      apply(event.payload);
    })
      .then(async (dispose) => {
        if (disposed) {
          dispose();
          return;
        }
        unlisten = dispose;
        try {
          const hud = await Window.getByLabel("hud");
          if (hud) apply(await hud.isVisible());
        } catch {
          setMainHudKnown(false);
        }
      })
      .catch(() => setMainHudKnown(false));
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);

  useEffect(() => {
    reconcilerMountedRef.current = true;
    const reconciler = reconcilerRef.current ?? createRecordingControlReconciler(
      async (show, step, current) => {
        const controlWindow = getCurrentWindow();
        if (!show) {
          if (!nativeVisibleRef.current) return;
          await step(() => controlWindow.hide());
          nativeVisibleRef.current = false;
          console.log("[HUD] dedicated control hidden lifecycle=tauri");
          return;
        }
        const hudVisible = await step(async () => {
          const hud = await Window.getByLabel("hud");
          if (!hud) { setMainHudKnown(false); throw new Error("hud_unavailable"); }
          return await hud.isVisible();
        });
        if (!current()) return;
        if (hudVisible) {
          setMainHudVisible(true);
          setMainHudKnown(true);
          return;
        }
        const geometry = await step(() => resolveHudGeometry("seed", false, 0));
        if (!current()) return;
        const anchor = geometry.position === null ? null : pillAnchorFromWindowPosition("seed", geometry.position);
        const position = anchor === null ? null : dedicatedRecordingControlPositionFromPillAnchor(anchor);
        await step(() => controlWindow.setSize(new LogicalSize(
          DEDICATED_RECORDING_CONTROL_WINDOW_W, DEDICATED_RECORDING_CONTROL_WINDOW_H,
        )));
        if (!current()) return;
        if (position !== null) await step(() => controlWindow.setPosition(new LogicalPosition(position.x, position.y)));
        if (!current()) return;
        await step(() => controlWindow.show());
        nativeVisibleRef.current = true;
        console.log(`[HUD] dedicated control shown lifecycle=tauri window=${DEDICATED_RECORDING_CONTROL_WINDOW_W}x${DEDICATED_RECORDING_CONTROL_WINDOW_H}`);
      },
      async (step) => {
        // A timeout did not cancel its native command. Hide only after it settles.
        await step(() => getCurrentWindow().hide());
        nativeVisibleRef.current = false;
      },
    );
    reconcilerRef.current = reconciler;
    reconciler.resume();
    return () => {
      reconcilerMountedRef.current = false;
      reconciler.suspend();
      // StrictMode replays this effect synchronously with the same refs. Keep
      // its outstanding physical owner; only a real unmount retires it.
      queueMicrotask(() => {
        if (reconcilerMountedRef.current || reconcilerRef.current !== reconciler) return;
        reconciler.dispose();
        reconcilerRef.current = null;
      });
    };
  }, []);

  useEffect(() => {
    reconcilerRef.current?.request(shouldShow);
  }, [shouldShow]);

  useEffect(() => {
    if (!startPending) return;
    const timeout = window.setTimeout(() => {
      const tMs = performance.now();
      void emit(RECORDING_CONTROL_START_PENDING_EVENT, false).then(
        () => {
          console.log(
            `[HUD] dedicated start pending dispatch pending=false source=timeout t_ms=${tMs.toFixed(1)}`,
          );
        },
        (error) => {
          console.warn(
            `[HUD] dedicated start pending dispatch failed pending=false source=timeout t_ms=${tMs.toFixed(1)} reason=${
              error instanceof Error ? error.message : String(error)
            }`,
          );
        },
      );
      setStartPending(false);
    }, START_COMMAND_TIMEOUT_MS);
    return () => window.clearTimeout(timeout);
  }, [startPending]);

  useEffect(() => {
    return () => {
      if (collapseRef.current !== null) {
        window.clearTimeout(collapseRef.current);
      }
    };
  }, []);

  const onStart = useCallback(() => {
    if (!connected || startPending) return;
    const clickTMs = performance.now();
    setStartPending(true);
    console.log(`[HUD] dedicated start click t_ms=${clickTMs.toFixed(1)}`);
    void (async () => {
      try {
        await emit(RECORDING_CONTROL_START_PENDING_EVENT, true);
        console.log(
          `[HUD] dedicated start pending dispatch pending=true t_ms=${performance.now().toFixed(1)}`,
        );
      } catch (error) {
        // The event prepares presentation only. A frontend event failure must
        // never prevent the guarded backend start command itself.
        console.warn(
          `[HUD] dedicated start pending dispatch failed pending=true t_ms=${performance.now().toFixed(1)} reason=${
            error instanceof Error ? error.message : String(error)
          }`,
        );
      }
      try {
        await postHudDictationCommand("start");
        console.log(
          `[HUD] dedicated start request settled outcome=accepted t_ms=${performance.now().toFixed(1)}`,
        );
      } catch (error) {
        console.warn(
          `[HUD] dedicated start request error t_ms=${performance.now().toFixed(1)} reason=${
            error instanceof Error ? error.message : String(error)
          }`,
        );
        const rejectTMs = performance.now();
        try {
          await emit(RECORDING_CONTROL_START_PENDING_EVENT, false);
          console.log(
            `[HUD] dedicated start pending dispatch pending=false source=rejection t_ms=${rejectTMs.toFixed(1)}`,
          );
        } catch (emitError) {
          console.warn(
            `[HUD] dedicated start pending dispatch failed pending=false source=rejection t_ms=${rejectTMs.toFixed(1)} reason=${
              emitError instanceof Error
                ? emitError.message
                : String(emitError)
            }`,
          );
        }
        setStartPending(false);
      }
    })();
  }, [connected, startPending]);

  // Once the native shell reports hover (macOS), it alone drives expansion:
  // WebKit's DOM hover in this never-key panel goes stale after a click and
  // would fight it. Windows never sends native hover and keeps DOM hover.
  const nativeHoverRef = useRef(false);

  const expand = useCallback(() => {
    if (collapseRef.current !== null) {
      window.clearTimeout(collapseRef.current);
      collapseRef.current = null;
    }
    setExpanded(true);
  }, []);

  const scheduleCollapse = useCallback(() => {
    if (collapseRef.current !== null) {
      window.clearTimeout(collapseRef.current);
    }
    collapseRef.current = window.setTimeout(() => {
      collapseRef.current = null;
      setExpanded(false);
    }, RECORDING_CONTROL_COLLAPSE_MS);
  }, []);

  const onPointerEnter = useCallback(() => {
    console.log(`[HUD] dedicated control dom hover entered=true ignored=${nativeHoverRef.current}`);
    if (nativeHoverRef.current) return;
    expand();
  }, [expand]);

  const onPointerLeave = useCallback(() => {
    console.log(`[HUD] dedicated control dom hover entered=false ignored=${nativeHoverRef.current}`);
    if (dragRef.current?.dragging) return;
    // A sub-threshold move may leave the window before capture begins. Cancel
    // that pending press so an async position read cannot strand a drag after
    // the browser stops delivering its pointer events to this window.
    pointerSequenceRef.current += 1;
    dragRef.current = null;
    if (nativeHoverRef.current) return;
    scheduleCollapse();
  }, [scheduleCollapse]);

  // macOS reports hover natively; see nativeHoverRef.
  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    listen<boolean>(RECORDING_CONTROL_HOVER_EVENT, (event) => {
      const entered = event.payload === true;
      console.log(`[HUD] dedicated control native hover entered=${entered}`);
      nativeHoverRef.current = true;
      if (entered) {
        expand();
      } else if (!dragRef.current?.dragging) {
        scheduleCollapse();
      }
    }).then(
      (dispose) => {
        if (disposed) dispose();
        else unlisten = dispose;
      },
      () => {
        // Browser development.
      },
    );
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, [expand, scheduleCollapse]);

  const redrawCurrentWindow = useCallback(async () => {
    try {
      await invoke("redraw_current_window");
    } catch (error) {
      if (isTauri()) {
        console.error(
          `[HUD] dedicated control pointer redraw failed reason=${
            error instanceof Error ? error.message : String(error)
          }`,
        );
      }
    }
  }, []);

  const redrawAfterPointerDispatch = useCallback(() => {
    // Capture sees the glyph-only microphone even though its bubble handler
    // stops propagation. One microtask lets React and Framer finish the
    // synchronous pointer transition before the native child-inclusive paint.
    queueMicrotask(() => {
      void redrawCurrentWindow();
    });
  }, [redrawCurrentWindow]);

  const requestSettledDragRedraw = useCallback(
    (drag: DragState) => {
      if (drag.settledRedrawRequested) return;
      drag.settledRedrawRequested = true;
      void drag.movePromise.then((moved) => {
        if (moved) return redrawCurrentWindow();
      });
    },
    [redrawCurrentWindow],
  );

  const onPointerDown = useCallback(
    async (event: ReactPointerEvent<HTMLDivElement>) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest("[data-hud-interactive]")) return;
      if (dragRef.current !== null) return;
      const sequence = ++pointerSequenceRef.current;
      const captureTarget = event.currentTarget;
      const pointerId = event.pointerId;
      const startScreenX = event.screenX;
      const startScreenY = event.screenY;
      const win = getCurrentWindow();
      const [position, monitor] = await Promise.all([
        win.outerPosition(),
        currentMonitor(),
      ]);
      if (pointerSequenceRef.current !== sequence) return;
      const scale = monitor?.scaleFactor ?? 1;
      dragRef.current = {
        pointerId,
        captureTarget,
        startScreenX,
        startScreenY,
        startWinX: position.x / scale,
        startWinY: position.y / scale,
        lastScreenX: startScreenX,
        lastScreenY: startScreenY,
        totalMovement: 0,
        dragging: false,
        captured: false,
        guide: null,
        movePromise: Promise.resolve(true),
        settledRedrawRequested: false,
        scale,
        monitorX: (monitor?.position.x ?? 0) / scale,
        monitorY: (monitor?.position.y ?? 0) / scale,
        screenW: (monitor?.size.width ?? 1920) / scale,
        screenH: (monitor?.size.height ?? 1080) / scale,
      };
    },
    [],
  );

  const onPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      if (!drag) return;
      drag.totalMovement += Math.hypot(
        event.screenX - drag.lastScreenX,
        event.screenY - drag.lastScreenY,
      );
      drag.lastScreenX = event.screenX;
      drag.lastScreenY = event.screenY;
      if (drag.totalMovement < 5) return;

      let startedDragging = false;
      if (!drag.dragging) {
        drag.dragging = true;
        startedDragging = true;
        try {
          drag.captureTarget.setPointerCapture(drag.pointerId);
          drag.captured = true;
        } catch {
          // Continue while the pointer remains inside the window. Never
          // manufacture a second capture transition for a failed attempt.
        }
        void (async () => {
          try {
            const guide = await Window.getByLabel("snap-guide");
            if (!guide || dragRef.current !== drag || !drag.dragging) return;
            await guide.setSize(new LogicalSize(1, drag.screenH));
            await guide.setPosition(
              new LogicalPosition(
                drag.monitorX + (drag.screenW - 1) / 2,
                drag.monitorY,
              ),
            );
            await guide.setIgnoreCursorEvents(true);
            if (dragRef.current !== drag || !drag.dragging) return;
            drag.guide = guide;
            await guide.show();
          } catch {
            // Snap guide unavailable.
          }
        })();
      }

      let nextX =
        drag.startWinX + (event.screenX - drag.startScreenX);
      const nextY =
        drag.startWinY + (event.screenY - drag.startScreenY);
      const screenCenterX = drag.monitorX + drag.screenW / 2;
      const windowCenterX =
        nextX + DEDICATED_RECORDING_CONTROL_WINDOW_W / 2;
      if (Math.abs(windowCenterX - screenCenterX) <= SNAP_THRESHOLD) {
        nextX =
          screenCenterX - DEDICATED_RECORDING_CONTROL_WINDOW_W / 2;
      }
      drag.movePromise = drag.movePromise.then(async () => {
        try {
          await invoke("set_current_window_logical_position", {
            x: nextX,
            y: nextY,
          });
          return true;
        } catch (error) {
          if (isTauri()) {
            console.error(
              `[HUD] dedicated control drag move failed reason=${
                error instanceof Error ? error.message : String(error)
              }`,
            );
          }
          return false;
        }
      });
      if (startedDragging) {
        // The first native move is a proven paint boundary. Redraw once after
        // that synchronous move, never on the later move stream.
        void drag.movePromise.then((moved) => {
          if (moved) return redrawCurrentWindow();
        });
      }
    },
    [redrawCurrentWindow],
  );

  const onPointerUp = useCallback(
    async (event: ReactPointerEvent<HTMLDivElement>) => {
      pointerSequenceRef.current += 1;
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      dragRef.current = null;
      drag.totalMovement += Math.hypot(
        event.screenX - drag.lastScreenX,
        event.screenY - drag.lastScreenY,
      );
      // A hold or click is not a drag. In particular it must never enter
      // WebView2 pointer capture or perform release-time window bookkeeping:
      // both transitions have exposed stale transparent padding in this HUD.
      if (!drag.dragging) return;
      if (drag.captured) {
        releasedDragRef.current = drag;
      } else {
        requestSettledDragRedraw(drag);
      }
      try {
        await drag.guide?.hide();
      } catch {
        // Ignore guide teardown failures.
      }
      try {
        await drag.movePromise;
        const position = await getCurrentWindow().outerPosition();
        const anchor = pillAnchorFromDedicatedRecordingControlPosition({
          x: position.x / drag.scale,
          y: position.y / drag.scale,
        });
        localStorage.setItem("hud-position", JSON.stringify(anchor));
      } catch {
        // Position persistence is best effort.
      }
    },
    [requestSettledDragRedraw],
  );

  const onPointerCancel = useCallback(
    async (event: ReactPointerEvent<HTMLDivElement>) => {
      pointerSequenceRef.current += 1;
      const drag = dragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      dragRef.current = null;
      if (!drag.dragging) return;
      if (drag.captured) {
        releasedDragRef.current = drag;
      } else {
        requestSettledDragRedraw(drag);
      }
      try {
        await drag.guide?.hide();
      } catch {
        // Ignore guide teardown failures. Pointer capture releases implicitly.
      }
    },
    [requestSettledDragRedraw],
  );

  const onLostPointerCapture = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = releasedDragRef.current;
      if (!drag || drag.pointerId !== event.pointerId) return;
      releasedDragRef.current = null;
      requestSettledDragRedraw(drag);
    },
    [requestSettledDragRedraw],
  );

  const desktopBorder =
    theme === "dark"
      ? HUD_DARK_DESKTOP_BORDER
      : HUD_LIGHT_DESKTOP_BORDER;
  const desktopHighlight =
    theme === "dark"
      ? HUD_DARK_PERSISTENT_CONTROL_HIGHLIGHT
      : HUD_LIGHT_PERSISTENT_CONTROL_HIGHLIGHT;

  return (
    <div
      className="relative flex h-full w-full cursor-grab flex-col items-center justify-end active:cursor-grabbing"
      data-testid="recording-control-window-root"
      data-hud-palette={`desktop-${theme}`}
      style={{
        ...(theme === "dark"
          ? HUD_DARK_DESKTOP_PALETTE
          : HUD_LIGHT_DESKTOP_PALETTE),
        background: "transparent",
        paddingBottom: DEDICATED_RECORDING_CONTROL_CONTENT_PAD,
        touchAction: "none",
      }}
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      onPointerDownCapture={redrawAfterPointerDispatch}
      onPointerUpCapture={redrawAfterPointerDispatch}
      onPointerCancelCapture={redrawAfterPointerDispatch}
      onLostPointerCapture={onLostPointerCapture}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
    >
      <PersistentRecordingControl
        expanded={expanded}
        returningFromActive={false}
        desktopBorder={desktopBorder}
        desktopHighlight={desktopHighlight}
        connected={connected}
        pending={startPending}
        onStart={onStart}
      />
    </div>
  );
}
