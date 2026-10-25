// @vitest-environment node

/**
 * Tests for normalizeDomain — the UI-layer host normalization helper.
 *
 * Matching is HOST-ONLY (strip www. only; mail.google.com stays mail.google.com,
 * NOT folded to google.com).
 *
 * Required cases (from tasks.md Task 2.1):
 *   https://github.com/x     -> github.com
 *   mail.google.com          -> mail.google.com  (kept, NOT google.com)
 *   www.example.com          -> example.com
 *   how to code              -> null  (interior whitespace / search text)
 *   localhost                -> null  (no dot)
 *   ""                       -> null  (empty)
 */
import { describe, it, expect } from "vitest";
import { normalizeDomain } from "../normalizeDomain";

describe("normalizeDomain", () => {
  // --- required cases from tasks.md ---

  it("strips scheme and path: https://github.com/x -> github.com", () => {
    expect(normalizeDomain("https://github.com/x")).toBe("github.com");
  });

  it("keeps subdomain (host-only, no eTLD+1): mail.google.com -> mail.google.com", () => {
    expect(normalizeDomain("mail.google.com")).toBe("mail.google.com");
  });

  it("strips leading www.: www.example.com -> example.com", () => {
    expect(normalizeDomain("www.example.com")).toBe("example.com");
  });

  it("returns null for search text with spaces: 'how to code'", () => {
    expect(normalizeDomain("how to code")).toBeNull();
  });

  it("returns null for bare word without dot: localhost", () => {
    expect(normalizeDomain("localhost")).toBeNull();
  });

  it("returns null for empty string", () => {
    expect(normalizeDomain("")).toBeNull();
  });

  // --- additional coverage ---

  it("strips http:// scheme", () => {
    expect(normalizeDomain("http://example.com")).toBe("example.com");
  });

  it("strips query string", () => {
    expect(normalizeDomain("https://example.com?q=test")).toBe("example.com");
  });

  it("strips fragment", () => {
    expect(normalizeDomain("https://example.com#section")).toBe("example.com");
  });

  it("strips port", () => {
    expect(normalizeDomain("example.com:8080")).toBe("example.com");
  });

  it("strips port from full URL", () => {
    expect(normalizeDomain("https://example.com:443/path")).toBe("example.com");
  });

  it("lowercases the result", () => {
    expect(normalizeDomain("HTTPS://GITHUB.COM/X")).toBe("github.com");
  });

  it("trims surrounding whitespace", () => {
    expect(normalizeDomain("  github.com  ")).toBe("github.com");
  });

  it("keeps multi-level subdomain intact (news.ycombinator.com)", () => {
    expect(normalizeDomain("https://news.ycombinator.com/newest")).toBe(
      "news.ycombinator.com"
    );
  });

  it("returns null for null-like falsy: whitespace only", () => {
    expect(normalizeDomain("   ")).toBeNull();
  });

  it("returns null for a bare IP-like without dot check failing (e.g. '127' — no dot)", () => {
    expect(normalizeDomain("127")).toBeNull();
  });

  it("passes through a plain domain without scheme", () => {
    expect(normalizeDomain("github.com")).toBe("github.com");
  });

  it("strips www. even when present in a full URL", () => {
    expect(normalizeDomain("https://www.github.com/x")).toBe("github.com");
  });
});
