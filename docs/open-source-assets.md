# Source assets and model licensing

Lanreal's original source contributions are licensed under GPL-3.0-only. Models,
fonts, language data and other separately identified assets retain their own
terms. Source licensing does not establish permission to redistribute every
downloaded artifact.

## Fonts, media and branding

The UI and offline PDF use Inter and Outfit under SIL OFL 1.1. Their copyright
notices and license texts accompany the font files and generated third-party
notices. The source and offline PDF do not include Satoshi, whose retained
Fontshare terms restrict redistribution.

The bundled voice previews are generated fixed-text demonstrations.
[Provenance and terms](voice-previews.md) record exact model inputs, native preset
mappings, output identities and unverified speaker-rights limitations. Playback
is offered only for clips present at build time, including partial sets.

The bundled HIPAA summary PDF is generated from
[public Markdown](security/lockdown-hipaa-summary.md) with
`scripts/gen-hipaa-summary-pdf.py`, using standard Helvetica fonts. Its claims
are limited to the official implementation and are not a certification or
compliance guarantee. Modified builds require their own assessment. Voice
selection and local speech remain available, with the applicable processing
consent, Lockdown acknowledgment and recovery controls.

The Whisperstream name, app icons and sound assets retain their attribution.
Verify the applicable owner or license permission before redistributing those
assets. Source rights do not authorize a fork to claim official endorsement.
Preserve upstream authorship in AUTHORS.md and LICENSES/Chirp-MIT.txt.

## Official native dependency

The GPL source edition uses official sherpa-onnx and sherpa-onnx-core 1.13.8
Windows wheels. Their exact public URLs, wheel hashes and installed native-file
hashes are recorded in [the wheel inventory](../scripts/sherpa-source-wheels.json).
They contain eSpeak NG and piper-phonemize code. The generated notices preserve
the pinned upstream GPL-3.0-or-later, MIT,
Unicode and other component notices; [notice provenance](../scripts/license-texts/sherpa-upstream-provenance.json)
records the original files and hashes. Sherpa's Apache metadata alone does not
describe all linked components. No wheels or native binaries enter this source
export. Binary redistribution still needs its own source/compliance review.

## Downloaded models

Model weights are downloaded separately. Pinned repository revisions and
artifact identities are recorded in `src/whisperstream/model_sources.py` and the
model catalog. Public metadata access or a missing model-card license field is
not a redistribution grant.

| Model | Terms and source considerations |
| --- | --- |
| Parakeet v2/v3 | Retain NVIDIA attribution and CC-BY-4.0 terms. Quantized exports have their own provenance. |
| Qwen3-ASR | Preserve Apache-2.0 and converter attribution; the pinned artifact association and remaining conversion-reproducibility limits are recorded in [conversion provenance](qwen-provenance.md). |
| Cohere Transcribe | Preserve the existing Apache-2.0 notice and recorded intermediary provenance qualification. |
| Gemma 4 | Preserve the pinned model's Apache-2.0 terms and upstream attribution. The Google and Unsloth artifacts are distinct, hash-pinned inputs. |
| Supertonic 3 | Preserve OpenRAIL-M terms, including use restrictions; the application source license does not replace them. |
| Kokoro | Use pinned upstream v1.0 English/eSpeak files for new acquisition; keep all model/conversion/data notices in `vendor/kokoro/`. Legacy eSpeak-free caches remain supported. Complete compiled eSpeak data source/build mapping before distribution. |
| Stream-1 Mini | First-party fine-tune of Qwen/Qwen3-0.6B-Base under the Stream-1 Mini License, Version 1.0 (Apache-2.0 plus an identification condition). Preserve the "Stream-1 Mini" by "Whisperstream" identification, Alibaba Cloud attribution and the pinned LICENSE and NOTICE in `vendor/stream-1-mini/`. Downloads use the pinned `whisperstream/stream-1-mini` Hugging Face revision with no automatic backup. |

Model delivery uses pinned Hugging Face files with verified upstream GitHub
backups where supported. New upstream Kokoro files use Hugging Face only.
Downloads enforce checksums, expected sizes and archive validation. Model hosting
and consent text must match the delivery policy.

Authoritative inputs: [NVIDIA model card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), [Inter license](https://github.com/rsms/inter/blob/master/LICENSE.txt), [Fontshare terms](https://www.fontshare.com/licenses), `src/whisperstream/model_sources.py`, `vendor/`, and [generated third-party notices](../THIRD_PARTY_LICENSES.txt). The generated [dependency inventory](dependency-licenses.md) covers Python, native, npm, Rust, fonts and models.

## Binary distribution

Conveyed binaries require corresponding source/build inputs, complete component
notices and applicable LGPL/MPL source and relinking materials.
`scripts/source_compliance.py` supports that workflow. Complete those
requirements for the exact artifacts conveyed; source checks alone do not
establish binary compliance.
