// Round-03 component-level coverage for the Claude interaction surfaces
// (ResponsePanel, QuestionPanel, currentClaudeAttention). These render the
// components directly and deliberately do NOT mock the socket or the Tauri
// window, so they stay separate from HUD.test.tsx, which drives the whole HUD
// through the event pipeline with fake timers and a mocked window.
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { AgentActiveQuestion, AgentCompletion } from "../lib/socket/agentEvents";
import { CLAUDE_PILL_MAX_W, CLAUDE_PILL_MIN_W, CLAUDE_RAIL_PILL_W, HUD_REGION_EDGE_BLEED, PANEL_WIDTH } from "./hud/geometry";
import { ClaudeIdentityRow } from "./hud/components/AgentIdentity";
import { ClaudePermissionPill, QuestionPanel } from "./hud/views/QuestionPanel";
import { measureCompactPillWidth } from "./hud/views/CompactResponse";
import { ResponsePanel } from "./hud/views/ResponsePanel";
import { compactQuestionPillProps, currentClaudeAttention } from "./hud/presentation";

const originalDocumentFonts = Object.getOwnPropertyDescriptor(document, "fonts");

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  if (originalDocumentFonts === undefined) {
    Reflect.deleteProperty(document, "fonts");
  } else {
    Object.defineProperty(document, "fonts", originalDocumentFonts);
  }
});

function measuredRect(width: number): DOMRect {
  return {
    x: 0,
    y: 0,
    width,
    height: 11,
    top: 0,
    right: width,
    bottom: 11,
    left: 0,
    toJSON: () => ({}),
  } as DOMRect;
}

const completion: AgentCompletion = {
  completion_id: "completion-1",
  sequence: 1,
  kind: "response",
  text: "Implemented the requested feature.\nAll checks are passing.",
  agent: "claude",
  source_label: "Claude session",
  source_name: "speak-harness",
  empty: false,
  spoken_summary: "The feature is ready.",
};

const baseProps = {
  completion,
  remainingCount: 1,
  truncated: false,
  replyState: "idle" as const,
  rms: 0,
  peak: 0,
  speaking: false,
};

describe("compact pill resting reply hint", () => {
  const shortcuts = { pushToTalk: "right shift", toggle: "", lastOrigin: null };

  it("shows the faint hint at rest, yields to the stop button, and a click starts typing", async () => {
    const onDraftEdit = vi.fn();
    const props = { ...baseProps, remainingCount: 0, canReply: true, replyShortcuts: shortcuts, onDraftEdit };
    const { rerender } = render(<ResponsePanel {...props} />);
    const hint = screen.getByTestId("claude-compact-reply-hint");
    expect(hint).toHaveTextContent("Hold Right Shift to reply");
    expect(hint).toHaveStyle({ opacity: "0.7" });
    fireEvent.click(screen.getByTestId("compact-claude-pill"));
    expect(onDraftEdit).toHaveBeenCalledOnce();
    rerender(<ResponsePanel {...props} speaking />);
    // It fades out with its height, so the name glides back to center.
    await waitFor(() => expect(screen.queryByTestId("claude-compact-reply-hint")).toBeNull());
  });

  it("shows the countdown at rest and keeps it after the hint retires", () => {
    const replyWindow = { remainingSeconds: 6, progress: 0.5 } as never;
    const props = { ...baseProps, remainingCount: 0, canReply: true, replyShortcuts: shortcuts, replyWindow };
    const { rerender } = render(<ResponsePanel {...props} />);
    expect(screen.getByTestId("claude-compact-reply-time")).toHaveTextContent("0:06");
    expect(screen.getByTestId("claude-compact-reply-hint")).toBeInTheDocument();
    rerender(<ResponsePanel {...props} replyHintRetired />);
    expect(screen.getByTestId("claude-compact-reply-time")).toHaveTextContent("0:06");
    return waitFor(() =>
      expect(screen.queryByTestId("claude-compact-reply-hint")).toBeNull(),
    );
  });

  it("puts the queue depth in the caption, in place of the hint, even while speaking", () => {
    const props = { ...baseProps, remainingCount: 2, canReply: true, replyShortcuts: shortcuts };
    const { rerender } = render(<ResponsePanel {...props} />);
    expect(screen.getByTestId("claude-compact-queue")).toHaveTextContent("2 in queue");
    expect(screen.queryByTestId("claude-compact-reply-hint")).toBeNull();
    rerender(<ResponsePanel {...props} speaking />);
    expect(screen.getByTestId("claude-compact-queue")).toHaveTextContent("2 in queue");
    // The shared meta size, not the hint's smaller caption.
    expect(screen.getByTestId("claude-compact-queue")).toHaveStyle({ fontSize: "10.5px" });
  });

  it("shows no hint when the session cannot take a reply or shortcuts are unknown", () => {
    const { rerender } = render(<ResponsePanel {...baseProps} remainingCount={0} canReply={false} replyShortcuts={shortcuts} />);
    expect(screen.queryByTestId("claude-compact-reply-hint")).toBeNull();
    rerender(<ResponsePanel {...baseProps} remainingCount={0} canReply />);
    expect(screen.queryByTestId("claude-compact-reply-hint")).toBeNull();
  });
});

