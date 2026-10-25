// @vitest-environment node

/**
 * Tests for the pure enhancement-profile reducers.
 *
 * The load-bearing case is the delete-default-reassign guard: deleting the
 * profile the default pointer references must reassign the pointer to "chat"
 * in the SAME result so the list is never default-less. The other cases pin
 * the no-silent-selection rule (add never changes the default) and id
 * uniqueness (fresh ids never collide with existing or built-in ids).
 */
import { describe, it, expect } from "vitest";
import {
  addProfile,
  removeProfile,
  makeDefault,
  freshProfileId,
  isBuiltinId,
  FALLBACK_PROFILE_ID,
  type ProfilesState,
} from "./profiles";
import {
  PROFILE_ICON_KEYS,
  PROFILE_ICON_LABELS,
  BUILTIN_PROFILE_ICONS,
  isProfileIconKey,
} from "../components/profileIcons";

function baseState(): ProfilesState {
  return {
    profiles: [
      { id: "chat", name: "Chat", prompt: "chat-prompt" },
      { id: "code", name: "Code", prompt: "code-prompt" },
      { id: "abc-123", name: "Email", prompt: "email-prompt" },
    ],
    default_profile_id: "abc-123",
  };
}

describe("removeProfile", () => {
  it("deleting the default reassigns the pointer to chat", () => {
    const next = removeProfile(baseState(), "abc-123");
    expect(next.profiles.map((p) => p.id)).toEqual(["chat", "code"]);
    expect(next.default_profile_id).toBe("chat");
    expect(next.default_profile_id).toBe(FALLBACK_PROFILE_ID);
  });

  it("deleting a non-default leaves the default untouched", () => {
    const state: ProfilesState = { ...baseState(), default_profile_id: "code" };
    // Add a second custom row so there is a non-default custom to delete.
    state.profiles = [
      ...state.profiles,
      { id: "xyz-999", name: "Notes", prompt: "notes-prompt" },
    ];
    const next = removeProfile(state, "xyz-999");
    expect(next.profiles.map((p) => p.id)).toEqual(["chat", "code", "abc-123"]);
    expect(next.default_profile_id).toBe("code");
  });
});

describe("addProfile", () => {
  it("appends without changing the default pointer", () => {
    const next = addProfile(baseState(), {
      id: "new-1",
      name: "Slack",
      prompt: "slack-prompt",
      builtin: false,
    });
    expect(next.profiles.map((p) => p.id)).toEqual([
      "chat",
      "code",
      "abc-123",
      "new-1",
    ]);
    // Creating is not selecting: default stays where it was.
    expect(next.default_profile_id).toBe("abc-123");
  });

  it("preserves a custom profile icon selection", () => {
    const next = addProfile(baseState(), {
      id: "new-2",
      name: "Research",
      prompt: "research-prompt",
      icon: "search",
    });
    expect(next.profiles[next.profiles.length - 1]?.icon).toBe("search");
  });
});

describe("makeDefault", () => {
  it("repoints the default and leaves the list intact", () => {
    const next = makeDefault(baseState(), "code");
    expect(next.default_profile_id).toBe("code");
    expect(next.profiles).toEqual(baseState().profiles);
  });
});

describe("freshProfileId", () => {
  it("never collides with existing ids or the built-in ids", () => {
    // gen yields a collision (existing id), then a built-in id, then a free id.
    const sequence = ["abc-123", "chat", "code", "free-id"];
    let i = 0;
    const id = freshProfileId(["abc-123"], () => sequence[i++]);
    expect(id).toBe("free-id");
  });

  it("returns the first candidate when it is already unique", () => {
    const id = freshProfileId(["abc-123"], () => "unique-one");
    expect(id).toBe("unique-one");
  });
});

describe("isBuiltinId", () => {
  it("only chat and code are built in", () => {
    expect(isBuiltinId("chat")).toBe(true);
    expect(isBuiltinId("code")).toBe(true);
    expect(isBuiltinId("abc-123")).toBe(false);
    expect(isBuiltinId("custom")).toBe(false);
  });
});

describe("profile icon table", () => {
  it("is exactly the ordered 14-key curated set the backend validates", () => {
    expect([...PROFILE_ICON_KEYS]).toEqual([
      "chat",
      "code",
      "mail",
      "document",
      "note",
      "terminal",
      "checklist",
      "calendar",
      "people",
      "sparkle",
      "search",
      "pen",
      "book",
      "globe",
    ]);
  });

  it("labels every key with the locked one-word name", () => {
    expect(PROFILE_ICON_KEYS.map((key) => PROFILE_ICON_LABELS[key])).toEqual([
      "Chat",
      "Code",
      "Mail",
      "Document",
      "Note",
      "Terminal",
      "Checklist",
      "Calendar",
      "People",
      "Sparkle",
      "Search",
      "Pen",
      "Book",
      "Globe",
    ]);
  });

  it("recognizes only curated keys", () => {
    expect(isProfileIconKey("sparkle")).toBe(true);
    expect(isProfileIconKey("rocket")).toBe(false);
    expect(isProfileIconKey("")).toBe(false);
    expect(isProfileIconKey(undefined)).toBe(false);
  });

  it("derives built-in glyphs from the immutable ids only", () => {
    expect(BUILTIN_PROFILE_ICONS).toEqual({
      chat: "chat",
      code: "code",
      email: "mail",
    });
  });
});
