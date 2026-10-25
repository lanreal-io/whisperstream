import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ReplyHint, replyHintShortcut } from "./ReplyHint";

afterEach(cleanup);

const both = { pushToTalk: "right shift", toggle: "ctrl+alt+space" };

describe("replyHintShortcut", () => {
  it("names push-to-talk with Hold, toggle with Press, and nothing when unbound", () => {
    expect(replyHintShortcut({ pushToTalk: "right shift", toggle: "", lastOrigin: null })).toEqual({ verb: "Hold", keys: "right shift" });
    expect(replyHintShortcut({ pushToTalk: "", toggle: "ctrl+alt+space", lastOrigin: null })).toEqual({ verb: "Press", keys: "ctrl+alt+space" });
    expect(replyHintShortcut({ pushToTalk: " ", toggle: "", lastOrigin: null })).toBeNull();
  });

  it("follows the last-used shortcut when both are bound, push-to-talk by default", () => {
    expect(replyHintShortcut({ ...both, lastOrigin: null })?.verb).toBe("Hold");
    expect(replyHintShortcut({ ...both, lastOrigin: "push_to_talk" })?.verb).toBe("Hold");
    expect(replyHintShortcut({ ...both, lastOrigin: "toggle" })).toEqual({ verb: "Press", keys: "ctrl+alt+space" });
  });
});

describe("ReplyHint", () => {
  it("reads 'Hold <key> to reply' in the pill and adds 'or click' in the field", () => {
    const shortcuts = { pushToTalk: "right shift", toggle: "", lastOrigin: null };
    render(<><ReplyHint shortcuts={shortcuts} form="pill" platform="windows" /><ReplyHint shortcuts={shortcuts} form="field" platform="windows" /></>);
    expect(screen.getByTestId("reply-hint-pill")).toHaveTextContent("Hold Right Shift to reply");
    expect(screen.getByTestId("reply-hint-field")).toHaveTextContent("Hold Right Shift or click to reply");
    expect(screen.getByTestId("reply-hint-pill")).toHaveStyle({ fontSize: "9px" });
  });

  it("offers click alone when no shortcut is bound", () => {
    render(<ReplyHint shortcuts={{ pushToTalk: "", toggle: "", lastOrigin: null }} form="pill" />);
    expect(screen.getByTestId("reply-hint-pill")).toHaveTextContent("Click to reply");
  });
});
