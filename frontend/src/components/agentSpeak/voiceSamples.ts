import type { SpeechEngine } from "./useVoicePreview";

// Only offer playback for clips actually included in this source distribution.
const samples = import.meta.glob("/public/voice-samples/*.ogg");

export function hasVoiceSample(engine: SpeechEngine, id: string | number): boolean {
  return `/public/voice-samples/${engine}-${id}.ogg` in samples;
}
