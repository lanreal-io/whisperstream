import { useEffect, useRef, useState, type ReactNode } from "react";

interface ReadingWellProps {
  children: ReactNode;
  /** Fixed-height box (px). Existing callers (EulaPopup) keep using this and are
   *  unchanged. Exactly one of `height` / `maxHeight` should be set. */
  height?: number;
  /** Auto-height up to this cap (px): the well sizes to its content, then scrolls
   *  once the content exceeds the cap. Used by the expanded transcript card so a
   *  short transcript takes little space. */
  maxHeight?: number;
  ariaLabel: string;
  /** Optional description associated with the focusable scroll region. */
  ariaDescribedBy?: string;
  /** Body text size in px. Defaults to 12 (EulaPopup's dense legal text); the
   *  transcript well passes 14 so the expanded text reads as primary content. */
  fontSize?: number;
  /** Text font-family. Defaults to the app body font; the transcript well passes
   *  the quoted-content font (Inter) so a transcript reads less styled than the
   *  app chrome. EulaPopup keeps the default. */
  fontFamily?: string;
  /** Text weight. Omit to inherit the app body weight (500); the transcript well
   *  passes 400 so quoted content reads as neutral, document-like text rather
   *  than the heavier app chrome. */
  fontWeight?: number;
  /** Whether the scroll region may receive keyboard focus. */
  focusable?: boolean;
}

export function ReadingWell({
  children,
  height,
  maxHeight,
  ariaLabel,
  ariaDescribedBy,
  fontSize = 12,
  fontFamily = "var(--font-body)",
  fontWeight,
  focusable = true,
}: ReadingWellProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(false);
  // Whether the content actually overflows the well. Drives the bottom fade, so
  // a short (non-scrolling) transcript shows no spurious fade.
  const [scrollable, setScrollable] = useState(false);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const recompute = () => {
      setScrollable(el.scrollHeight > el.clientHeight + 1);
      setAtBottom(el.scrollTop + el.clientHeight >= el.scrollHeight - 12);
    };
    recompute();
    el.addEventListener("scroll", recompute, { passive: true });
    // Content can change (toggling Enhanced <-> Original); observe size too.
    const ro = new ResizeObserver(recompute);
    ro.observe(el);
    return () => {
      el.removeEventListener("scroll", recompute);
      ro.disconnect();
    };
  }, [children]);

  // Auto mode sizes to content up to maxHeight; fixed mode is the old behavior.
  const sizing =
    height != null
      ? { height }
      : { height: "auto" as const, maxHeight: maxHeight ?? 220 };

  return (
    <div
      className="relative rounded-2xl overflow-hidden"
      style={{
        background: "var(--surface-control)",
        border: "1px solid var(--border-hairline)",
        boxShadow:
          "inset 0 1px 2px rgba(107, 130, 153, 0.10), inset 0 -1px 0 rgba(255, 255, 255, 0.4)",
      }}
    >
      <div
        ref={scrollRef}
        role="region"
        aria-label={ariaLabel}
        aria-describedby={ariaDescribedBy}
        tabIndex={focusable ? 0 : undefined}
        className="select-text outline-none focus-visible:ring-2 focus-visible:ring-frost"
        style={{
          ...sizing,
          overflowY: "auto",
          padding: "20px 24px",
          fontFamily,
          fontSize: `${fontSize}px`,
          fontWeight,
          lineHeight: "1.65",
          color: "var(--color-slate)",
          whiteSpace: "pre-wrap",
        }}
      >
        {children}
      </div>
      {/* Fade only when content actually overflows AND we are not at the bottom. */}
      <div
        aria-hidden="true"
        className="absolute left-0 right-0 bottom-0 h-3 pointer-events-none transition-opacity duration-200"
        style={{
          opacity: scrollable && !atBottom ? 1 : 0,
          background:
            "linear-gradient(to bottom, color-mix(in srgb, var(--color-canvas) 0%, transparent), color-mix(in srgb, var(--color-canvas) 90%, transparent))",
        }}
      />
    </div>
  );
}
