# Qwen3-ASR conversion provenance

This document identifies the Qwen3-ASR-0.6B INT8 ONNX download, its upstream
license declarations and the available conversion provenance.

The [pinned original ModelScope publisher](https://modelscope.cn/api/v1/models/zengshuishui/Qwen3-ASR-onnx/repo?Revision=9c182309f7bb075f241424441add9e16c5086dfb&FilePath=README.md)
declares Apache-2.0 and identifies the official Qwen model. The
[pinned mirror card](https://huggingface.co/csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25/blob/68818b2313fe77bd06f6a7c5068ff3ef59d02b8a/README.md)
credits that publisher and Wasser1462's converter. In the
[October 14 author reply](https://github.com/Wasser1462/Qwen3-ASR-onnx/issues/11#issuecomment-6162677694),
Wasser1462 confirmed official Qwen3-ASR ancestry and the same Apache-2.0
license for the repository, stating that a LICENSE file would follow.

## Exact artifact association

`src/whisperstream/model_sources.py` selects mirror revision
`68818b2313fe77bd06f6a7c5068ff3ef59d02b8a`. These six runtime files match the
[publisher listing](https://modelscope.cn/api/v1/models/zengshuishui/Qwen3-ASR-onnx/repo/files?Revision=9c182309f7bb075f241424441add9e16c5086dfb&Recursive=true)
at ModelScope revision `9c182309f7bb075f241424441add9e16c5086dfb`:

| Runtime path | Bytes | SHA-256 |
| --- | ---: | --- |
| `tokenizer/vocab.json` | 2776833 | `ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910` |
| `tokenizer/tokenizer_config.json` | 12487 | `4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c` |
| `tokenizer/merges.txt` | 1671853 | `8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5` |
| `conv_frontend.onnx` | 44148281 | `d22dc4423e0940e49884e903d2ea2f7e5567c14fc1aed97e4e26d6b8f208ef9e` |
| `encoder.int8.onnx` | 182491662 | `60748d3e6744a57c9c91e1b17424a6c2990567e8adceb0783940c03ed98fa9d9` |
| `decoder.int8.onnx` | 755914231 | `4f6885be5959ae26af3089d38ee7972c5fafbeeb1cf8d5e76eab6d8b61ca5771` |

Total: 987,015,347 bytes. The selected GitHub backup archive is 878,702,423
bytes with SHA-256 `393f8a14e2f5fb96746aaab342997a40641001fbd5bf9592a080a8329178ee96`.
These identities come from the published file metadata and the application
catalog. They do not establish an independent reproduction of the conversion.

## Reproducibility and notices

Exact input-model and exporter revisions are not recorded in the available
conversion provenance. This project has not independently reproduced the
conversion. The author's reply does not enumerate the runtime hashes; their
association with the publisher comes from the listing and mirror card above.

Keep Alibaba/Qwen and converter attribution, verbatim Apache-2.0 text and the
INT8 ONNX conversion/quantization disclosure in both generated notice copies.
The model is downloaded on demand and is not bundled in the installer. Update
this record when changing the selected conversion or its artifact pins.
