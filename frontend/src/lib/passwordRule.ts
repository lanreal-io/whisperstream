/**
 * Shared rule for NEW passwords: Lockdown setup, Lockdown change password and
 * the optional history recovery password. Never applied at unlock, so older
 * 8 to 11 character Lockdown passwords keep working.
 *
 * The client checks length and confirmation only. The common-password block
 * lives in the backend (`password_rule.py`), which answers `password_too_common`;
 * map that with `passwordRuleErrorFromResponse`. Never log a password.
 */

export const MIN_PASSWORD_LENGTH = 12;

export type PasswordRuleError = "too-short" | "too-common" | "mismatch";

/** Locked copy (copy-review.md, Errors). */
export const PASSWORD_RULE_MESSAGES: Record<PasswordRuleError, string> = {
  "too-short": "Use at least 12 characters.",
  "too-common": "That password is too common. Try a longer phrase.",
  mismatch: "Passwords don't match.",
};

export function passwordRuleMessage(error: PasswordRuleError): string {
  return PASSWORD_RULE_MESSAGES[error];
}

/**
 * Length that counts toward the rule: surrounding and repeated whitespace is
 * ignored, matching the backend, so padding can't satisfy the minimum.
 */
export function meaningfulPasswordLength(password: string): number {
  const collapsed = password.replace(/\s+/g, " ").trim();
  const words = collapsed.split(" ");
  // "p a s s w o r d": spacing out letters adds no strength.
  if (collapsed && Math.max(...words.map((word) => word.length)) <= 2) {
    return words.join("").length;
  }
  return collapsed.length;
}

/** Client-side check for a new password and its confirmation. */
export function checkNewPassword(
  password: string,
  confirm: string,
): PasswordRuleError | null {
  if (meaningfulPasswordLength(password) < MIN_PASSWORD_LENGTH) return "too-short";
  if (password !== confirm) return "mismatch";
  return null;
}

const BACKEND_CODES: Record<string, PasswordRuleError> = {
  password_too_short: "too-short",
  password_too_common: "too-common",
};

/**
 * Map a failed request's backend rule code to a PasswordRuleError. Handles the
 * Lockdown routes (`error_code`) and the history recovery routes (`error`).
 * fetchJSON errors carry "<status>: <body>" as their message.
 */
export function passwordRuleErrorFromResponse(
  err: unknown,
): PasswordRuleError | null {
  const message = err instanceof Error ? err.message : String(err ?? "");
  const body = message.replace(/^\d+:\s*/, "");
  try {
    const parsed = JSON.parse(body) as { error?: unknown; error_code?: unknown };
    for (const code of [parsed.error_code, parsed.error]) {
      if (typeof code === "string" && code in BACKEND_CODES) {
        return BACKEND_CODES[code];
      }
    }
  } catch {
    /* Non-JSON body: fall through to the substring check. */
  }
  for (const [code, error] of Object.entries(BACKEND_CODES)) {
    if (message.includes(`"${code}"`)) return error;
  }
  return null;
}
