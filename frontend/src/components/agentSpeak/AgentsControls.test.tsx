import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { SecondsControl } from "./SecondsControl";
import { AgentTimeline } from "./AgentTimeline";
import { AgentVoiceCompletion, AgentVoiceGlyph } from "./AgentVoiceGlyph";
import { parseAgentConversationEvent } from "../../lib/socket/agentEvents";

afterEach(cleanup);

it("keeps invalid seconds local and allows exact values between tiers", () => {
  const change = vi.fn();
  render(<SecondsControl label="Reply window" value={6} tiered onChange={change} />);
  const input = screen.getByRole("textbox", { name: "Reply window seconds" });
  fireEvent.change(input, { target: { value: "" } });
  fireEvent.change(input, { target: { value: "31" } });
  expect(change).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("Enter a whole number from 0 to 30.");
  fireEvent.change(input, { target: { value: "7" } });
  expect(change).toHaveBeenLastCalledWith(7);
  fireEvent.keyDown(screen.getByRole("slider"), { key: "ArrowRight" });
  expect(change).toHaveBeenLastCalledWith(10);
});

it("renders real turns and activity without interpreting executable markup", () => {
  render(<AgentTimeline provider="codex" conversation={{ session_handle: "h", activity: "working", turns: [
    { id: "one", role: "user", text: "Check it." },
    { id: "two", role: "assistant", text: "# Result\n\n<script>alert(1)</script>\n\n```js\nconst ok = true;\n```" },
  ] }} />);
  expect(screen.getByText("You")).toBeInTheDocument();
  expect(screen.getByText("Codex")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Result" })).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.getByTestId("agent-working-row")).toHaveTextContent("Working");
});

it("rejects malformed or over-budget history snapshots", () => {
  const event = { type: "agent.conversation.snapshot", version: 1, sessions: [{ session_handle: "h", activity: "finished", turns: [{ id: "one", role: "assistant", text: "Okay." }] }] };
  expect(parseAgentConversationEvent(event)).toEqual(event);
  expect(parseAgentConversationEvent({ ...event, secret: "bad" })).toBeNull();
  event.sessions[0].turns[0].text = "🗣".repeat(32000);
  expect(parseAgentConversationEvent(event)).toEqual(event);
  event.sessions[0].turns[0].text += "🗣";
  expect(parseAgentConversationEvent(event)).toBeNull();
  event.sessions[0].turns[0].text = "x".repeat(32001);
  expect(parseAgentConversationEvent(event)).toBeNull();
});

it("draws only an explicitly completed voice and restores bars for new speech", () => {
  const view = render(<AgentVoiceGlyph speaking />);
  expect(document.querySelectorAll("rect")).toHaveLength(5);
  view.rerender(<AgentVoiceGlyph speaking={false} />);
  expect(document.querySelector('[data-agent-glyph="rest"]')).not.toBeNull();
  view.rerender(<AgentVoiceCompletion.Provider value={true}><AgentVoiceGlyph speaking={false} /></AgentVoiceCompletion.Provider>);
  expect(document.querySelector('[data-agent-glyph="completed"]')).not.toBeNull();
  view.rerender(<AgentVoiceCompletion.Provider value={true}><AgentVoiceGlyph speaking /></AgentVoiceCompletion.Provider>);
  expect(document.querySelectorAll("rect")).toHaveLength(5);
});
