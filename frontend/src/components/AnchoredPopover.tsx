/**
 * AnchoredPopover — a portal helper that lifts a floating menu OUT of any
 * `overflow-hidden` / clipping ancestor and pins it (position: fixed) to an
 * anchor element's bounding rect.
 *
 * Why this exists: the Connection card and the profile-editor rows use
 * `overflow-hidden` for their expand/collapse height animation. A menu rendered
 * as `position: absolute` inside those cards gets clipped at the card edge (the
 * provider dropdown's last option and the app-picker popover were cut off and
 * unclickable). Portalling the menu to `document.body` escapes the clip while a
 * fixed position keyed off the anchor's `getBoundingClientRect()` keeps it
 * visually attached to its trigger.
 *
 * It opens BELOW the anchor by default, but flips ABOVE when there isn't enough
 * room below (e.g. a `+` on a profile row near the window bottom), and clamps
 * horizontally so the menu never spills off-screen. It re-measures on scroll /
 * resize so it stays anchored as the page moves. Click-outside / Escape close
 * logic stays with the *content* (the existing RunningAppPicker owns its own
 * outside-click handler); this wrapper is purely about position + escaping the
 * clip.
 */
import { useLayoutEffect, useRef, useState, type RefObject, type ReactNode } from "react";
import { createPortal } from "react-dom";

type Align = "left" | "right";

// Keep the menu this far from the viewport edges when it would otherwise spill.
const VIEWPORT_MARGIN = 8;

export function AnchoredPopover({
  anchorRef,
  align = "left",
  gap = 8,
  width,
  children,
}: {
  /** The trigger element the popover anchors to. */
  anchorRef: RefObject<HTMLElement | null>;
  /** Which edge of the anchor the popover's same edge lines up with. */
  align?: Align;
  /** Vertical gap (px) between the anchor and the popover. */
  gap?: number;
  /** Optional fixed width (px). When set, the popover is sized to it rather
   *  than stretching to the anchor's width. */
  width?: number;
  children: ReactNode;
}) {
  const contentRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number; width: number } | null>(null);

  useLayoutEffect(() => {
    const measure = () => {
      const el = anchorRef.current;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const w = width ?? r.width;

      // Horizontal: align to the requested edge, then clamp into the viewport.
      let left = align === "right" ? r.right - w : r.left;
      left = Math.min(Math.max(left, VIEWPORT_MARGIN), Math.max(VIEWPORT_MARGIN, vw - w - VIEWPORT_MARGIN));

      // Vertical: prefer below the anchor. If the content height (once measured)
      // doesn't fit below, flip above. Falls back to "below" before the first
      // measurement, then re-runs once the ref is populated.
      const menuH = contentRef.current?.offsetHeight ?? 0;
      const spaceBelow = vh - r.bottom - gap;
      const spaceAbove = r.top - gap;
      let top: number;
      if (menuH > 0 && menuH > spaceBelow && spaceAbove > spaceBelow) {
        // Flip up: bottom edge of menu sits `gap` above the anchor's top.
        top = Math.max(VIEWPORT_MARGIN, r.top - gap - menuH);
      } else {
        top = r.bottom + gap;
        // If it would run off the bottom, lift it up to fit (but never above the margin).
        if (menuH > 0 && top + menuH > vh - VIEWPORT_MARGIN) {
          top = Math.max(VIEWPORT_MARGIN, vh - VIEWPORT_MARGIN - menuH);
        }
      }

      setPos({ top, left, width: w });
    };
    // Measure twice: once for an initial placement, then again on the next frame
    // after the content has laid out so flip/clamp can use its real height.
    measure();
    const raf = requestAnimationFrame(measure);
    window.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
    };
  }, [anchorRef, align, gap, width, children]);

  if (!pos) return null;

  return createPortal(
    <div
      ref={contentRef}
      style={{
        position: "fixed",
        top: pos.top,
        left: pos.left,
        width: pos.width,
        zIndex: 60,
      }}
    >
      {children}
    </div>,
    document.body,
  );
}
