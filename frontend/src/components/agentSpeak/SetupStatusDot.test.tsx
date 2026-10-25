import { render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SetupStatusDot } from "./SetupStatusDot";

const reduced = { value: false };

vi.mock("framer-motion", async (importOriginal) => {
  const actual = await importOriginal<typeof import("framer-motion")>();
  return { ...actual, useReducedMotion: () => reduced.value };
});

/** The one sanctioned theme-invariant structural literal in this flow. Steel
 *  resolves dark-on-light in Light and light-on-dark in Dark, so a single value
 *  draws the disc on both grounds. */
const STEEL_BORDER = "1px solid rgba(107,130,153,0.28)";

function disc(container: HTMLElement): HTMLElement {
  return container.querySelector<HTMLElement>("[data-setup-dot]")!;
}

describe("SetupStatusDot — the ordinal state disc", () => {
  beforeEach(() => {
    reduced.value = false;
  });

  it("shows the ordinal numeral while pending and while active", () => {
    for (const state of ["pending", "active"] as const) {
      const { container } = render(<SetupStatusDot ordinal={2} state={state} />);
      expect(disc(container).textContent).toBe("2");
    }
  });

  // The numeral-to-check swap is why pending versus done is not carried by
  // colour alone.
  it("replaces the numeral with a check glyph when done", () => {
    const { container } = render(<SetupStatusDot ordinal={1} state="done" />);
    expect(disc(container).textContent).toBe("");
    expect(disc(container).querySelector("svg")).not.toBeNull();
  });

  // copy-review.md, Accessibility notes: exposed, the numeral would make every
  // state change re-announce the ordinal ("1, Speaking...", "1, Listening...").
  // The exercise's own label is the entire message instead.
  it("is decorative in every state, so no state change re-announces the ordinal", () => {
    for (const state of ["pending", "active", "done"] as const) {
      const { container } = render(<SetupStatusDot ordinal={1} state={state} />);
      expect(disc(container)).toHaveAttribute("aria-hidden", "true");
      expect(container.querySelector("[role='status']")).toBeNull();
    }
  });

  // design.md decision 16: StepTest's RecStatus hardcodes rgba(148,163,184,0.5)
  // and #7EC489. Both are raw literals and therefore dark-mode bugs. These
  // three states are token-routed instead.
  it("routes pending through a steel ring with no fill", () => {
    const { container } = render(<SetupStatusDot ordinal={1} state="pending" />);
    expect(disc(container)).toHaveStyle({
      background: "transparent",
      border: STEEL_BORDER,
      boxShadow: "none",
    });
  });

  it("routes active through --accent-fill and the frost glow", () => {
    const { container } = render(<SetupStatusDot ordinal={1} state="active" />);
    expect(disc(container)).toHaveStyle({
      background: "var(--accent-fill)",
      boxShadow: "var(--shadow-frost-glow)",
    });
  });

  it("routes done through --color-mint and a mint color-mix glow", () => {
    const { container } = render(<SetupStatusDot ordinal={1} state="done" />);
    expect(disc(container)).toHaveStyle({
      background: "var(--color-mint)",
      boxShadow: "0 0 8px color-mix(in srgb, var(--color-mint) 55%, transparent)",
    });
  });

  it("carries no raw color literal beyond the sanctioned steel ring", () => {
    for (const state of ["pending", "active", "done"] as const) {
      const { container } = render(<SetupStatusDot ordinal={1} state={state} />);
      const style = (disc(container).getAttribute("style") ?? "")
        .split("rgba(107, 130, 153, 0.28)")
        .join("");
      expect(style).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(style).not.toContain("rgba(");
    }
  });

  it("breathes only in the active state", () => {
    for (const state of ["pending", "active", "done"] as const) {
      const { container } = render(<SetupStatusDot ordinal={1} state={state} />);
      expect(disc(container)).toHaveAttribute(
        "data-breathing",
        state === "active" ? "true" : "false",
      );
    }
  });

  it("does not breathe when the user prefers reduced motion", () => {
    reduced.value = true;
    const { container } = render(<SetupStatusDot ordinal={1} state="active" />);
    expect(disc(container)).toHaveAttribute("data-breathing", "false");
  });
});
