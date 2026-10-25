# Startup capture fixture

This synthetic fixture exercises speech begun at the exact end of the
push-to-talk start cue. The Kokoro voice model (voice `af_heart`) synthesized
`one` through `five` in five separate calls. `raw/` preserves those outputs;
`words/` contains the independently onset/offset-trimmed 16 kHz clips. No real
voice is involved.

`numbers.wav` concatenates the trimmed words with the strictest spacing, 0 ms,
and begins at the detected onset of `one`, with no leading padding. Direct
Parakeet v2 and v3 calls retain all five numbers in order. Exact trim points,
threshold, voice, and hashes are recorded in `manifest.json`. Generation is
deterministic: a rerun reproduces the same bytes.

Regenerate with the Kokoro voice model installed in the selected data directory:

```powershell
uv run --extra dev python scripts/generate_startup_capture_fixture.py --gap-ms 0
```
