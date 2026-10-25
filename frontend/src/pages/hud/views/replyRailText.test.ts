// @vitest-environment node
import { describe, expect, it } from "vitest";

import { replyRailText } from "./ResponsePanel";

describe("replyRailText", () => {
  it("keeps a draft's text beside live speech instead of replacing it", () => {
    // Live: the kept text renders plain, only the new words reveal.
    expect(replyRailText("live", "Ship it first. ", "", "", 0)).toEqual({
      text: "",
      stableCharCount: 0,
      keptText: "Ship it first.",
    });
    expect(replyRailText("live", "Ship it first.", "", "then the logs", 4)).toEqual({
      text: "then the logs",
      stableCharCount: 4,
      keptText: "Ship it first.",
    });
    // Processing shows the joined text, as the backend will append it.
    expect(replyRailText("processing", "Ship it first.", "", "then the logs", 13)).toEqual({
      text: "Ship it first. then the logs",
      stableCharCount: 0,
      keptText: "",
    });
    expect(replyRailText("processing", "Ship it first.", "", "", 0).text).toBe("Ship it first.");
  });

  it("keeps live speech alone when there is no kept draft", () => {
    expect(replyRailText("live", "", "", "hello", 2)).toEqual({ text: "hello", stableCharCount: 2, keptText: "" });
    expect(replyRailText("processing", "  ", "", "hello", 2).text).toBe("hello");
  });

  it("uses the draft, the receipt or nothing in the other phases", () => {
    expect(replyRailText("draft", "Draft", "Sent text", "x", 1).text).toBe("Draft");
    expect(replyRailText("sending", "Draft", "Sent text", "x", 1).text).toBe("Draft");
    expect(replyRailText("sent", "Draft", "Sent text", "x", 1).text).toBe("Sent text");
    expect(replyRailText(null, "Draft", "Sent text", "x", 1).text).toBe("");
  });
});
