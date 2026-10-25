# Upstream Kokoro source and data mapping

This record covers the GPL edition's unchanged upstream files at
`csukuangfj/kokoro-multi-lang-v1_0` revision
`f7b96bb6bef5c5da4d3aa4f4e0498fbbf62dc78b`. The authoritative
[file inventory](upstream-files.json) records all 13 SHA-256 hashes and sizes.
The [upstream notice](UPSTREAM-NOTICE.txt) owns model, conversion and data notices.

## Current delivery boundary

The Windows [engine recipe](../../scripts/build.py) includes the Kokoro worker
and official sherpa package/native files. Its data-file arguments do not include
the Kokoro model cache, `voices.bin` or `espeak-ng-data`. The
[Windows Tauri resources](../../src-tauri/tauri.windows.conf.json) do not stage
them either. This is source-level evidence of the intended package boundary;
verify the final installer inventory during package verification.

The [download registry and downloader](../../src/whisperstream/model_downloader.py)
select all 13 pinned files and stage them in the user's configured models root.
The Kokoro entry in [model sources](../../src/whisperstream/model_sources.py) has
only the HF repository/revision/files, with no R2 mirror or GitHub archive.
`_download_files` constructs immutable HF resolve URLs and checks size/SHA-256.
The private staging build allows ordinary upstream Kokoro downloads. Other
models retain their existing provider policy. Existing legacy caches are retained.

The five unresolved compiled-data mappings below are relevant if Whisperstream
later conveys those files in a model bundle, mirror or offline pack. The current
installer recipe and source tree contain none of those data files. Native eSpeak
code is included in the installer; its source/build mapping is recorded in
[the native mapping](../sherpa-onnx/SOURCE-MATERIALS.md).

## Model and voice scope

The pinned [HF README](https://huggingface.co/csukuangfj/kokoro-multi-lang-v1_0/blob/f7b96bb6bef5c5da4d3aa4f4e0498fbbf62dc78b/README.md)
describes a 54-embedding voice table generated from official Kokoro-82M voices.
The app exposes the same nine American English identities, but downloads the
complete unchanged `voices.bin`, SHA-256
`1c5a5b983d3d50d8586d437a51f3faa2da7919ce76a013c081e65671a3447c29`.

The pinned sherpa tree contains [the generator and mapping](https://github.com/k2-fsa/sherpa-onnx/tree/11afbd009a7f8c08f4bcf2fc1b265d0df4670fbf/scripts/kokoro/v1.0)
and the corresponding model-metadata generator. Its `run.sh` clones Kokoro-82M
and retrieves Misaki lexicons from moving upstream branches; it does not bind
those inputs to immutable revisions or explain the compiled eSpeak-data build.
Consequently it documents the conversion method, not an exact reproduction of
the selected model, voice or lexicon bytes. Preserve the existing Apache/MIT
and training-data attribution when redistributing these files.

## Legacy cache attribution

Existing caches can still use the earlier nine-preset archive described in
[NOTICE.txt](NOTICE.txt) and the archive's `export-manifest.json`.
Its model, conversion, lexicon, G2P, tagger and training-data attribution remains
in the generated notices. No upstream NOTICE text file was identified in the
inspected Kokoro, Misaki or PeterReid snapshots recorded in that notice.
The archive's inference frontend and the current official sherpa native runtime
have distinct dependencies; the latter includes eSpeak/Piper.

## English eSpeak data: verified and unresolved files

Metadata and plaintext sources were inspected on October 15, 2026. No model or
compiled language-data binary was downloaded during this mapping.

Two selected files are already preferred-form text. Their complete upstream
bytes match the SHA-256 values in `upstream-files.json`:

| Selected file | Matching source in eSpeak commit `ed530aa113046142eb5115cf2fc9157854d0ffe1` | SHA-256 |
| --- | --- | --- |
| `espeak-ng-data/lang/gmw/en` | [English definition](https://github.com/csukuangfj/espeak-ng/blob/ed530aa113046142eb5115cf2fc9157854d0ffe1/espeak-ng-data/lang/gmw/en), 140 bytes | `4605d5330801de3641c6e366d15f129ea1f5ffbce8722642aba01ace07ab9c83` |
| `espeak-ng-data/lang/gmw/en-US` | [US English definition](https://github.com/csukuangfj/espeak-ng/blob/ed530aa113046142eb5115cf2fc9157854d0ffe1/espeak-ng-data/lang/gmw/en-US), 257 bytes | `41534c2a22df5dd4f1052ff9e1a33a3ea7bff5a26b5c02bdad5ba8ddb7524704` |

The remaining five files are compiled outputs. HF Git object metadata matches
each selected file to the same bytes in upload
[`faadfa485121aa7b51bb87103c82cb18e85f9f6e`](https://huggingface.co/csukuangfj/kokoro-multi-lang-v1_0/tree/faadfa485121aa7b51bb87103c82cb18e85f9f6e/espeak-ng-data)
on February 4, 2025. Their source revision and generation command were not
identified in the inspected model README/history or pinned Kokoro scripts.

| Compiled file | Bytes | HF Git blob identity (not SHA-256) |
| --- | --- | --- |
| `en_dict` | 166944 | `b05b86152a011739d4cfebe716b43adcc9da76a2` |
| `phondata` | 550424 | `5b3c94a570dc3bdc6500dcd92602faa0640503bc` |
| `phonindex` | 39074 | `03ba8068e7ab71932d2d1b2d6e55b5d63447b30d` |
| `phontab` | 55796 | `5c25ca1f9ffd3d80c0bba5a5dea59edfdb9de573` |
| `intonations` | 2040 | `ca2d4b362c3dc1dac7b5da760c33f7ad9c96047a` |

The [current tree metadata](https://huggingface.co/api/models/csukuangfj/kokoro-multi-lang-v1_0/tree/f7b96bb6bef5c5da4d3aa4f4e0498fbbf62dc78b/espeak-ng-data)
and [earlier upload metadata](https://huggingface.co/api/models/csukuangfj/kokoro-multi-lang-v1_0/tree/faadfa485121aa7b51bb87103c82cb18e85f9f6e/espeak-ng-data)
establish file continuity, not preferred source. The exact download SHA-256 values
remain in `upstream-files.json`.

Before adding redistribution of these five compiled files, obtain their actual source
revision, any modifications and build recipe from upstream evidence or a
documented reproduction. The same filename, compatible runtime or matching
plaintext definitions does not establish that all five outputs came from the
native wheel's eSpeak revision. Do not label that revision as proven data source.
If matching evidence is unavailable, selecting reproducibly generated replacement
data would be a separate artifact change with new hashes and synthesis checks.

This unresolved mapping concerns those five outputs. The two plaintext files
have the verified source matches above. Keep direct upstream downloading,
source-only publication and downstream binary/data redistribution distinct.
The native component source pins are recorded in
[the sherpa mapping](../sherpa-onnx/SOURCE-MATERIALS.md). Final installer/source
identity and package verification follow the distribution guide.
