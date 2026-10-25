import { readFileSync } from "node:fs";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import {
  HUD_EXPAND_TOGGLE_H,
  HUD_EXPAND_TOGGLE_W,
  HudExpandToggle,
} from "./HudExpandToggle";
import { ClaudeExpandedCard } from "../views/ExpandedResponse";
import { PreviewCard } from "../views/LivePreview";

// Vitest runs with `css: false`, so read the token contract from disk.
const globalsCss = readFileSync(
  path.join(process.cwd(), "src/styles/globals.css"),
  "utf-8",
).split(String.fromCharCode(13)).join("");

afterEach(() => cleanup());

function themeBlock(selector: string): string {
  const start = globalsCss.indexOf(`${selector} {`);
  expect(start).toBeGreaterThanOrEqual(0);
  return globalsCss.slice(start, globalsCss.indexOf("\n}", start));
}

const cardProps = {
  provider: "claude" as const,
  body: "Refactored the config loader.",
  sourceName: "whisperstream",
  speaking: false,
  spineSegments: [1],
  remainingCount: 0,
  accessibleLabel: "Claude",
  railPhase: "resting" as const,
  countdownHint: null,
  railProps: {},
};

describe("HudExpandToggle", () => {
  it("is a 40x28 icon-only pill drawn from theme tokens", () => {
    render(<HudExpandToggle expanded={false} label="Expand response" onToggle={() => {}} />);
    const button = screen.getByRole("button", { name: "Expand response" });

    expect(HUD_EXPAND_TOGGLE_W).toBe(40);
    expect(HUD_EXPAND_TOGGLE_H).toBe(28);
    expect(button).toHaveStyle({ width: "40px", height: "28px" });
    expect(button).toHaveClass("rounded-full");
    expect(button.style.border).toBe("1px solid var(--hud-expand-border)");
    expect(button.style.color).toBe("var(--hud-expand-icon)");
    expect(button.className).toContain("bg-[var(--surface-control)]");
    expect(button).toHaveAttribute("title", "Expand response");
    expect(button.textContent).toBe("");
    expect(button.querySelector("svg")).toHaveAttribute("width", "14");
    expect(button.querySelector("path")).toHaveAttribute("stroke-width", "1.4");
  });

  it("draws outward arrows to expand and inward arrows to collapse", () => {
    const { rerender } = render(
      <HudExpandToggle expanded={false} label="Expand" onToggle={() => {}} />,
    );
    const outward = screen.getByRole("button").querySelector("path")?.getAttribute("d");
    rerender(<HudExpandToggle expanded label="Collapse" onToggle={() => {}} />);
    const inward = screen.getByRole("button").querySelector("path")?.getAttribute("d");

    expect(outward).toBeTruthy();
    expect(inward).toBeTruthy();
    expect(outward).not.toBe(inward);
    expect(screen.getByRole("button")).toHaveAttribute("data-expanded", "true");
  });

  it("toggles without starting a HUD drag or a double-click toggle", () => {
    const onToggle = vi.fn();
    const parentPointerDown = vi.fn();
    const parentDoubleClick = vi.fn();
    render(
      <div onPointerDown={parentPointerDown} onDoubleClick={parentDoubleClick}>
        <HudExpandToggle expanded={false} label="Expand" onToggle={onToggle} />
      </div>,
    );
    const button = screen.getByRole("button");
    fireEvent.pointerDown(button);
    fireEvent.click(button);
    fireEvent.doubleClick(button);

    expect(onToggle).toHaveBeenCalledTimes(1);
    expect(parentPointerDown).not.toHaveBeenCalled();
    expect(parentDoubleClick).not.toHaveBeenCalled();
    expect(button).toHaveAttribute("data-hud-interactive", "true");
  });

  it("defines accent values in Light and neutral values in Dark", () => {
    const light = themeBlock('[data-theme="light"]');
    const dark = themeBlock('[data-theme="dark"]');
    expect(light).toContain("--hud-expand-border: rgba(85, 144, 196, 0.45);");
    expect(light).toContain("--hud-expand-icon: #3A6E9E;");
    expect(dark).toContain("--hud-expand-border: rgba(255, 255, 255, 0.16);");
    expect(dark).toContain("--hud-expand-icon: #A6B9CC;");
  });

  it("is the one control on the glance card, the console and Live Preview", () => {
    const onToggle = vi.fn();
    const { unmount } = render(
      <ClaudeExpandedCard {...cardProps} summaryMode onToggleExpanded={onToggle} />,
    );
    const expand = screen.getByRole("button", { name: "Expand response" });
    expect(expand).toHaveAttribute("data-expanded", "false");
    expect(expand).toHaveStyle({ width: "40px", height: "28px" });
    fireEvent.click(expand);
    expect(onToggle).toHaveBeenCalledTimes(1);
    unmount();

    render(<ClaudeExpandedCard {...cardProps} onToggleExpanded={onToggle} />);
    const collapse = screen.getByRole("button", { name: "Collapse response" });
    expect(collapse).toHaveAttribute("data-expanded", "true");
    expect(collapse).toHaveStyle({ width: "40px", height: "28px" });
    // The console has no separate close button.
    expect(screen.queryByRole("button", { name: /close/i })).toBeNull();
    cleanup();

    render(
      <PreviewCard
        phase="recording"
        preview={null}
        rms={0}
        peak={0}
        barColor="var(--frost-muted)"
        expanded={false}
        cardWidth={320}
        cardHeight={160}
        closing={false}
        hudOriginRecording={false}
        commandsDisabled={false}
        onCancel={() => {}}
        onFinish={() => {}}
        onExpandedChange={() => {}}
        onNaturalTextHeightChange={() => {}}
        agentCompose={false}
        profileMark={null}
      />,
    );
    const preview = screen.getByRole("button", { name: "Expand preview" });
    expect(preview).toHaveStyle({ width: "40px", height: "28px" });
    expect(preview.style.border).toBe("1px solid var(--hud-expand-border)");
  });
});
