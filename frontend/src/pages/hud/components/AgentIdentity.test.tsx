import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ClaudeIdentityRow } from "./AgentIdentity";

afterEach(cleanup);

describe("ClaudeIdentityRow speaking state", () => {
  // Escape and replying stop speech; neither row carries a stop button
  // (owner, 2026-10-24). The animated mark is the only speaking signal.
  it.each(["card", "compact"] as const)("has no stop button in the %s row", (variant) => {
    render(<ClaudeIdentityRow variant={variant} sourceName="whisperstream" speaking />);
    expect(screen.queryByRole("button", { name: "Stop speaking" })).toBeNull();
    expect(screen.queryByText(/speaking/i)).toBeNull();
    expect(document.querySelector('[data-agent-glyph="speaking"]')).not.toBeNull();
  });
});
