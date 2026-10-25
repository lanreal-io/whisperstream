/**
 * Pure reducer helpers for the enhancement-profile manager.
 *
 * Every mutation returns the COMPLETE next `{ profiles, default_profile_id }`
 * pair. The Enhancement page persists that whole pair on every change because
 * PUT /api/config replaces the entire config — sending a partial `profiles`
 * array would wipe the rest. Keeping the math here (rather than inline in the
 * component) lets the delete-default-reassign guard be unit-tested directly.
 */

export interface Profile {
  id: string;
  name: string;
  prompt: string;
  builtin?: boolean;
  /** Optional curated glyph for custom profiles. Built-in glyphs are derived from id. */
  icon?: string;
  /** Optional per-profile input-style override. Absent = inherit the global `input_style` default. */
  language?: string;
  input_style?: string;
  /** Registry-owned cleanup mode. Only consulted when the active local model
   *  owns a fixed prompt contract; Gemma and cloud providers ignore it and use
   *  `prompt`. Absent = the backend's id-derived/default mode. */
  mode?: "chat" | "code" | "email";
  /** Optional default email sign-off for this profile. Appended (as its own
   *  block) after cleanup when this profile runs and no sign-off was dictated.
   *  Absent/blank = no default signature for this profile. */
  signature?: string;
}

export interface ProfilesState {
  profiles: Profile[];
  default_profile_id: string;
}

/** The fixed id the default pointer falls back to when its target is deleted. */
export const FALLBACK_PROFILE_ID = "chat";

/** A profile id is built-in iff it is one of the three seeded rows. */
export function isBuiltinId(id: string): boolean {
  return id === "chat" || id === "code" || id === "email";
}

/**
 * Append a new profile. Never changes the default pointer — creating is not
 * selecting (project rule: no silent selection migrations).
 */
export function addProfile(
  state: ProfilesState,
  profile: Profile,
): ProfilesState {
  return {
    profiles: [...state.profiles, profile],
    default_profile_id: state.default_profile_id,
  };
}

/**
 * Remove a profile by id. If the removed profile was the default, the default
 * pointer is reassigned to the fixed fallback ("chat") in the SAME result so
 * the list never has zero defaults.
 */
export function removeProfile(
  state: ProfilesState,
  id: string,
): ProfilesState {
  const profiles = state.profiles.filter((p) => p.id !== id);
  const default_profile_id =
    state.default_profile_id === id
      ? FALLBACK_PROFILE_ID
      : state.default_profile_id;
  return { profiles, default_profile_id };
}

/** Point the default pointer at an existing profile id. */
export function makeDefault(state: ProfilesState, id: string): ProfilesState {
  return { profiles: state.profiles, default_profile_id: id };
}

/**
 * Produce a collision-free profile id that is never a built-in id and never
 * clashes with an existing profile. `gen` defaults to `crypto.randomUUID`.
 */
export function freshProfileId(
  existingIds: string[],
  gen: () => string = () => crypto.randomUUID(),
): string {
  const taken = new Set([...existingIds, "chat", "code", "email"]);
  let id = gen();
  while (taken.has(id)) id = gen();
  return id;
}
