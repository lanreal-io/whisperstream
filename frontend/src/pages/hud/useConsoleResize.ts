import { useCallback, useEffect, useRef, useState } from "react";
import {
  CONSOLE_PAD_X,
  CONSOLE_PAD_Y,
  type ConsoleResizeEdge,
  type ConsoleSize,
  type ConsoleSizeRequest,
  type HudWorkArea,
  clampConsoleSize,
  currentHudDisplay,
  resizedConsoleSize,
  saveConsoleSize,
} from "./geometry";

/** Pointer handlers for one resize handle. The handle that starts a drag
 *  captures the pointer, so it also receives every move and the release. */
export interface ConsoleResizeHandlers {
  onPointerDown: (
    edge: ConsoleResizeEdge,
    event: React.PointerEvent<HTMLElement>,
  ) => void;
  onPointerMove: (event: React.PointerEvent<HTMLElement>) => void;
  onPointerUp: (event: React.PointerEvent<HTMLElement>) => void;
}

interface ResizeDrag {
  pointerId: number;
  edge: ConsoleResizeEdge;
  startScreenX: number;
  startScreenY: number;
  start: ConsoleSize;
  displayKey: string | null;
  workArea: HudWorkArea | null;
  last: ConsoleSize | null;
  ended: boolean;
}

/**
 * The console's resize intent. It produces only a size request for the
 * display the console is on; the native reconciler stays the one owner that
 * moves and resizes the window (centered, through `resolveHudGeometry`). The
 * size is remembered for that display when the drag ends, and the request is
 * dropped whenever the console closes.
 */
export function useConsoleResize(active: boolean): {
  request: ConsoleSizeRequest | null;
  handlers: ConsoleResizeHandlers;
} {
  const [request, setRequest] = useState<ConsoleSizeRequest | null>(null);
  const dragRef = useRef<ResizeDrag | null>(null);

  useEffect(() => {
    if (active) return;
    dragRef.current = null;
    setRequest(null);
  }, [active]);

  const finish = useCallback((drag: ResizeDrag) => {
    drag.ended = true;
    if (dragRef.current === drag) dragRef.current = null;
    if (drag.displayKey !== null && drag.last !== null) {
      saveConsoleSize(drag.displayKey, drag.last);
    }
    // The remembered size now equals the last request, so dropping the
    // request resolves to the same geometry without another native step.
    setRequest(null);
  }, []);

  const onPointerDown = useCallback(
    (edge: ConsoleResizeEdge, event: React.PointerEvent<HTMLElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      event.currentTarget.setPointerCapture?.(event.pointerId);
      const drag: ResizeDrag = {
        pointerId: event.pointerId,
        edge,
        startScreenX: event.screenX,
        startScreenY: event.screenY,
        // The card always fills its window less the shadow pad.
        start: {
          width: window.innerWidth - CONSOLE_PAD_X * 2,
          height: window.innerHeight - CONSOLE_PAD_Y * 2,
        },
        displayKey: null,
        workArea: null,
        last: null,
        ended: false,
      };
      dragRef.current = drag;
      void currentHudDisplay()
        .then((display) => {
          if (display === null || drag.ended) return;
          drag.displayKey = display.displayKey;
          drag.workArea = display.workArea;
        })
        .catch(() => {
          // No native monitor (browser development): nothing to resize.
        });
    },
    [],
  );

  const onPointerMove = useCallback(
    (event: React.PointerEvent<HTMLElement>) => {
      const drag = dragRef.current;
      if (drag === null || drag.pointerId !== event.pointerId) return;
      // A release that landed elsewhere must not leave a hover resizing.
      if (event.buttons === 0) {
        finish(drag);
        return;
      }
      if (drag.displayKey === null || drag.workArea === null) return;
      const next = clampConsoleSize(
        resizedConsoleSize(
          drag.start,
          drag.edge,
          event.screenX - drag.startScreenX,
          event.screenY - drag.startScreenY,
        ),
        drag.workArea,
      );
      if (
        drag.last !== null &&
        drag.last.width === next.width &&
        drag.last.height === next.height
      ) {
        return;
      }
      drag.last = next;
      setRequest({ displayKey: drag.displayKey, ...next });
    },
    [finish],
  );

  const onPointerUp = useCallback(
    (event: React.PointerEvent<HTMLElement>) => {
      const drag = dragRef.current;
      if (drag === null || drag.pointerId !== event.pointerId) return;
      event.stopPropagation();
      finish(drag);
    },
    [finish],
  );

  return { request, handlers: { onPointerDown, onPointerMove, onPointerUp } };
}
