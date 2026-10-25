// @vitest-environment node

import { describe, expect, it } from "vitest";
import {
  agentIdentityPhrase,
  agentProviderLabel,
  claudeIdentityPhrase,
  normalizeSessionNameForDisplay,
} from "./sessionName";

// Mirrors tests/test_agent_session_names.py::TestNormalizeSessionNameForSpeech
// case for case (locked copy contract 1) so the Python and TypeScript twins
// cannot drift.
describe("normalizeSessionNameForDisplay", () => {
  it("converts a hyphen to a space", () => {
    expect(normalizeSessionNameForDisplay("speak-harness")).toBe("speak harness");
  });

  it("converts an underscore to a space", () => {
    expect(normalizeSessionNameForDisplay("pty_proxy")).toBe("pty proxy");
  });

  it("splits camelCase boundaries", () => {
    expect(normalizeSessionNameForDisplay("payKitReview")).toBe("pay Kit Review");
  });

  it("collapses repeated separators", () => {
    expect(normalizeSessionNameForDisplay("a--b")).toBe("a b");
  });

  it("leaves an already-clean name unchanged", () => {
    expect(normalizeSessionNameForDisplay("speak harness")).toBe("speak harness");
  });

  it("does not change case", () => {
    expect(normalizeSessionNameForDisplay("SPEAK-HARNESS")).toBe("SPEAK HARNESS");
  });

  it("converts a slash to a space", () => {
    expect(normalizeSessionNameForDisplay("app/pty")).toBe("app pty");
  });
});

describe("claudeIdentityPhrase", () => {
  it("returns 'Claude, <name>' with the name normalized for a named session", () => {
    expect(claudeIdentityPhrase("speak-harness")).toBe("Claude, speak harness");
  });

  it("returns the bare literal for an empty name", () => {
    expect(claudeIdentityPhrase("")).toBe("Claude");
  });

  it("returns the bare literal when the resolved name is the literal fallback", () => {
    expect(claudeIdentityPhrase("Claude")).toBe("Claude");
  });

  it("never emits a trailing comma", () => {
    expect(claudeIdentityPhrase("speak-harness").endsWith(",")).toBe(false);
  });
});

describe("provider-aware identity phrases", () => {
  it("uses Codex as the bare fallback and normalizes a named Codex session", () => {
    expect(agentProviderLabel("codex")).toBe("Codex");
    expect(agentIdentityPhrase("codex", "Codex")).toBe("Codex");
    expect(agentIdentityPhrase("codex", "speak-harness")).toBe(
      "Codex, speak harness",
    );
  });

  it("preserves the shipped Claude identity wrapper", () => {
    expect(agentIdentityPhrase("claude", "speak-harness")).toBe(
      claudeIdentityPhrase("speak-harness"),
    );
  });
});
