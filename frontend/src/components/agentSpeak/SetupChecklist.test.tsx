import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  SetupChecklist,
  type SetupChecklistLineState,
} from "./SetupChecklist";
import type { CodingAgentProvider } from "../../lib/api/agents";

/**
 * `SetupChecklist` is presentational, so every test here is a DOM contract from
 * `design.md` decisions 29, 16 and 31, not a behaviour test. The behaviour (the
 * observers, the back-filling, the auto-submit guard) belongs to
 * `SetupStepTest`.
 */

const PTT = "right shift";

function lines(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>("[data-setup-checklist-line]"),
  );
}

function statusWords(container: HTMLElement): string[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>("[data-setup-checklist-status]"),
  ).map((el) => (el.textContent ?? "").replace(/^,\s*/, ""));
}

function region(container: HTMLElement): HTMLElement {
  return container.querySelector<HTMLElement>("[data-setup-checklist]")!;
}

function renderStates(
  states: SetupChecklistLineState[],
  provider: CodingAgentProvider | null = "claude",
) {
  return render(
    <SetupChecklist
      states={states}
      pushToTalkShortcut={PTT}
      provider={provider}
    />,
  );
}

describe("SetupChecklist - the live checklist over a selected coding-agent turn", () => {
  it("renders four lines with the locked titles", () => {
    const { container } = renderStates(["done", "done", "active", "pending"]);
    expect(lines(container)).toHaveLength(4);
    expect(container.textContent).toContain("Send a message in Claude Code");
    expect(container.textContent).toContain("Hear the reply in Whisperstream");
    expect(container.textContent).toContain(
      "Hold your dictation key and answer",
    );
    expect(container.textContent).toContain("Press Enter to send your reply");
  });

  it("renders text-only summary copy when speechEnabled is false", () => {
    const { container } = render(
      <SetupChecklist
        states={["done", "done", "active", "pending"]}
        pushToTalkShortcut={PTT}
        provider="claude"
        speechEnabled={false}
      />,
    );
    expect(lines(container)).toHaveLength(4);
    expect(container.textContent).toContain("See the reply in Whisperstream");
    expect(container.textContent).toContain("Read the summary in the overlay.");
    expect(container.textContent).not.toContain("Hear the reply in Whisperstream");
    expect(container.textContent).not.toContain(
      "Listen for the reply through your speakers",
    );
  });

  it("resolves the exact provider copy for Claude Code", () => {
    const { container } = renderStates(
      ["active", "pending", "pending", "pending"],
      "claude",
    );
    expect(container.textContent).toContain("Send a message in Claude Code");
    expect(container.textContent).toContain(
      "Ask your own Claude Code session anything. If you just connected, run /reload-plugins there first.",
    );
    expect(container.textContent).toContain(
      "Your words land in the HUD reply box. Press Enter to send them to Claude Code.",
    );
  });

  it("uses agent-agnostic copy when the provider is not yet resolved (null)", () => {
    const { container } = renderStates(
      ["active", "pending", "pending", "pending"],
      null,
    );
    expect(container.textContent).toContain("Send a message in your coding agent");
    expect(container.textContent).toContain(
      "Ask your coding agent anything. If you just connected it, reload it first.",
    );
    expect(container.textContent).toContain(
      "Your words land in the HUD reply box. Press Enter to send them to your coding agent.",
    );
    // No agent named, and no single command claimed, before a responder is known.
    expect(container.textContent).not.toContain("Claude Code");
    expect(container.textContent).not.toContain("Codex");
    expect(container.textContent).not.toContain("/reload-plugins");
    expect(container.textContent).not.toContain("/hooks");
  });

  it("resolves the exact provider copy for Codex without Claude-only instructions", () => {
    const { container } = renderStates(
      ["active", "pending", "pending", "pending"],
      "codex",
    );
    expect(container.textContent).toContain("Send a message in Codex");
    expect(container.textContent).toContain(
      "Ask your own Codex session anything. If you just connected, run /hooks there first.",
    );
    expect(container.textContent).toContain(
      "Your words land in the HUD reply box. Press Enter to send them to Codex.",
    );
    expect(container.textContent).not.toContain("Claude Code");
    expect(container.textContent).not.toContain("/reload-plugins");
  });

  // Decision 30's guard: with `claude_reply_auto_submit` on, line 4 is dropped
  // WHOLE. Never render a line whose event cannot arrive.
  it("renders three lines when line 4 is guarded off, dropping B1d and B2d", () => {
    const { container } = renderStates(["done", "active", "pending"]);
    expect(lines(container)).toHaveLength(3);
    expect(container.textContent).not.toContain(
      "Press Enter to send your reply",
    );
    expect(container.textContent).not.toContain("Your words land in the HUD");
  });

  // Decision 16. The research anti-pattern this exists to avoid: a user tries
  // to operate a checkbox-shaped mark on a system-verified item and stalls.
  it("has no checkbox affordance anywhere in the DOM", () => {
    const { container } = renderStates(["done", "active", "pending", "pending"]);
    expect(container.querySelector("input")).toBeNull();
    expect(container.querySelector("button")).toBeNull();
    expect(container.querySelector('[role="checkbox"]')).toBeNull();
    expect(container.querySelector("[tabindex]")).toBeNull();
    expect(container.querySelector('[class*="cursor-"]')).toBeNull();
    expect(container.querySelector('[class*="hover:"]')).toBeNull();
  });

  // A live region that mounts only when it has something to say is never
  // announced. Owner decision 4: ONE region for the list, not one per line.
  it("mounts one polite live region before any state change", () => {
    const { container } = renderStates(["pending", "pending", "pending"]);
    const live = region(container);
    expect(live.getAttribute("role")).toBe("status");
    expect(live.getAttribute("aria-live")).toBe("polite");
    expect(live.getAttribute("aria-atomic")).toBe("false");
    expect(
      container.querySelectorAll('[role="status"], [aria-live]'),
    ).toHaveLength(1);
  });

  it("marks the region busy only while a line is active", () => {
    const { container, rerender } = renderStates([
      "pending",
      "pending",
      "pending",
    ]);
    expect(region(container).getAttribute("aria-busy")).toBe("false");

    rerender(
      <SetupChecklist
        states={["active", "pending", "pending"]}
        pushToTalkShortcut={PTT}
        provider="claude"
      />,
    );
    expect(region(container).getAttribute("aria-busy")).toBe("true");

    rerender(
      <SetupChecklist
        states={["done", "done", "done"]}
        pushToTalkShortcut={PTT}
        provider="claude"
      />,
    );
    expect(region(container).getAttribute("aria-busy")).toBe("false");
  });

  // Decision 31, the alt-tab reference-card rule. The sub-text is what the user
  // comes back to, so it renders at full strength in EVERY state, done included.
  it("keeps every line's sub-text in every state, including done", () => {
    for (const state of ["pending", "active", "done"] as const) {
      const { container } = renderStates([state, state, state, state]);
      const subs = container.querySelectorAll("[data-setup-checklist-subtext]");
      expect(subs).toHaveLength(4);
      for (const sub of subs) {
        expect((sub.textContent ?? "").trim().length).toBeGreaterThan(0);
        expect(sub.className).toContain("text-steel");
        expect(sub.className).not.toContain("hidden");
      }
      // `B2a` must keep `/reload-plugins`: it is the remedy that travels with
      // the user to another window.
      expect(container.textContent).toContain("/reload-plugins");
    }
  });

  // `B2c`'s chip reads as part of its sentence: no aria-label on the chip, and
  // the sentence continues after it.
  it("renders line 3's dictation key as an inline chip inside its sentence", () => {
    const { container } = renderStates(["done", "done", "active", "pending"]);
    const sub = container.querySelectorAll<HTMLElement>(
      "[data-setup-checklist-subtext]",
    )[2];
    const chip = sub.querySelector<HTMLElement>("[data-setup-key]")!;
    expect(chip.textContent).toBe("Right Shift");
    expect(chip.getAttribute("aria-label")).toBeNull();
    expect(sub.textContent).toContain("Hold");
    expect(sub.textContent).toContain("while speaking, then release.");
  });

  // Signal 2: the status word is the entire state message for a user who
  // cannot see the disc.
  it("changes the status word across the three states", () => {
    const { container } = renderStates(["done", "active", "pending"]);
    expect(statusWords(container)).toEqual(["Done", "Now", "Not yet"]);
  });

  // Each line announces as `<title>, <status word>`, in that order.
  it("announces a line atomically as title then status word", () => {
    const { container } = renderStates(["active", "pending", "pending"]);
    const row = lines(container)[0].firstElementChild as HTMLElement;
    expect(row.getAttribute("aria-atomic")).toBe("true");
    expect(row.textContent).toBe("1Send a message in Claude Code, Now");
  });

  // Signal 3: a hierarchy shift, not an opacity reduction. A literal gray-out
  // would land near 2.6:1 in Light.
  it("lifts the title from text-steel to text-slate on activation", () => {
    const { container, rerender } = renderStates([
      "pending",
      "pending",
      "pending",
    ]);
    const title = () => container.querySelector<HTMLElement>("h3")!;
    expect(title().className).toContain("text-steel");
    expect(title().className).not.toContain("text-slate");

    rerender(
      <SetupChecklist
        states={["active", "pending", "pending"]}
        pushToTalkShortcut={PTT}
        provider="claude"
      />,
    );
    expect(title().className).toContain("text-slate");

    rerender(
      <SetupChecklist
        states={["done", "active", "pending"]}
        pushToTalkShortcut={PTT}
        provider="claude"
      />,
    );
    expect(title().className).toContain("text-slate");
  });

  // Signal 1: the shape swap, and the disc stays out of the accessibility tree.
  it("reuses SetupStatusDot verbatim and keeps every disc aria-hidden", () => {
    const { container } = renderStates(["done", "active", "pending"]);
    const discs = container.querySelectorAll<HTMLElement>("[data-setup-dot]");
    expect(discs).toHaveLength(3);
    for (const disc of discs) {
      expect(disc.getAttribute("aria-hidden")).toBe("true");
      // 20px, the existing size. No smaller sibling.
      expect(disc.className).toContain("h-5");
      expect(disc.className).toContain("w-5");
    }
    expect(discs[0].querySelector("svg")).not.toBeNull(); // done: check glyph
    expect(discs[1].textContent).toBe("2"); // active: numeral
    expect(discs[2].textContent).toBe("3"); // pending: numeral
  });

  // No raw hex anywhere: a raw literal is a dark-mode bug, not duplication.
  it("uses no raw colour literal of its own", () => {
    const { container } = renderStates(["done", "active", "pending", "pending"]);
    for (const el of container.querySelectorAll<HTMLElement>("*")) {
      if (el.hasAttribute("data-setup-dot") || el.hasAttribute("data-setup-key"))
        continue;
      const style = el.getAttribute("style") ?? "";
      expect(style).not.toMatch(/#[0-9a-f]{3,8}\b/i);
      expect(style).not.toMatch(/rgba?\(/i);
    }
  });
});
