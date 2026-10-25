import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AgentTimeline } from "./AgentTimeline";

afterEach(cleanup);

const turns = [
  { id: "a", role: "user" as const, text: "Check it." },
  { id: "b", role: "assistant" as const, text: "**Done.**" },
  { id: "c", role: "user" as const, text: "Ship it." },
];

describe("AgentTimeline frost panes (Variant B)", () => {
  it("marks only the newest turn, of either role, as Latest", () => {
    render(<AgentTimeline provider="claude" conversation={{ session_handle: "h", activity: "finished", turns }} />);
    const panes = Array.from(document.querySelectorAll("article"));
    expect(panes).toHaveLength(3);
    expect(screen.getAllByTestId("agent-turn-latest")).toHaveLength(1);
    expect(panes[2]).toHaveAttribute("data-newest-turn", "true");
    expect(panes[2].querySelector('[data-testid="agent-turn-spine"]')).not.toBeNull();
    expect(panes[2].getAttribute("style")).toContain("var(--border-accent)");
    expect(panes[0].getAttribute("style")).toContain("var(--border-hairline)");
  });

  it("insets and tints your replies, and leaves agent panes full width", () => {
    render(<AgentTimeline provider="codex" conversation={{ session_handle: "h", activity: "finished", turns }} />);
    const [user, agent] = Array.from(document.querySelectorAll("article"));
    expect(user.style.marginLeft).toBe("56px");
    expect(user.style.background).toBe("var(--hud-user-tint)");
    expect(agent.style.marginLeft).toBe("0px");
    expect(agent.style.background).toBe("var(--hud-pane)");
    expect(agent).toHaveTextContent("Codex");
  });

  it("uses only theme tokens for pane color", () => {
    render(<AgentTimeline provider="claude" conversation={{ session_handle: "h", activity: "working", turns }} />);
    for (const pane of Array.from(document.querySelectorAll("article"))) {
      expect(pane.getAttribute("style")).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(/i);
    }
  });

  it("renders message text at the reply field's size", () => {
    render(<AgentTimeline provider="claude" conversation={{ session_handle: "h", activity: "finished", turns }} />);
    const body = document.querySelector('[data-testid="markdown-text"]') as HTMLElement;
    expect(body.style.fontSize).toBe("13px");
    expect(body.style.lineHeight).toBe("1.55");
  });
});
