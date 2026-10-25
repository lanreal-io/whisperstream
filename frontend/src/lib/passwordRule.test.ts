import { describe, expect, it } from "vitest";
import {
  MIN_PASSWORD_LENGTH,
  checkNewPassword,
  passwordRuleErrorFromResponse,
  passwordRuleMessage,
} from "./passwordRule";

describe("checkNewPassword", () => {
  it("requires at least 12 characters with no composition rules", () => {
    expect(MIN_PASSWORD_LENGTH).toBe(12);
    expect(checkNewPassword("a".repeat(11), "a".repeat(11))).toBe("too-short");
    expect(checkNewPassword("a".repeat(12), "a".repeat(12))).toBeNull();
    expect(checkNewPassword("all lowercase words", "all lowercase words")).toBeNull();
  });

  it("checks length before the confirmation", () => {
    expect(checkNewPassword("short", "different")).toBe("too-short");
    expect(checkNewPassword("long enough phrase", "long enough phrasE")).toBe("mismatch");
  });

  it("doesn't count surrounding or repeated whitespace", () => {
    expect(checkNewPassword(" ".repeat(12), " ".repeat(12))).toBe("too-short");
    expect(checkNewPassword("password    ", "password    ")).toBe("too-short");
    expect(checkNewPassword("plum   tide ow", "plum   tide ow")).toBeNull();
    expect(checkNewPassword("p a s s w o r d 1", "p a s s w o r d 1")).toBe("too-short");
  });

  it("leaves the common-password block to the backend", () => {
    expect(checkNewPassword("passwordpassword", "passwordpassword")).toBeNull();
  });
});

describe("passwordRuleMessage", () => {
  it("uses the locked copy", () => {
    expect(passwordRuleMessage("too-short")).toBe("Use at least 12 characters.");
    expect(passwordRuleMessage("too-common")).toBe(
      "That password is too common. Try a longer phrase.",
    );
    expect(passwordRuleMessage("mismatch")).toBe("Passwords don't match.");
  });
});

describe("passwordRuleErrorFromResponse", () => {
  it("maps the Lockdown error_code body", () => {
    const err = new Error(
      '400: {"ok":false,"error_code":"password_too_common","message":"x"}',
    );
    expect(passwordRuleErrorFromResponse(err)).toBe("too-common");
  });

  it("maps the history recovery error body", () => {
    expect(
      passwordRuleErrorFromResponse(new Error('400: {"error":"password_too_short"}')),
    ).toBe("too-short");
  });

  it("returns null for unrelated failures", () => {
    expect(passwordRuleErrorFromResponse(new Error("401: Unauthorized"))).toBeNull();
    expect(
      passwordRuleErrorFromResponse(new Error('409: {"error_code":"managed_by_organization"}')),
    ).toBeNull();
    expect(passwordRuleErrorFromResponse(undefined)).toBeNull();
  });
});
