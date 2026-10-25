# Silero VAD v6.2.3

Official upstream revision: `5cd7945676eb32225748052e2e6a0580e4686a08`.
No intermediary conversion, quantization or weights modification.

| Upstream file | Local file | SHA-256 |
| --- | --- | --- |
| `src/silero_vad/data/silero_vad_16k_op15.onnx` | `src/whisperstream/assets/silero_vad.onnx` | `7ed98ddbad84ccac4cd0aeb3099049280713df825c610a8ed34543318f1b2c49` |
| `LICENSE` | `src/whisperstream/assets/silero-vad-LICENSE.txt` | `2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b` |

The model is 1,289,603 bytes. The license is preserved byte-for-byte. Source:
https://github.com/snakers4/silero-vad/tree/5cd7945676eb32225748052e2e6a0580e4686a08

The frame/context/recurrent-state contract in `speech_gate.py` is adapted from
upstream `src/silero_vad/utils_vad.py` (MIT). It uses NumPy and the already shipped
ONNX Runtime, not the upstream Python package or Torch wrapper. At 16 kHz it
passes 512 samples plus 64 context samples with state shape `(2, 1, 128)` and an
int64 sample-rate scalar. Reset state at every independent recognition window.

The runtime verifies the model hash before creating a single-thread CPU session.
Nuitka includes both asset files; the license generator emits their attribution
and MIT text into both shipped third-party notices. 