# Voice preview provenance and terms

The 19 bundled clips in `frontend/public/voice-samples/` are locally generated
fixed-text demonstrations, not customer recordings or downloaded actor samples.
The text is "This is how your agent's replies will sound." No microphone or
personal transcript is an input. They demonstrate presets, not an endorsement
by any performer or model publisher.

`manifest.json` records the exact model repositories/revisions and every loaded
input file size/hash, generation-code and native-library hashes, native speaker
mapping, speed, sample rate, encoder version and each clip's size/frame/hash.
Generation used official sherpa-onnx/core 1.13.8 on Windows, with libsndfile's
Ogg/Vorbis encoder. Code hashes identify the generating implementation; source
adaptations and nondeterministic synthesis/encoder behavior can produce different
bytes on another platform. Regeneration produces a new manifest for those bytes.

| Engine | Immutable model revision | Preview scope |
| --- | --- | --- |
| Supertonic 3 INT8 | `csukuangfj2/sherpa-onnx-supertonic-3-tts-int8-2026-05-11@cca5a0e6c96e1d2c720986bf7e75fcc81dee3ae4` | Presets 0 through 9 |
| Kokoro v1.0 | `csukuangfj/kokoro-multi-lang-v1_0@f7b96bb6bef5c5da4d3aa4f4e0498fbbf62dc78b` | af_heart=3, af_bella=2, af_nicole=6, af_aoede=1, af_kore=5, af_sarah=9, am_fenrir=14, am_michael=16, am_puck=18 |

Kokoro uses the selected upstream English lexicon/eSpeak frontend. Its complete
voice input has 54 embeddings, but only these nine generate bundled clips. No
model weights, voice embeddings or compiled phonemizer data are in the clips.
The nine selected float32 arrays compare exactly with the corresponding previous
curated v1.0 arrays, not just their names. Model/conversion/data attribution remains
in [`vendor/kokoro`](../vendor/kokoro/UPSTREAM-NOTICE.txt). This comparison does
not independently establish training-data or speaker consent rights.

[Supertonic's OpenRAIL-M license](https://huggingface.co/Supertone/supertonic-3/blob/3cadd1ee6394adea1bd021217a0e650ede09a323/LICENSE)
addresses generated output in section 6 and retains applicable use restrictions.
Preserve lawful-use and output-use conditions. Its model-user terms remain
separate from application entitlement; an output clip is not a model weight.
[Kokoro's pinned model card](https://huggingface.co/hexgrad/Kokoro-82M/blob/f3ff3571791e39611d31c381e3a41a3af07b4987/README.md)
represents permissive/public-domain/synthetic training sources. Apache/MIT model
and conversion permissions do not supply a complete performer, personality or
publicity-rights chain. Individual releases/preset-to-source mappings remain
unverified for these demonstrations. No independent actor clearance is claimed.

To the extent Lanreal holds copyright or related rights in these demonstration
recordings, it grants permission to use, copy, modify and redistribute them with
this repository under GPL-3.0-only. This grants no third-party voice/personality
rights and makes no claim that generated output itself is copyrightable. Model
licenses retain their separate scope and conditions; the app's license does not
turn all generated audio or all voice rights into GPL-licensed material.

Generate with `uv run python scripts/generate-voice-samples.py` after installing
both selected bundles in an isolated `WHISPERSTREAM_DATA_DIR`. The generator
checks every input against the immutable catalog and refuses legacy Kokoro
fallback or altered files. `--verify` fully decodes all expected outputs and
checks the current sentence, roster and manifest output hashes without loading
models. The picker offers playback only when a clip exists at build time;
partial or absent sets retain voice selection without a broken playback button.
