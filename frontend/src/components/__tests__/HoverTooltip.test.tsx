import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { HoverTooltip } from "../HoverTooltip";

afterEach(cleanup);

describe("HoverTooltip", () => {
  it("preserves the existing hover-only contract by default", () => {
    render(
      <HoverTooltip content="Helpful detail">
        <span>Trigger</span>
      </HoverTooltip>,
    );

    const bubble = screen.getByRole("tooltip");
    expect(bubble.id).toBe("");
    expect(bubble.className).toContain("opacity-0");
    expect(bubble.className).toContain("group-hover:opacity-100");
    expect(bubble.className).not.toContain("focus");
    expect(bubble.className).toContain("right-0");
    expect(bubble.className).toContain("top-full");
  });

  it("wires a stable bubble ID and opt-in caller focus visibility class", () => {
    render(
      <HoverTooltip
        content="Profile: Code"
        bubbleId="transcript-provenance-42"
        focusVisibleClassName="group-focus-visible/transcript:opacity-100"
      >
        <span>Enhanced</span>
      </HoverTooltip>,
    );

    const bubble = screen.getByRole("tooltip");
    expect(bubble.id).toBe("transcript-provenance-42");
    expect(bubble.className).toContain(
      "group-focus-visible/transcript:opacity-100",
    );
    expect(bubble.className).toContain("group-hover:opacity-100");
  });

  it("keeps explicit placement and theme overlay styling", () => {
    render(
      <HoverTooltip content="Detail" side="bottom-left" width="w-64">
        <span>Trigger</span>
      </HoverTooltip>,
    );

    const bubble = screen.getByRole("tooltip");
    expect(bubble.className).toContain("left-0");
    expect(bubble.className).toContain("top-full");
    expect(bubble.className).toContain("w-64");
    expect(bubble.style.background).toBe("var(--surface-overlay)");
    expect(bubble.style.border).toBe("1px solid var(--border-emphasis)");
  });

  it("renders only the trigger when content is null", () => {
    render(
      <HoverTooltip
        content={null}
        bubbleId="unused-tooltip"
        focusVisibleClassName="group-focus-visible/transcript:opacity-100"
      >
        <span>Trigger only</span>
      </HoverTooltip>,
    );

    expect(screen.getByText("Trigger only")).toBeTruthy();
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(document.getElementById("unused-tooltip")).toBeNull();
  });

  it("centers the bubble above the trigger with side top", () => {
    render(
      <HoverTooltip content="Above" side="top">
        <span>Trigger</span>
      </HoverTooltip>,
    );

    const bubble = screen.getByRole("tooltip");
    expect(bubble.className).toContain("bottom-full");
    expect(bubble.className).toContain("left-1/2");
    expect(bubble.className).toContain("-translate-x-1/2");
    expect(bubble.className).not.toContain("top-full");
  });
});
