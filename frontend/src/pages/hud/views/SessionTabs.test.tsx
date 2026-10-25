import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HudSession } from "../../../lib/socket/agentEvents";
import {
  SESSION_TAB_NAME_MIN_W,
  SessionTabs,
  sessionTabMinWidth,
} from "./ExpandedResponse";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function sessions(names: string[], mixed = false): HudSession[] {
  return names.map((name, index) => ({
    session_handle: `tab-${index}`,
    agent: mixed && index % 2 === 1 ? "codex" : "claude",
    source_name: name,
    unread: false,
    needs_input: false,
    can_reply: true,
  }));
}

const tabs = () => screen.getAllByRole("tab");

describe("SessionTabs layout", () => {
  it("lets every tab show its full name when there is room", () => {
    render(
      <SessionTabs
        sessions={sessions(["whisperstream", "fixture-studio", "site"])}
        activeHandle="tab-0"
        onSelect={() => {}}
      />,
    );
    for (const tab of tabs()) {
      // No fixed pixel cap: a tab is never cut while the strip has room.
      expect(tab.style.maxWidth === "" || tab.style.maxWidth === "max-content").toBe(true);
      const name = tab.querySelector("[data-session-tab-name]") as HTMLElement;
      expect(name).toHaveClass("min-w-0", "truncate");
    }
    expect(tabs()[1]).toHaveTextContent("fixture-studio");
  });

  it("shrinks inactive tabs evenly to the badge plus about three letters, keeping the active tab whole", () => {
    render(
      <SessionTabs
        sessions={sessions(["whisperstream", "fixture-studio", "release-notes", "docs-sync", "site"])}
        activeHandle="tab-2"
        onSelect={() => {}}
      />,
    );
    const [first, second, active, fourth, fifth] = tabs();
    expect(active).toHaveAttribute("data-active", "true");
    expect(active.style.flex).toBe("0 0 auto");
    expect(active.style.minWidth).toBe("");
    expect(active.style.maxWidth).toBe("");

    const floor = sessionTabMinWidth(false);
    for (const inactive of [first, second, fourth, fifth]) {
      // Equal flex share from a zero basis is what makes the shrink even.
      expect(inactive.style.flex).toBe("1 1 0px");
      expect(inactive.style.maxWidth).toBe("max-content");
    }
    for (const long of [first, second, fourth]) {
      expect(long.style.minWidth).toBe(`${floor}px`);
    }
    // "site" is already about three letters: it is never padded past its text.
    expect(fifth.style.minWidth).toBe("max-content");
    // Status slot, badge, padding and gaps plus room for about three letters.
    expect(floor).toBe(7 + 8 + 6 + SESSION_TAB_NAME_MIN_W + 6 + 6 + 9);
    expect(SESSION_TAB_NAME_MIN_W).toBeGreaterThanOrEqual(18);
    // A mixed strip also reserves the provider mark.
    expect(sessionTabMinWidth(true)).toBe(floor + 9 + 6);
  });

  it("scrolls past the floor with fade edges and keeps the active tab in view", () => {
    const scrollIntoView = vi.fn();
    const original = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    try {
      const names = Array.from({ length: 10 }, (_unused, index) => `session-${index + 1}`);
      const { rerender } = render(
        <SessionTabs sessions={sessions(names, true)} activeHandle="tab-0" onSelect={() => {}} />,
      );
      const strip = screen.getByTestId("hud-session-tabs");
      expect(strip).toHaveClass("overflow-x-auto");

      Object.defineProperty(strip, "scrollWidth", { configurable: true, value: 900 });
      Object.defineProperty(strip, "clientWidth", { configurable: true, value: 400 });
      Object.defineProperty(strip, "scrollLeft", { configurable: true, writable: true, value: 120 });
      act(() => {
        strip.dispatchEvent(new Event("scroll"));
      });
      const fades = strip.parentElement!.querySelectorAll('[aria-hidden="true"].pointer-events-none');
      expect(fades).toHaveLength(2);

      scrollIntoView.mockClear();
      rerender(
        <SessionTabs sessions={sessions(names, true)} activeHandle="tab-8" onSelect={() => {}} />,
      );
      expect(scrollIntoView).toHaveBeenCalledWith({ block: "nearest", inline: "nearest" });
      expect(scrollIntoView.mock.instances[0]).toBe(tabs()[8]);
    } finally {
      HTMLElement.prototype.scrollIntoView = original;
    }
  });

  it("shows the full name on hover only when the tab is shortened", () => {
    render(
      <SessionTabs
        sessions={sessions(["fixture-studio", "site"])}
        activeHandle="tab-1"
        onSelect={() => {}}
      />,
    );
    const shortened = tabs()[0];
    const name = shortened.querySelector("[data-session-tab-name]") as HTMLElement;
    Object.defineProperty(name, "scrollWidth", { configurable: true, value: 90 });
    Object.defineProperty(name, "clientWidth", { configurable: true, value: 24 });
    fireEvent.pointerEnter(shortened);
    expect(shortened).toHaveAttribute("title", "fixture-studio");

    Object.defineProperty(name, "clientWidth", { configurable: true, value: 90 });
    fireEvent.pointerEnter(shortened);
    expect(shortened).toHaveAttribute("title", "");
  });
});
