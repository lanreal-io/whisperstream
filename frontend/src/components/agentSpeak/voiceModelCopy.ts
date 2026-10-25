import type { SpeechSelection } from "../../lib/api/speech";

/** The voice model choice, shared by the setup voice step and Configure so the
 *  two never drift (owner, 2026-10-24). Automatic is the default and a real
 *  choice: it is no longer shown as whichever engine it resolved to. */
export const VOICE_MODEL_SELECTIONS: { value: SpeechSelection; label: string }[] = [
  { value: "auto", label: "Automatic" },
  { value: "kokoro", label: "Higher quality" },
  { value: "supertonic", label: "Lower memory" },
];

/** The info tooltip beside the selector explains the current choice. Memory
 *  figures were measured on 2026-10-24: Kokoro peaks near 1.5 GB on a long
 *  reply, Supertonic 3 near 400 MB. Owner-approved wording. */
export const VOICE_MODEL_INFO: Record<SpeechSelection, string> = {
  auto: "Uses Higher quality when 2 GB of memory is free, otherwise Lower memory.",
  kokoro: "Uses Kokoro 82M, which uses up to 1.5 GB of memory while speaking.",
  supertonic: "Uses Supertonic 3, which uses up to 400 MB of memory while speaking.",
};
