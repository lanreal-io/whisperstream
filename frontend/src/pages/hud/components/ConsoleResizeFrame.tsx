import { type CSSProperties } from "react";
import { type ConsoleResizeEdge } from "../geometry";
import { type ConsoleResizeHandlers } from "../useConsoleResize";

// Pointer-only resize affordances for the centered console: thin strips on
// each edge, larger squares at the corners, and the painted grip in the
// bottom-right corner. They are not focusable and carry no accessible name;
// the console's size is a pointer convenience and the default size is always
// usable from the keyboard.
const EDGE = 6;
const CORNER = 16;

const EDGES: Array<{
  edge: ConsoleResizeEdge;
  cursor: CSSProperties["cursor"];
  style: CSSProperties;
}> = [
  { edge: "n", cursor: "ns-resize", style: { top: 0, left: CORNER, right: CORNER, height: EDGE } },
  { edge: "s", cursor: "ns-resize", style: { bottom: 0, left: CORNER, right: CORNER, height: EDGE } },
  { edge: "w", cursor: "ew-resize", style: { left: 0, top: CORNER, bottom: CORNER, width: EDGE } },
  { edge: "e", cursor: "ew-resize", style: { right: 0, top: CORNER, bottom: CORNER, width: EDGE } },
  { edge: "nw", cursor: "nwse-resize", style: { top: 0, left: 0, width: CORNER, height: CORNER } },
  { edge: "ne", cursor: "nesw-resize", style: { top: 0, right: 0, width: CORNER, height: CORNER } },
  { edge: "sw", cursor: "nesw-resize", style: { bottom: 0, left: 0, width: CORNER, height: CORNER } },
  { edge: "se", cursor: "nwse-resize", style: { bottom: 0, right: 0, width: CORNER, height: CORNER } },
];

export function ConsoleResizeFrame({
  handlers,
}: {
  handlers: ConsoleResizeHandlers;
}) {
  return (
    <>
      <svg
        aria-hidden="true"
        data-testid="console-resize-grip"
        width="12"
        height="12"
        viewBox="0 0 12 12"
        className="pointer-events-none absolute"
        style={{ right: 7, bottom: 7, color: "var(--color-steel)", opacity: 0.7 }}
      >
        <path
          d="M11 4L4 11M11 8L8 11"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.3"
          strokeLinecap="round"
        />
      </svg>
      {EDGES.map(({ edge, cursor, style }) => (
        <div
          key={edge}
          aria-hidden="true"
          data-testid={`console-resize-${edge}`}
          data-hud-interactive="true"
          className="absolute"
          style={{ ...style, cursor, zIndex: 2, touchAction: "none" }}
          onPointerDown={(event) => handlers.onPointerDown(edge, event)}
          onPointerMove={handlers.onPointerMove}
          onPointerUp={handlers.onPointerUp}
          onPointerCancel={handlers.onPointerUp}
          onDoubleClick={(event) => event.stopPropagation()}
        />
      ))}
    </>
  );
}
