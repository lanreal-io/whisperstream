import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentResponseCompletion, HudSession } from "../../../lib/socket/agentEvents";
import { AgentConversationContext, AgentTimeline } from "../../../components/agentSpeak/AgentTimeline";
import { ResponsePanel } from "../views/ResponsePanel";
import {
  REPLY_RAIL_REFUSED_COPY,
  REPLY_RAIL_FIELD_PAD_L,
  REPLY_RAIL_SUBMIT_HINT,
  ReplyRail,
} from "./ReplyRail";

afterEach(cleanup);

const hint = () => screen.queryByTestId("claude-reply-hint");

describe("ReplyRail submit hint", () => {
  it("shows the hint for a spoken draft with auto-submit off", () => {
    render(<ReplyRail phase="draft" text="Merge it." submitHint reserveHint platform="windows" />);
    expect(hint()).toHaveTextContent(REPLY_RAIL_SUBMIT_HINT);
    expect(hint()).toHaveStyle({ visibility: "visible" });
    expect(screen.getByTestId("claude-reply-keycap")).toHaveAttribute("data-lit", "true");
  });

  it("never shows the hint for typed text, but keeps its row reserved", () => {
    render(<ReplyRail phase="editing" editingValue="typed" submitHint reserveHint platform="windows" />);
    expect(hint()).toHaveAttribute("data-state", "reserved");
    expect(hint()).toHaveStyle({ visibility: "hidden" });
    expect(screen.queryByText(REPLY_RAIL_SUBMIT_HINT)).toBeNull();
  });

  it("shows no hint and reserves nothing when auto-submit is on", () => {
    render(<ReplyRail phase="draft" text="Merge it." submitHint={false} reserveHint={false} />);
    expect(hint()).toBeNull();
  });

  it("uses the Return glyph on Mac and speaks the key's name", () => {
    render(<ReplyRail phase="draft" text="Merge it." submitHint reserveHint platform="macos" />);
    const row = hint() as HTMLElement;
    expect(row.querySelector('[aria-hidden="true"]')).toHaveTextContent("Press ↩ to submit");
    expect(row.querySelector(".sr-only")).toHaveTextContent("Press Return to submit");
    expect(screen.getByTestId("claude-reply-keycap")).toHaveTextContent("⏎");
  });

  it("puts a refusal ahead of the hint and keeps the draft", () => {
    render(<ReplyRail phase="editing" editingValue="keep me" submitHint reserveHint notice="refused" />);
    expect(hint()).toHaveTextContent(REPLY_RAIL_REFUSED_COPY);
    expect(hint()).toHaveAttribute("data-state", "refused");
    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveValue("keep me");
  });
});