describe("compact response pill measurement", () => {
  it("ceil-rounds Inter metrics and includes the outer border before even snapping", () => {
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      measuredRect(78.25),
    );

    expect(measureCompactPillWidth("whisperstream")).toBe(200);
  });

  it("remeasures the compact footprint after the bundled font is ready", async () => {
    let resolveFonts!: () => void;
    const ready = new Promise<void>((resolve) => {
      resolveFonts = resolve;
    });
    Object.defineProperty(document, "fonts", {
      configurable: true,
      value: { ready },
    });
    let measuredWidth = 78.25;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      () => measuredRect(measuredWidth),
    );

    render(
      <ResponsePanel
        {...baseProps}
        completion={{ ...completion, source_name: "whisperstream" }}
      />,
    );
    await waitFor(() =>
      expect(screen.getByTestId("compact-claude-pill")).toHaveStyle({
        width: "200px",
      }),
    );

    measuredWidth = 90.25;
    await act(async () => {
      resolveFonts();
      await ready;
    });

    await waitFor(() =>
      expect(screen.getByTestId("compact-claude-pill")).toHaveStyle({
        width: "212px",
      }),
    );
  });
});

describe("response HUD expansion", () => {
  // Round-05 (hud-presentation) rebuilt this surface: the `Claude response` /
  // `Your reply` eyebrows, the `Listening` / `Processing` word labels, and
  // the compact `Double-click to view full response` hint are all gone
  // The spine plus the 14px-vs-13px split
  // now carry attribution instead.

  it("shows the full response with a live reply rail beneath it, no eyebrows", () => {
    render(
      <ResponsePanel
        {...baseProps}
        expanded
        replyPhase="recording"
        replyPreview="I am reviewing the change"
      />,
    );

    expect(screen.queryByText("Claude response")).not.toBeInTheDocument();
    expect(screen.queryByText("Your reply")).not.toBeInTheDocument();
    expect(screen.queryByText("Listening")).not.toBeInTheDocument();
    expect(
      screen.getByText("Implemented the requested feature.", {
        exact: false,
      }),
    ).toBeInTheDocument();
    // The live rail text reuses ReconciledPreviewText, which reveals the
    // string as one span per grapheme (decision 24), so it is not a single
    // text node getByText can match directly.
    expect(screen.getByTestId("claude-reply-rail")).toHaveTextContent(
      "I am reviewing the change",
    );
    expect(screen.getByText("1 in queue")).toBeInTheDocument();
  });

  it("shows processing without replacing the Claude response, and no word label", () => {
    render(
      <ResponsePanel
        {...baseProps}
        expanded
        replyPhase="processing"
        replyPreview="Frozen interim reply"
      />,
    );

    expect(
      screen.getByTestId("dictation-processing-spinner"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Processing")).not.toBeInTheDocument();
    expect(screen.getByText("Frozen interim reply")).toBeInTheDocument();
    expect(
      screen.getByText("Implemented the requested feature.", {
        exact: false,
      }),
    ).toBeInTheDocument();
  });

  it("offers draft editing without persistent keyboard instructions", () => {
    const onDraftEdit = vi.fn();
    render(
      <ResponsePanel
        {...baseProps}
        expanded
        replyDraft={{
          phase: "draft_ready",
          generation: 3,
          text: "Please ship it.",
        }}
        onDraftEdit={onDraftEdit}
      />,
    );

    const rail = screen.getByTestId("claude-reply-rail");
    expect(rail).toHaveTextContent("Please ship it.");
    fireEvent.click(rail);
    expect(onDraftEdit).toHaveBeenCalledOnce();
    expect(screen.queryByText("Click to edit")).not.toBeInTheDocument();
    expect(screen.queryByText("Enter to send")).not.toBeInTheDocument();
    expect(screen.queryByText("Press Enter to send")).not.toBeInTheDocument();
  });

  it("shows the resting compact pill with no response body text", () => {
    render(<ResponsePanel {...baseProps} />);

    // Decision 19: identity only (mark, session name, optional queue chip).
    // Decision 35: the in-pill wayfinding hint is cut too.
    expect(
      screen.queryByText("Double-click to view full response"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Implemented the requested feature.", {
        exact: false,
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("All checks are passing.", { exact: false }),
    ).not.toBeInTheDocument();
    // hud-session-name: the compact pill shows the resolved session name
    // alone, not the pre-joined `source_label`.
    expect(screen.getByText("speak-harness")).toBeInTheDocument();
  });

  it("shows the reply rail only for live, draft, and sent, never at rest", () => {
    const { rerender } = render(<ResponsePanel {...baseProps} />);
    expect(screen.queryByTestId("claude-reply-rail")).not.toBeInTheDocument();

    rerender(
      <ResponsePanel
        {...baseProps}
        replyPhase="recording"
        replyPreview="go ahead"
      />,
    );
    expect(screen.getByTestId("claude-reply-rail")).toBeInTheDocument();

    rerender(
      <ResponsePanel
        {...baseProps}
        replyDraft={{
          phase: "draft_ready",
          generation: 1,
          text: "Ship it.",
        }}
      />,
    );
    expect(screen.getByTestId("claude-reply-rail")).toBeInTheDocument();

    rerender(
      <ResponsePanel
        {...baseProps}
        sentReply={{
          completionId: completion.completion_id,
          generation: 1,
          text: "Ship it.",
        }}
      />,
    );
    expect(screen.getByTestId("claude-reply-rail")).toBeInTheDocument();
  });

  it("shows the sent confirmation the same way regardless of auto-submit mode", () => {
    // sentReply is set by the caller from the reply owner's receipt
    // (design.md state 6): with auto-submit on, it follows processing with no
    // draft_ready pause in between. The component itself
    // does not know which submit mode produced it, and does not need to.
    render(
      <ResponsePanel
        {...baseProps}
        sentReply={{
          completionId: completion.completion_id,
          generation: 1,
          text: "Ship it.",
        }}
      />,
    );

    const rail = screen.getByTestId("claude-reply-rail");
    expect(rail).toHaveTextContent("Ship it.");
    expect(screen.getByText("Sent")).toBeInTheDocument();
  });

  it("renders the full expanded response with no ReadingWell and no clamp", () => {
    render(<ResponsePanel {...baseProps} expanded />);

    expect(
      screen.getByText("Implemented the requested feature.", {
        exact: false,
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("All checks are passing.", { exact: false }),
    ).toBeInTheDocument();
    // ReadingWell's marker is its inner scroll node: a <div role="region">.
    // (The card's own <section aria-label="..."> also computes an implicit
    // ARIA region role, so queryByRole("region") is not a safe check here.)
    // The expanded card deliberately does not reuse ReadingWell as a
    // component (design.md state 7): the spine has to live inside the
    // well's scrolled content box, which ReadingWell has no slot for.
    expect(document.querySelector('div[role="region"]')).toBeNull();
    const body = screen.getByText("Implemented the requested feature.", {
      exact: false,
    });
    expect(body.style.getPropertyValue("-webkit-line-clamp")).toBe("");
  });

  it("renders the reply rail after the body in DOM order, always alongside it", () => {
    render(<ResponsePanel {...baseProps} expanded />);

    const card = screen.getByTestId("claude-expanded-card");
    const body = screen.getByText("Implemented the requested feature.", {
      exact: false,
    });
    const rail = screen.getByTestId("claude-reply-rail");
    expect(card).toContainElement(body);
    expect(card).toContainElement(rail);
    expect(
      body.compareDocumentPosition(rail) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("marks Claude's own words with the spine, and only those", () => {
    const { unmount } = render(<ResponsePanel {...baseProps} expanded />);
    const spine = screen.getByTestId("claude-spine");
    expect(spine).toBeInTheDocument();
    expect(
      screen.getByTestId("claude-reply-rail").querySelector(
        '[data-testid="claude-spine"]',
      ),
    ).toBeNull();
    unmount();

    render(<ResponsePanel {...baseProps} />);
    expect(screen.queryByTestId("claude-spine")).not.toBeInTheDocument();
    expect(screen.queryByTestId("claude-reply-rail")).not.toBeInTheDocument();
  });

  it("renders the identity row as the literal word Claude, no CLAUDE SESSION, no status dot", () => {
    render(<ResponsePanel {...baseProps} expanded />);

    expect(screen.getByText("Claude")).toBeInTheDocument();
    expect(screen.queryByText(/CLAUDE SESSION/i)).not.toBeInTheDocument();
    expect(
      document.querySelector('[data-testid="claude-status-dot"]'),
    ).toBeNull();
  });

  it("uses exactly one reply rail on the expanded response", () => {
    const { container } = render(<ResponsePanel {...baseProps} expanded />);
    expect(
      container.querySelectorAll('[data-testid="claude-reply-rail"]'),
    ).toHaveLength(1);
  });
});

describe("natural-height summary card mode", () => {
  it("renders the natural-height summary card when showSummaryText is on with a spoken summary", () => {
    render(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="The feature is ready."
        replyWindow={{ state: "available", remainingSeconds: 20, progress: 1 }}
      />,
    );

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(screen.queryByTestId("compact-claude-pill")).not.toBeInTheDocument();
    expect(screen.getByText("The feature is ready.")).toBeInTheDocument();
    expect(
      screen.queryByText("Implemented the requested feature.", {
        exact: false,
      }),
    ).not.toBeInTheDocument();
    // The glance card's summary carries no spine.
    expect(screen.queryByTestId("claude-spine")).not.toBeInTheDocument();
    expect(
      screen.getByTestId("claude-expanded-card"),
    ).toHaveAttribute(
      "aria-label",
      expect.stringMatching(/^Claude.+Summary\. The feature is ready\..+Reply for 0:20/),
    );
  });

  it("falls back to compact pill and announces no summary when summary is missing or blank", () => {
    const { rerender } = render(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary={null}
        replyWindow={{ state: "available", remainingSeconds: 6, progress: 1 }}
      />,
    );

    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    expect(screen.queryByTestId("claude-expanded-card")).not.toBeInTheDocument();
    expect(
      screen.getByTestId("compact-claude-pill"),
    ).toHaveAttribute(
      "aria-label",
      expect.stringContaining("No summary for this reply."),
    );

    rerender(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="   "
        replyWindow={{ state: "available", remainingSeconds: 6, progress: 1 }}
      />,
    );

    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    expect(screen.queryByTestId("claude-expanded-card")).not.toBeInTheDocument();
    expect(
      screen.getByTestId("compact-claude-pill"),
    ).toHaveAttribute(
      "aria-label",
      expect.stringContaining("No summary for this reply."),
    );
  });

  it("opens typing from a click on the summary text, only while the rail would", () => {
    const onDraftEdit = vi.fn();
    const props = {
      ...baseProps,
      showSummaryText: true,
      spokenSummary: "The feature is ready.",
      canReply: true,
      onDraftEdit,
    };
    const { rerender } = render(<ResponsePanel {...props} />);
    const body = screen.getByTestId("claude-card-body");
    // Not a drag handle: only the identity row moves the card.
    expect(body).toHaveAttribute("data-hud-interactive", "true");
    // One area from the text down through the reply line, gaps included.
    expect(body).toContainElement(screen.getByTestId("claude-reply-rail"));
    fireEvent.click(screen.getByText("The feature is ready."));
    expect(onDraftEdit).toHaveBeenCalledOnce();

    rerender(<ResponsePanel {...props} canReply={false} />);
    expect(screen.getByTestId("claude-card-body").style.cursor).toBe("default");
    fireEvent.click(screen.getByText("The feature is ready."));
    expect(onDraftEdit).toHaveBeenCalledOnce();
  });

  it("remains compact when showSummaryText is false even if spoken summary exists", () => {
    render(
      <ResponsePanel
        {...baseProps}
        showSummaryText={false}
        spokenSummary="The feature is ready."
      />,
    );

    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();
    expect(screen.queryByTestId("claude-expanded-card")).not.toBeInTheDocument();
  });

  it("supports text-only and text+speech modes without gating summary visibility", () => {
    const { rerender } = render(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="The feature is ready."
        speaking={false}
      />,
    );

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(screen.getByText("The feature is ready.")).toBeInTheDocument();
    expect(screen.queryByText("[x]")).not.toBeInTheDocument();

    rerender(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="The feature is ready."
        speaking={true}
      />,
    );

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(screen.getByText("The feature is ready.")).toBeInTheDocument();
    // Escape and replying stop speech; the card has no stop button.
    expect(screen.queryByRole("button", { name: "Stop speaking" })).toBeNull();
  });

  it("reuses ReplyRail below the summary across live, processing, draft, and sent states", () => {
    const onDraftEdit = vi.fn();
    const { rerender } = render(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="The feature is ready."
        replyPhase="recording"
        replyPreview="Running verification tests"
      />,
    );

    expect(screen.getByTestId("claude-reply-rail")).toHaveTextContent(
      "Running verification tests",
    );

    rerender(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="The feature is ready."
        replyPhase="processing"
        replyPreview="Running verification tests"
      />,
    );

    expect(
      screen.getByTestId("dictation-processing-spinner"),
    ).toBeInTheDocument();

    rerender(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="The feature is ready."
        replyDraft={{
          phase: "draft_ready",
          generation: 1,
          text: "Deploy to staging",
        }}
        onDraftEdit={onDraftEdit}
      />,
    );

    const draftRail = screen.getByTestId("claude-reply-rail");
    expect(draftRail).toHaveTextContent("Deploy to staging");
    fireEvent.click(draftRail);
    expect(onDraftEdit).toHaveBeenCalledOnce();

    rerender(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="The feature is ready."
        sentReply={{
          completionId: completion.completion_id,
          generation: 1,
          text: "Deploy to staging",
        }}
      />,
    );

    expect(screen.getByTestId("claude-reply-rail")).toHaveTextContent(
      "Deploy to staging",
    );
    expect(screen.getByText("Sent")).toBeInTheDocument();
  });

  it("switches to full stripped response body when explicitly expanded", () => {
    render(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="The feature is ready."
        expanded
      />,
    );

    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(
      screen.getByText("Implemented the requested feature.", { exact: false }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("All checks are passing.", { exact: false }),
    ).toBeInTheDocument();
  });

  it("honors explicit renderingIntent prop override", () => {
    const { rerender } = render(
      <ResponsePanel
        {...baseProps}
        showSummaryText
        spokenSummary="The feature is ready."
        renderingIntent="compact"
      />,
    );
    expect(screen.getByTestId("compact-claude-pill")).toBeInTheDocument();

    rerender(
      <ResponsePanel
        {...baseProps}
        showSummaryText={false}
        spokenSummary="The feature is ready."
        renderingIntent="summary"
      />,
    );
    expect(screen.getByTestId("claude-expanded-card")).toBeInTheDocument();
    expect(screen.getByText("The feature is ready.")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Iteration 03: minimal question card
// ---------------------------------------------------------------------------

const activeQuestion: AgentActiveQuestion = {
  session_handle: "handle-1",
  generation: 7,
  phase: "grace",
  questions: [
    {
      question: "Which environment should I deploy to?",
      header: "Deploy target",
      multiSelect: false,
      options: [
        { label: "Staging", description: "Use the internal test environment" },
        { label: "Production", description: "Publish to customers" },
      ],
    },
  ],
  current_index: 0,
  request_id: "request-1",
  routable: true,
  capture_available: true,
  remaining_seconds: 8,
  answers: {},
};

const questionProps = {
  active: activeQuestion,
  questionIndex: 0,
  selected: {},
  freeform: {},
  interimText: null,
  replyPhase: "idle" as const,
  onOption: vi.fn(),
  onFreeform: vi.fn(),
  onSubmit: vi.fn(),
  inputRef: { current: null },
};

describe("minimal question card", () => {
  it("renders the question and its options with no hint clutter", () => {
    render(<QuestionPanel {...questionProps} />);

    expect(
      screen.getByText("Which environment should I deploy to?"),
    ).toBeInTheDocument();
    expect(screen.getByText("Staging")).toBeInTheDocument();

    // Everything the owner called "far too many hints and things" is gone.
    for (const removed of [
      "Claude asked",
      "Choose all that apply",
      "Type here",
      "Click to use keyboard controls",
      "Esc to answer in Claude Code",
      "Press Enter to send",
    ]) {
      expect(screen.queryByText(removed)).not.toBeInTheDocument();
    }
    // Exactly one escape hint survives.
    expect(screen.getByText("Esc to cancel")).toBeInTheDocument();
  });

  it("shows the Claude identity header", () => {
    render(<QuestionPanel {...questionProps} />);
    expect(screen.getByText("Claude")).toBeInTheDocument();
  });

  it("hides the batch counter for a single question", () => {
    render(<QuestionPanel {...questionProps} />);
    expect(screen.queryByText("1 of 1")).not.toBeInTheDocument();
  });

  // Round-05 (hud-presentation) rebuilt the question card's footer to just
  // the action state and "Esc to cancel" (design.md state 9); the batch
  // counter has no home on the new card and is not rendered for any batch
  // size. Re-homed from "shows the batch counter only when the batch has
  // more than one" to document the cut instead of asserting dead text.
  it("shows no batch counter on the rebuilt footer even with a multi-question batch", () => {
    render(
      <QuestionPanel
        {...questionProps}
        active={{
          ...activeQuestion,
          questions: [activeQuestion.questions[0], activeQuestion.questions[0]],
        }}
      />,
    );
    expect(screen.queryByText("1 of 2")).not.toBeInTheDocument();
    expect(screen.queryByText(/^\d+ of \d+$/)).not.toBeInTheDocument();
  });

  it("submits an option on the very first click, with no arming step", () => {
    const onOption = vi.fn();
    render(<QuestionPanel {...questionProps} onOption={onOption} />);

    fireEvent.click(screen.getByText("Staging"));

    expect(onOption).toHaveBeenCalledTimes(1);
    expect(onOption.mock.calls[0][1]).toBe("Staging");
  });

  // Re-homed from "shows live speech in the reply field while listening":
  // the question card now uses the shared ReplyRail (decision 23), whose
  // live phase renders read-only ReconciledPreviewText, not an editable
  // field with a value (design.md Reference Code D). There is no textarea
  // and no placeholder during live speech; it is genuinely read-only.
  it("shows live speech read-only in the shared reply rail while listening", () => {
    render(
      <QuestionPanel
        {...questionProps}
        replyPhase="recording"
        interimText="deploy to staging please"
      />,
    );

    const rail = screen.getByTestId("claude-reply-rail");
    expect(rail).toHaveTextContent("deploy to staging please");
    expect(rail.querySelector("textarea")).toBeNull();
    expect(screen.getByTestId("agent-question-action")).toHaveTextContent(
      "Listening",
    );
    const bars = screen.getByTestId("reply-voice-bars");
    expect(bars).toHaveAttribute("data-voice-bars-size", "rail");
    expect(bars.children).toHaveLength(5);
    expect(bars).toHaveStyle({ height: "12px", gap: "2px" });
  });

  it("focuses the freeform textarea when the expanded question mounts", () => {
    render(<QuestionPanel {...questionProps} />);

    expect(screen.getByRole("textbox", { name: "Edit your reply" })).toHaveFocus();
  });

  it("uses a plain digit in the focused textarea to select its numbered option", () => {
    const onOption = vi.fn();
    const onFreeform = vi.fn();
    render(
      <QuestionPanel
        {...questionProps}
        onOption={onOption}
        onFreeform={onFreeform}
      />,
    );
    const input = screen.getByRole("textbox", { name: "Edit your reply" });

    fireEvent.keyDown(input, { key: "2" });

    expect(onOption).toHaveBeenCalledWith(
      activeQuestion.questions[0],
      "Production",
    );
    expect(onFreeform).not.toHaveBeenCalled();
    expect(input).toHaveValue("");
  });

  it("leaves composing Enter and digits to the IME, then submits confirmed Enter", () => {
    const onSubmit = vi.fn();
    const onOption = vi.fn();
    render(<QuestionPanel {...questionProps} onSubmit={onSubmit} onOption={onOption} />);
    const input = screen.getByRole("textbox", { name: "Edit your reply" });

    fireEvent.keyDown(input, { key: "Enter", isComposing: true });
    fireEvent.keyDown(input, { key: "2", isComposing: true });
    expect(onSubmit).not.toHaveBeenCalled();
    expect(onOption).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("keeps ordinary typing in the focused question freeform", () => {
    const onFreeform = vi.fn();
    render(<QuestionPanel {...questionProps} onFreeform={onFreeform} />);
    const input = screen.getByRole("textbox", { name: "Edit your reply" });

    fireEvent.change(input, { target: { value: "Use the canary group" } });

    expect(onFreeform).toHaveBeenCalledWith(
      "Which environment should I deploy to?",
      "Use the canary group",
    );
  });

  it("marks the question text with the spine", () => {
    render(<QuestionPanel {...questionProps} />);
    expect(screen.getByTestId("claude-spine")).toBeInTheDocument();
  });

  it("uses exactly one reply rail on the question card, the same component as the response", () => {
    const { container } = render(<QuestionPanel {...questionProps} />);
    expect(
      container.querySelectorAll('[data-testid="claude-reply-rail"]'),
    ).toHaveLength(1);
  });

  it("keeps keyboard instructions out of both the rail and footer", () => {
    render(
      <QuestionPanel
        {...questionProps}
        freeform={{ "Which environment should I deploy to?": "staging" }}
      />,
    );

    expect(screen.getByTestId("agent-question-action")).toHaveTextContent("");
    expect(
      screen.queryByText("Enter to send. Shift+Enter for a new line."),
    ).not.toBeInTheDocument();
  });
});

describe("Claude activity has no HUD presentation", () => {
  it("returns no attention state when only activity is happening", () => {
    expect(currentClaudeAttention([])).toBeNull();
  });

  it("still surfaces a pending permission", () => {
    const attention = currentClaudeAttention([
      {
        request_id: "req-1",
        kind: "permission",
        status: "pending",
        sequence: 1,
        remaining_seconds: 12,
      } as never,
    ]);
    expect(attention?.state).toBe("permission");
  });

  it("carries the resolved source_name for the permission pill (hud-session-name Task 3.6)", () => {
    const attention = currentClaudeAttention([
      {
        request_id: "req-1",
        kind: "permission",
        status: "pending",
        sequence: 1,
        remaining_seconds: 12,
        source_name: "pay kit",
      } as never,
    ]);
    expect(attention?.sourceName).toBe("pay kit");
  });

  it("sourceName is null when the interaction resolved none", () => {
    const attention = currentClaudeAttention([
      {
        request_id: "req-1",
        kind: "permission",
        status: "pending",
        sequence: 1,
        remaining_seconds: 12,
      } as never,
    ]);
    expect(attention?.sourceName).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// hud-session-name Task 3.1: ClaudeIdentityRow reworked to a `sourceName`
// prop; the old session-count gate on the card suffix is removed entirely
// (design decisions 4, 5, 7).
// ---------------------------------------------------------------------------

describe("ClaudeIdentityRow (hud-session-name)", () => {
  it("compact variant renders the resolved name alone", () => {
    render(<ClaudeIdentityRow variant="compact" sourceName="speak-harness" />);
    expect(screen.getByText("speak-harness")).toBeInTheDocument();
    expect(screen.queryByText("Claude")).not.toBeInTheDocument();
  });

  it("card variant renders the literal Claude plus a `· <name>` suffix ungated", () => {
    render(<ClaudeIdentityRow variant="card" sourceName="speak-harness" />);
    expect(screen.getByText("Claude")).toBeInTheDocument();
    expect(screen.getByText("speak-harness")).toBeInTheDocument();
    expect(screen.getByText("·")).toBeInTheDocument();
  });

  it("card variant renders no suffix when the name is the literal fallback (Claude · Claude is unreachable)", () => {
    render(<ClaudeIdentityRow variant="card" sourceName="Claude" />);
    expect(screen.getAllByText("Claude")).toHaveLength(1);
    expect(screen.queryByText("·")).not.toBeInTheDocument();
  });

  it("allows long identities to share the larger reply width", () => {
    expect(CLAUDE_PILL_MAX_W).toBe(CLAUDE_RAIL_PILL_W);
    expect(CLAUDE_PILL_MAX_W).toBeGreaterThan(CLAUDE_PILL_MIN_W);
  });

  it("keeps the rail pill inside the fixed panel WebView", () => {
    // The rail width is free to grow for line length, but the painted card
    // plus its region bleed on both sides has to stay inside the WebView or
    // Windows clips the region back to the window rect.
    expect(CLAUDE_RAIL_PILL_W + HUD_REGION_EDGE_BLEED * 2).toBeLessThanOrEqual(
      PANEL_WIDTH,
    );
  });
});

// ---------------------------------------------------------------------------
// hud-session-name Task 3.3: the permission pill shows the resolved name
// (scope decision 3), and QuestionPanel stops painting the opaque compose
// lease id (`session_handle`) as an identity (design decision 8).
// ---------------------------------------------------------------------------

describe("ClaudePermissionPill (hud-session-name)", () => {
  const permissionProps = {
    detail: null as string | null,
    onAllow: vi.fn(),
    onDeny: vi.fn(),
    onDefer: vi.fn(),
  };

  it("renders the resolved name in its identity row", () => {
    render(<ClaudePermissionPill {...permissionProps} sourceName="pay kit" />);
    expect(screen.getByText("pay kit")).toBeInTheDocument();
  });

  it("aria-label is the identity phrase plus needs approval when named", () => {
    render(<ClaudePermissionPill {...permissionProps} sourceName="pay kit" />);
    expect(
      screen.getByTestId("claude-permission-pill"),
    ).toHaveAttribute("aria-label", "Claude, pay kit needs approval");
  });

  it("aria-label degrades byte-for-byte to today's string when unnamed", () => {
    render(<ClaudePermissionPill {...permissionProps} sourceName="Claude" />);
    expect(
      screen.getByTestId("claude-permission-pill"),
    ).toHaveAttribute("aria-label", "Claude needs approval");
  });

  it("aria-label includes the detail line when named and detailed", () => {
    render(
      <ClaudePermissionPill
        {...permissionProps}
        sourceName="pay kit"
        detail="Run `npm run build`"
      />,
    );
    expect(
      screen.getByTestId("claude-permission-pill"),
    ).toHaveAttribute(
      "aria-label",
      "Claude, pay kit needs approval. Run `npm run build`",
    );
  });
});

describe("QuestionPanel never paints the opaque session handle (hud-session-name Task 3.3)", () => {
  it("renders the resolved source_name instead of session_handle", () => {
    const { container } = render(
      <QuestionPanel
        {...questionProps}
        active={{ ...activeQuestion, source_name: "pay kit" }}
      />,
    );
    expect(screen.getByText("pay kit")).toBeInTheDocument();
    expect(container.textContent).not.toContain(activeQuestion.session_handle);
  });

  it("degrades to the literal Claude fallback when no source_name resolved, never to the handle", () => {
    const { container } = render(<QuestionPanel {...questionProps} />);
    expect(screen.getByText("Claude")).toBeInTheDocument();
    expect(container.textContent).not.toContain(activeQuestion.session_handle);
  });

  it("question card aria-label reads `<phrase> asked a question`", () => {
    const { rerender } = render(
      <QuestionPanel
        {...questionProps}
        active={{ ...activeQuestion, source_name: "pay kit" }}
      />,
    );
    expect(screen.getByLabelText("Claude, pay kit asked a question")).toBeInTheDocument();

    rerender(<QuestionPanel {...questionProps} />);
    expect(screen.getByLabelText("Claude asked a question")).toBeInTheDocument();
  });
});

describe("compact question pill copy (hud-session-name Task 3.3)", () => {
  it("keeps the question in the shared pill's accessible label", () => {
    expect(compactQuestionPillProps(activeQuestion)).toMatchObject({
      sourceName: "Claude",
      accessibleLabel: expect.stringContaining("Claude asked"),
      title: "Which environment should I deploy to?",
    });
  });

  it("leads with the identity phrase when named", () => {
    expect(
      compactQuestionPillProps({
        ...activeQuestion,
        source_name: "pay kit",
      }).accessibleLabel,
    ).toMatch(
      /^Claude, pay kit asked\. /,
    );
  });
});
