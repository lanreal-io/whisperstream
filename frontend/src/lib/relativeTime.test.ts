// @vitest-environment node

/**
 * Tests for the formatRelative helper used in the Updates settings row.
 *
 * Boundary semantics from design.md:
 *   < 60_000 ms          -> "just now"
 *   < 60 minutes         -> "N min ago"
 *   < 24 hours           -> "1 hour ago" / "N hours ago"
 *   >= 24 hours          -> "1 day ago" / "N days ago"
 *
 * design.md explicitly forbids a "yesterday" case.
 */
import { describe, it, expect } from "vitest";
import { formatRelative } from "./relativeTime";

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

describe("formatRelative", () => {
  it("returns 'just now' for 0ms", () => {
    expect(formatRelative(0)).toBe("just now");
  });

  it("returns 'just now' for 30s", () => {
    expect(formatRelative(30 * SECOND)).toBe("just now");
  });

  it("returns '1 min ago' at the 60s boundary", () => {
    expect(formatRelative(60 * SECOND)).toBe("1 min ago");
  });

  it("returns '1 min ago' for 90s", () => {
    expect(formatRelative(90 * SECOND)).toBe("1 min ago");
  });

  it("returns '59 min ago' at the 59min boundary", () => {
    expect(formatRelative(59 * MINUTE)).toBe("59 min ago");
  });

  it("returns '1 hour ago' at the 60min boundary", () => {
    expect(formatRelative(60 * MINUTE)).toBe("1 hour ago");
  });

  it("returns '1 hour ago' for 1h", () => {
    expect(formatRelative(1 * HOUR)).toBe("1 hour ago");
  });

  it("returns '2 hours ago' for 2h (plural)", () => {
    expect(formatRelative(2 * HOUR)).toBe("2 hours ago");
  });

  it("returns '23 hours ago' for 23h", () => {
    expect(formatRelative(23 * HOUR)).toBe("23 hours ago");
  });

  it("returns '1 day ago' at the 24h boundary", () => {
    expect(formatRelative(24 * HOUR)).toBe("1 day ago");
  });

  it("returns '1 day ago' for 25h", () => {
    expect(formatRelative(25 * HOUR)).toBe("1 day ago");
  });

  it("returns '1 day ago' for 47h", () => {
    expect(formatRelative(47 * HOUR)).toBe("1 day ago");
  });

  it("returns '2 days ago' for 48h", () => {
    expect(formatRelative(48 * HOUR)).toBe("2 days ago");
  });

  it("returns '7 days ago' for 7 days", () => {
    expect(formatRelative(7 * DAY)).toBe("7 days ago");
  });
});
