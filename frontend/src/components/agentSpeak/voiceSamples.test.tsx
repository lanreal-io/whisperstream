import { describe, expect, it } from "vitest";
import { hasVoiceSample } from "./voiceSamples";

describe("bundled voice sample discovery", () => {
  it("finds the included Supertonic and Kokoro previews", () => {
    for (let id = 0; id < 10; id += 1) {
      expect(hasVoiceSample("supertonic", id)).toBe(true);
    }
    for (const id of [
      "af_aoede", "af_bella", "af_heart", "af_kore", "af_nicole",
      "af_sarah", "am_fenrir", "am_michael", "am_puck",
    ]) {
      expect(hasVoiceSample("kokoro", id)).toBe(true);
    }
  });

  it("rejects missing clips and voices belonging to the other engine", () => {
    expect(hasVoiceSample("supertonic", 10)).toBe(false);
    expect(hasVoiceSample("kokoro", "unknown")).toBe(false);
    expect(hasVoiceSample("supertonic", "af_heart")).toBe(false);
    expect(hasVoiceSample("kokoro", 3)).toBe(false);
  });
});
