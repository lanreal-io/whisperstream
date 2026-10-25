import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentConversation } from "../../../lib/socket/agentEvents";
import { AgentConversationContext } from "../../../components/agentSpeak/AgentTimeline";
import { ClaudeExpandedCard } from "./ExpandedResponse";
import { HUD_DARK_CARD_ELEVATION, HUD_LIGHT_CARD_ELEVATION } from "../material";

// The well's scroll metrics and the newest turn's position are layout, which
// jsdom does not compute; these stand in for a 300px well over 1000px of turns.
const metrics = { scrollHeight: 1000, clientHeight: 300, newestTop: 900 };
const scrollTo = vi.fn();

function isWell(node: Element): boolean {
  return (node as HTMLElement).dataset?.testid === "claude-well";
}

beforeEach(() => {
  metrics.scrollHeight = 1000;
  metrics.clientHeight = 300;
  metrics.newestTop = 900;
  scrollTo.mockReset();
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get() {
      return isWell(this) ? metrics.scrollHeight : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get() {
      return isWell(this) ? metrics.clientHeight : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "scrollTo", {
    configurable: true,
    value: scrollTo,
  });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function (this: HTMLElement) {
      const top =
        this.dataset.newestTurn === "true"
          ? metrics.newestTop - this.closest<HTMLElement>('[data-testid="claude-well"]')!.scrollTop
          : 0;
      return { top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top, toJSON: () => ({}) };
    },
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function conversation(turns: number, handle = "h"): AgentConversation {
  return {
    session_handle: handle,
    activity: "finished",
    turns: Array.from({ length: turns }, (_unused, index) => ({
      id: `${handle}-${index}`,
      role: index % 2 === 0 ? ("user" as const) : ("assistant" as const),
      text: `Turn ${index}`,
    })),
  };
}

function Card({
  history,
  active = "one",
  summaryMode = false,
  remainingCount = 0,
}: {
  history: AgentConversation;
  active?: string;
  summaryMode?: boolean;
  remainingCount?: number;
}) {
  return (
    <AgentConversationContext.Provider value={history}>
      <ClaudeExpandedCard
        provider="claude"
        body="**Summary** body"
        sourceName="whisperstream"
        speaking={false}
        spineSegments={[1]}
        remainingCount={remainingCount}
        accessibleLabel="Claude"
        railPhase="resting"
        countdownHint={null}
        railProps={{}}
        activeSessionHandle={active}
        compactConsole
        summaryMode={summaryMode}
      />
    </AgentConversationContext.Provider>
  );
}

describe("ClaudeExpandedCard timeline scrolling", () => {
  it("opens on the newest turn", () => {
    render(<Card history={conversation(4)} />);
    // Newest turn fits: the timeline's bottom (scrollHeight - clientHeight).
    expect(screen.getByTestId("claude-well").scrollTop).toBe(700);
  });

  it("shows the top of a newest turn taller than the well", () => {
    metrics.newestTop = 400;
    render(<Card history={conversation(4)} />);
    // 400 minus the 6px breathing room above the turn.
    expect(screen.getByTestId("claude-well").scrollTop).toBe(394);
  });

  it("lands on the newest turn immediately on a tab switch", () => {
    const view = render(<Card history={conversation(4)} />);
    const well = screen.getByTestId("claude-well");
    well.scrollTop = 0;
    metrics.scrollHeight = 800;
    view.rerender(<Card history={conversation(2, "other")} active="two" />);
    expect(well.scrollTop).toBe(500);
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("glides to a new turn on the same tab", () => {
    const view = render(<Card history={conversation(4)} />);
    metrics.scrollHeight = 1200;
    view.rerender(<Card history={conversation(5)} />);
    expect(scrollTo).toHaveBeenCalledWith({ top: 894, behavior: "smooth" });
  });

  it("stays pinned through its own late scroll event, and lets the user scroll away", () => {
    const history = conversation(4);
    const view = render(<Card history={history} />);
    const well = screen.getByTestId("claude-well");
    expect(well.scrollTop).toBe(700);

    // The scroll event for our own move arrives after the content grew.
    metrics.scrollHeight = 1100;
    metrics.newestTop = 1000;
    act(() => {
      fireEvent.scroll(well);
    });
    view.rerender(<Card history={{ ...history }} />);
    expect(well.scrollTop).toBe(800);

    // The user scrolls up to read: growth no longer pulls the well down.
    act(() => {
      well.scrollTop = 100;
      fireEvent.scroll(well);
    });
    metrics.scrollHeight = 1200;
    view.rerender(<Card history={{ ...history }} />);
    expect(well.scrollTop).toBe(100);
  });

  it("keeps a single response body at its start and renders its Markdown", () => {
    render(<Card history={conversation(4)} summaryMode />);
    expect(screen.getByTestId("claude-well").scrollTop).toBe(0);
    expect(screen.getByText("Summary").tagName).toBe("STRONG");
    expect(screen.queryByTestId("agent-timeline")).toBeNull();
  });

  it("puts the glance card's queue depth on the resting line, the console's in a footer", () => {
    const { unmount } = render(<Card history={conversation(4)} summaryMode remainingCount={1} />);
    expect(screen.getByTestId("claude-reply-queue")).toHaveTextContent("1 in queue");
    expect(screen.getAllByText("1 in queue")).toHaveLength(1);
    unmount();
    render(<Card history={conversation(4)} remainingCount={1} />);
    expect(screen.queryByTestId("claude-reply-queue")).toBeNull();
    expect(screen.getByText("1 in queue")).toBeInTheDocument();
  });

  it("keeps the divider in the console but not in the glance card", () => {
    const { unmount } = render(<Card history={conversation(4)} summaryMode />);
    expect(screen.queryByTestId("claude-card-divider")).toBeNull();
    unmount();
    render(<Card history={conversation(4)} />);
    expect(screen.getByTestId("claude-card-divider")).toBeInTheDocument();
  });

  it("fades the top edge only once scrolled away from the start", () => {
    render(<Card history={conversation(4)} summaryMode />);
    const fade = screen.getByTestId("claude-well-top-fade");
    expect(fade).toHaveAttribute("data-visible", "false");
    const well = screen.getByTestId("claude-well");
    act(() => {
      well.scrollTop = 40;
      fireEvent.scroll(well);
    });
    expect(fade).toHaveAttribute("data-visible", "true");
    expect(screen.getByTestId("claude-well-fade")).toHaveAttribute(
      "data-visible",
      "true",
    );
  });

  it("drops the spine gutter in timeline mode", () => {
    render(<Card history={conversation(2)} />);
    const well = screen.getByTestId("claude-well");
    expect(screen.getByTestId("claude-tab-body").style.paddingLeft).toBe("0px");
    expect(well.style.paddingRight).toBe("6px");
  });
});

describe("ClaudeExpandedCard console body", () => {
  it("keeps a single reply's spine to its text, not the console's taller well", () => {
    render(
      <ClaudeExpandedCard
        provider="claude"
        body="One short reply."
        sourceName="whisperstream"
        speaking={false}
        spineSegments={[1]}
        remainingCount={0}
        accessibleLabel="Claude"
        railPhase="resting"
        countdownHint={null}
        railProps={{}}
      />,
    );
    const body = screen.getByTestId("claude-tab-body");
    expect(body).toHaveClass("relative");
    expect(body).toContainElement(screen.getByTestId("claude-spine"));
    expect(screen.getByTestId("claude-expanded-card")).toHaveAttribute("data-console", "true");
  });

  it("keeps the spine's 12px gap to the text inside its containing block", () => {
    render(
      <ClaudeExpandedCard
        provider="claude"
        body="One short reply."
        sourceName="whisperstream"
        speaking={false}
        spineSegments={[1]}
        remainingCount={0}
        accessibleLabel="Claude"
        railPhase="resting"
        countdownHint={null}
        railProps={{}}
      />,
    );
    // The spine sits at the body's left edge and the text starts after the
    // 2px rule plus the 12px gap, so the inset belongs to the same box.
    expect(screen.getByTestId("claude-tab-body").style.paddingLeft).toBe("14px");
    expect(screen.getByTestId("claude-well").style.paddingLeft).toBe("");
  });
});

describe("ClaudeExpandedCard glance summary", () => {
  it("draws no spine and starts the summary at the mark's left edge", () => {
    render(
      <ClaudeExpandedCard
        provider="claude"
        body="The feature is ready."
        sourceName="whisperstream"
        speaking={false}
        spineSegments={[1]}
        remainingCount={0}
        accessibleLabel="Claude"
        railPhase="resting"
        countdownHint={null}
        railProps={{}}
        summaryMode
      />,
    );
    expect(screen.queryByTestId("claude-spine")).toBeNull();
    expect(screen.getByTestId("claude-tab-body").style.paddingLeft).toBe("0px");
  });
});

describe("ClaudeExpandedCard material", () => {
  it("uses the card elevation, which has no top specular line", () => {
    render(
      <ClaudeExpandedCard
        provider="claude"
        body="The feature is ready."
        sourceName="whisperstream"
        speaking={false}
        spineSegments={[1]}
        remainingCount={0}
        accessibleLabel="Claude"
        railPhase="resting"
        countdownHint={null}
        railProps={{}}
        summaryMode
      />,
    );
    expect(screen.getByTestId("claude-expanded-card").style.boxShadow).toBe(
      "var(--hud-card-elevation)",
    );
    for (const elevation of [HUD_LIGHT_CARD_ELEVATION, HUD_DARK_CARD_ELEVATION]) {
      expect(elevation).not.toMatch(/inset 0 1px 0/);
      expect(elevation).toMatch(/inset 0 -1px 0/);
    }
  });
});