describe("ReplyRail Copy", () => {
  it("is an icon-only button named Copy reply that confirms with Copied", () => {
    vi.useFakeTimers();
    try {
      const onCopy = vi.fn();
      render(<ReplyRail phase="editing" editingValue="keep me" notice="refused" onCopy={onCopy} />);
      const button = screen.getByRole("button", { name: "Copy reply" });
      expect(button).toHaveAttribute("type", "button");
      expect(button).toHaveAttribute("title", "Copy reply");
      expect(button).toHaveTextContent("");
      expect(button.querySelector("svg")).not.toBeNull();

      fireEvent.click(button);
      expect(onCopy).toHaveBeenCalledTimes(1);
      const confirmed = screen.getByRole("button", { name: "Copied" });
      expect(confirmed).toBe(button);
      expect(confirmed).toHaveAttribute("title", "Copied");
      expect(confirmed).toHaveAttribute("data-copied", "true");
      expect(hint()).toHaveTextContent("Copied");

      act(() => {
        vi.advanceTimersByTime(1500);
      });
      expect(screen.getByRole("button", { name: "Copy reply" })).toHaveAttribute("data-copied", "false");
    } finally {
      vi.useRealTimers();
    }
  });

  it("sits in the field beside the key cap, outside the notice line", () => {
    render(<ReplyRail phase="editing" editingValue="keep me" notice="not_delivered" onCopy={() => {}} />);
    const button = screen.getByTestId("claude-reply-copy");
    expect(screen.getByTestId("claude-reply-field")).toContainElement(button);
    expect(hint()).not.toContainElement(button);
    expect(button.nextElementSibling).toBe(screen.getByTestId("claude-reply-keycap"));
  });

  it("stays reachable when an ended session removes the key cap", () => {
    render(<ReplyRail phase="editing" editingValue="kept" notice="ended" onCopy={() => {}} />);
    expect(screen.queryByTestId("claude-reply-keycap")).toBeNull();
    expect(screen.getByRole("button", { name: "Copy reply" })).toBeInTheDocument();
  });

  it("is offered for a held reply in flight", () => {
    render(<ReplyRail phase="sending" text="Later please" notice="held" onCopy={() => {}} />);
    expect(screen.getByRole("button", { name: "Copy reply" })).toBeInTheDocument();
  });

  it("does not click through to editing the draft", () => {
    const onEditStart = vi.fn();
    render(<ReplyRail phase="draft" text="Merge it." notice="refused" onCopy={() => {}} onEditStart={onEditStart} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy reply" }));
    expect(onEditStart).not.toHaveBeenCalled();
  });

  it("is absent without a notice", () => {
    render(<ReplyRail phase="editing" editingValue="typed" onCopy={() => {}} />);
    expect(screen.queryByTestId("claude-reply-copy")).toBeNull();
  });
});

describe("ReplyRail key cap", () => {
  it("is dim while empty and lights once there is text", () => {
    const view = render(<ReplyRail phase="editing" editingValue="" />);
    const cap = screen.getByTestId("claude-reply-keycap");
    expect(cap).toHaveAttribute("data-lit", "false");
    expect(cap).toHaveAttribute("aria-hidden", "true");
    view.rerender(<ReplyRail phase="editing" editingValue="go" />);
    expect(screen.getByTestId("claude-reply-keycap")).toHaveAttribute("data-lit", "true");
  });

  it("is absent where nothing can be submitted", () => {
    render(<ReplyRail phase="resting" canReply={false} />);
    expect(screen.queryByTestId("claude-reply-keycap")).toBeNull();
    expect(screen.getByText("Reply in your terminal")).toBeInTheDocument();
  });

  it("rests as a faint hint line with no field box", () => {
    render(<ReplyRail phase="resting" />);
    const field = screen.getByTestId("claude-reply-field");
    // Most replies are spoken, so only typing draws the box (owner, 2026-10-24).
    expect(field).toHaveAttribute("data-framed", "false");
    expect(field).toHaveStyle({ minHeight: "16px" });
    // Lined up with the agent's text above it.
    expect(field.style.padding).toBe("0px");
    // No reply text yet, so nothing can shift: the placeholder keeps the inset.
    expect(field.firstElementChild).toBe(screen.getByTestId("claude-reply-rail-text"));
  });

  it("rests with only the countdown once the hint is retired", () => {
    render(<ReplyRail phase="resting" placeholder={null} countdownHint="0:06" canReply />);
    expect(screen.getByTestId("claude-reply-rail-text")).toHaveTextContent(/^$/);
    expect(screen.getByTestId("claude-reply-inline-state")).toHaveTextContent("0:06");
  });

  it("moves the queue depth into a retired hint's spot", () => {
    const { rerender } = render(<ReplyRail phase="resting" placeholder="Hold Right Shift" queueHint="2 in queue" countdownHint="0:06" canReply />);
    expect(screen.getByTestId("claude-reply-queue")).toHaveTextContent("2 in queue");
    rerender(<ReplyRail phase="resting" placeholder={null} queueHint="2 in queue" countdownHint="0:06" canReply />);
    expect(screen.queryByTestId("claude-reply-queue")).toBeNull();
    expect(screen.getByTestId("claude-reply-rail-text")).toHaveTextContent("2 in queue");
    expect(screen.getByTestId("claude-reply-inline-state")).toHaveTextContent("0:06");
  });

  it("reserves no empty submit-hint row while resting", () => {
    const { rerender } = render(<ReplyRail phase="resting" reserveHint />);
    // The row arrives with the draft it describes (owner, 2026-10-24).
    expect(screen.queryByTestId("claude-reply-hint")).toBeNull();
    rerender(<ReplyRail phase="live" text="Ship" reserveHint />);
    expect(screen.getByTestId("claude-reply-hint")).toHaveAttribute("data-state", "reserved");
  });

  it("draws a spoken draft as plain text that becomes the field on click", () => {
    const onEditStart = vi.fn();
    const { rerender } = render(
      <ReplyRail phase="draft" text="Ship it" onEditStart={onEditStart} />,
    );
    const field = screen.getByTestId("claude-reply-field");
    expect(field).toHaveAttribute("data-framed", "false");
    expect(field).toHaveAttribute("data-click-to-edit", "true");
    expect(field.style.background).toBe("transparent");
    // Same inset as the field, so the text does not move when editing starts.
    expect(field).toHaveStyle({ padding: "6px 6px 6px 12px" });
    fireEvent.click(screen.getByTestId("claude-reply-rail"));
    expect(onEditStart).toHaveBeenCalledTimes(1);

    rerender(<ReplyRail phase="editing" editingValue="Ship it" />);
    expect(screen.getByTestId("claude-reply-field")).toHaveAttribute("data-framed", "true");
  });

  it.each(["live", "processing", "sent"] as const)(
    "draws the spoken %s phase without the field frame",
    (phase) => {
      render(<ReplyRail phase={phase} text="Ship it" />);
      expect(screen.getByTestId("claude-reply-field")).toHaveAttribute("data-framed", "false");
    },
  );
});

describe("AgentTimeline working and reply status", () => {
  const turns = [
    { id: "a", role: "assistant" as const, text: "Done." },
    { id: "b", role: "user" as const, text: "Merge it." },
  ];

  it("closes a working timeline with the working row", () => {
    render(<AgentTimeline provider="claude" conversation={{ session_handle: "h", activity: "working", turns }} />);
    expect(screen.getByTestId("agent-working-row")).toHaveTextContent("Working");
  });

  it("animates the working bars in CSS so a parent initial={false} cannot freeze them", () => {
    render(<AgentTimeline provider="claude" conversation={{ session_handle: "h", activity: "working", turns }} />);
    const bars = screen.getByTestId("agent-working-row").querySelectorAll("[data-working-bar]");
    expect(bars).toHaveLength(4);
    for (const bar of bars) {
      expect((bar as HTMLElement).style.animation).toContain("agent-working-wave");
    }
  });

  it("drops the working row once the agent finishes or needs input", () => {
    const view = render(<AgentTimeline provider="claude" conversation={{ session_handle: "h", activity: "finished", turns }} />);
    expect(screen.queryByTestId("agent-working-row")).toBeNull();
    view.rerender(<AgentTimeline provider="claude" needsInput conversation={{ session_handle: "h", activity: "working", turns }} />);
    expect(screen.queryByTestId("agent-working-row")).toBeNull();
  });

  it("states a held reply and a sent reply under the newest user turn", () => {
    const view = render(<AgentTimeline provider="codex" replyStatus="held" conversation={{ session_handle: "h", activity: "working", turns }} />);
    const status = screen.getByTestId("agent-reply-status");
    expect(status).toHaveTextContent("Sends when Codex finishes");
    expect(status.closest("article")).toHaveAttribute("data-newest-turn", "true");
    view.rerender(<AgentTimeline provider="claude" replyStatus="sent" conversation={{ session_handle: "h", activity: "working", turns }} />);
    expect(screen.getByTestId("agent-reply-status")).toHaveTextContent("Sent. Waiting for Claude");
  });

  it("never puts a reply status under an agent turn", () => {
    render(<AgentTimeline provider="claude" replyStatus="sent" conversation={{ session_handle: "h", activity: "working", turns: [...turns].reverse() }} />);
    expect(screen.queryByTestId("agent-reply-status")).toBeNull();
  });
});

describe("Console wiring", () => {
  const completion: AgentResponseCompletion = {
    completion_id: "c1",
    sequence: 1,
    kind: "response",
    text: "Done.",
    agent: "claude",
    source_label: "Claude",
    source_name: "whisperstream",
    empty: false,
  };
  const session = (overrides: Partial<HudSession>): HudSession => ({
    session_handle: "s1",
    agent: "claude",
    source_name: "whisperstream",
    unread: false,
    needs_input: false,
    can_reply: false,
    accepts_reply: true,
    ...overrides,
  });

  function Console({
    sessionState,
    activity = "working" as const,
    autoSubmit = false,
    spokenDraft,
  }: {
    sessionState: HudSession;
    activity?: "working" | "finished";
    autoSubmit?: boolean;
    spokenDraft?: string;
  }) {
    return (
      <AgentConversationContext.Provider
        value={{ session_handle: "h", activity, turns: [{ id: "u", role: "user", text: "Merge it." }] }}
      >
        <ResponsePanel
          completion={completion}
          remainingCount={0}
          truncated={false}
          replyState="idle"
          rms={0}
          peak={0}
          speaking={false}
          expanded
          compactConsole
          canReply={sessionState.accepts_reply ?? sessionState.can_reply}
          sessions={[sessionState]}
          activeSessionHandle={sessionState.session_handle}
          replyAutoSubmit={autoSubmit}
          replyPhase={spokenDraft !== undefined ? "draft_ready" : "passive"}
          replyDraft={
            spokenDraft !== undefined
              ? { phase: "draft_ready", generation: 2, text: spokenDraft }
              : null
          }
        />
      </AgentConversationContext.Provider>
    );
  }

  it("keeps the field open while the agent works", () => {
    render(<Console sessionState={session({})} />);
    expect(screen.getByRole("button", { name: "Reply to agent" })).toBeInTheDocument();
    expect(screen.getByText("Speak or type")).toBeInTheDocument();
    expect(screen.getByTestId("agent-reply-status")).toHaveTextContent("Sent. Waiting for Claude");
  });

  it("says a held reply sends at the agent's next stop", () => {
    render(<Console sessionState={session({ reply_held: true })} />);
    expect(screen.getByTestId("agent-reply-status")).toHaveTextContent("Sends when Claude finishes");
  });

  it("hints a spoken draft only while auto-submit is off", () => {
    const draft = session({});
    const view = render(<Console sessionState={draft} activity="finished" spokenDraft="Ship it." />);
    expect(hint()).toHaveAttribute("data-state", "submit");
    view.rerender(<Console sessionState={draft} activity="finished" spokenDraft="Ship it." autoSubmit />);
    expect(hint()).toBeNull();
  });
});

describe("ReplyRail live dictation bars", () => {
  it("draws the rail-size bars in the trailing slot, where the key cap sits", () => {
    render(<ReplyRail phase="live" text="Let's go with staging" rms={0.5} peak={0.5} />);
    const bars = screen.getByTestId("reply-voice-bars");
    expect(bars).toHaveAttribute("data-voice-bars-size", "rail");
    expect(bars.children).toHaveLength(5);
    const slot = screen.getByTestId("claude-reply-rail-glyph");
    expect(slot).toHaveStyle({ width: "24px", height: "22px", alignSelf: "center" });
    expect(slot).toContainElement(bars);
    // Trailing, after the text.
    expect(screen.getByTestId("claude-reply-field").lastElementChild).toBe(slot);
    expect(screen.queryByTestId("reply-voice-coil")).toBeNull();
  });

  it.each(["live", "processing", "draft", "editing", "sending", "sent"] as const)(
    "starts the %s text at the rail's left edge, so it lines up with the agent text",
    (phase) => {
      render(<ReplyRail phase={phase} text="Ship it." rms={0.5} peak={0.5} canReply />);
      const field = screen.getByTestId("claude-reply-field");
      expect(field.firstElementChild).toBe(screen.getByTestId("claude-reply-rail-text"));
      // The field's inset bleeds outward by exactly its padding and border.
      expect(field.style.paddingLeft).toBe(`${REPLY_RAIL_FIELD_PAD_L}px`);
      expect(field.style.marginLeft).toBe(`-${REPLY_RAIL_FIELD_PAD_L + 1}px`);
    },
  );
});

describe("ReplyRail sent state", () => {
  it("puts the check beside the Sent label with no trailing indicator", () => {
    render(<ReplyRail phase="sent" text="Ship it." />);
    const group = screen.getByTestId("claude-reply-inline-state");
    expect(group).toHaveTextContent("Sent");
    expect(group).toContainElement(screen.getByTestId("claude-reply-sent-glyph"));
    expect(screen.queryByTestId("claude-reply-rail-glyph")).toBeNull();
  });

  it("still names the sent state once the session has ended", () => {
    render(<ReplyRail phase="sent" text="Ship it." notice="ended" />);
    expect(screen.getByTestId("claude-reply-inline-state")).toHaveTextContent("Sent");
  });

  it("keeps the spinner in the trailing slot while processing", () => {
    render(<ReplyRail phase="processing" text="Ship it" />);
    expect(screen.getByTestId("claude-reply-rail-glyph")).toHaveAttribute("data-phase", "processing");
    expect(screen.queryByTestId("claude-reply-keycap")).toBeNull();
    expect(screen.queryByTestId("claude-reply-sent-glyph")).toBeNull();
  });
});
