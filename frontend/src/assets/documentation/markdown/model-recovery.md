# Install and recover models offline

Whisperstream 2.0.0.

Whisperstream can use model files that you download yourself or copy from another installation. Use this guide if an in-app download fails, a model link stops working, or you want to prepare a new computer without connecting Whisperstream to the internet.

You need **one speech-recognition model** to dictate. **Local enhancement is optional** and can use Stream-1 Mini or Customizable. Customizable has two supported Gemma artifacts; you only need one of them.

This guide covers the exact model files supported by Whisperstream. Paths use Windows examples; see [Restore models on a Mac](model-recovery.md#restore-models-on-a-mac) for Mac folders and the one model that differs. The PDF and Markdown documentation do not include the model weights. Keep a separate backup of the model files and your installer or source revision and build instructions.

## Model download sources

In-app model downloads use pinned files from Hugging Face. Supported
speech-recognition models also have the GitHub backups described below. Existing
compatible cached files stay usable. The upstream links in this guide identify
the exact supported artifacts for manual recovery.

## Automatic download backups

For Parakeet v2/v3, Qwen and Cohere, Whisperstream first tries the pinned Hugging Face files. If that source remains unavailable after retries, it tries the matching upstream sherpa-onnx GitHub release archive linked below. The app verifies the archive against a checksum shipped with the app before unpacking it, then verifies the required model files before installation. It preserves existing nonempty files. Cancellation, disk errors, checksum mismatches and download-policy failures stop the operation instead of switching sources.

Customizable first downloads Unsloth's pinned Gemma 4 E2B Q4_K_M file. If that source remains unavailable, it automatically tries Google's official pinned QAT Q4_0 file through Hugging Face. These are different artifacts with different checksums and may produce different results. The app verifies the downloaded file and checks the artifact it will actually use. Cancellation, disk errors, checksum or size mismatches and download-policy failures stop the operation instead of switching sources. Stream-1 Mini has no automatic backup. If the automatic sources fail, use the manual recovery steps below. Only the exact supported artifacts listed here are recognized; a similar name does not make another file compatible.

## Choose what to restore

| What you need | Model | Required format | Approximate space for model files |
| --- | --- | --- | --- |
| English dictation | NVIDIA Parakeet TDT 0.6B v2 | sherpa-onnx INT8 ONNX bundle | 661 MB |
| Dictation in Parakeet's supported non-English languages | NVIDIA Parakeet TDT 0.6B v3 | sherpa-onnx INT8 ONNX bundle | 670 MB |
| Dictation in Qwen's supported languages | Qwen3-ASR 0.6B | sherpa-onnx INT8 ONNX bundle | 987 MB |
| Optional recognition in Cohere's supported languages | Cohere Transcribe, 14-language export | sherpa-onnx INT8 ONNX bundle | 2.9 GB |
| Customizable local cleanup | Google Gemma 4 E2B Instruct | GGUF, Q4_K_M | 3.1 GB |
| Automatic backup for Customizable | Google Gemma 4 E2B Instruct QAT | GGUF, Q4_0 | 3.35 GB |
| English dictation cleanup (default) | Stream-1 Mini | GGUF, Q4_K_M | 484 MB |

Sizes use decimal MB and GB. Windows may display different numbers. Allow additional space if you download an archive and then extract it. Disk space alone does not determine whether a model will run well on your computer.

For the full language list, see [Languages and models](languages-models.md).

## Find the correct storage folder

With the default settings, Whisperstream stores models under:

```text
%LOCALAPPDATA%\Whisperstream
```

Paste that path into File Explorer's address bar. Windows expands `%LOCALAPPDATA%` to the current user's local application-data folder. Create the folder if it does not exist yet.

Speech models go inside its `models` folder. Enhancement models go inside `local-llm`, next to `models`:

```text
Whisperstream\
  models\
    nemo-parakeet-tdt-0.6b-v2-int8\
    nemo-parakeet-tdt-0.6b-v3-int8\
    qwen3-asr-0.6b-int8\
    cohere-transcribe-03-2026-int8\
  local-llm\
    gemma-4-E2B-it-Q4_K_M.gguf
    gemma-4-E2B_q4_0-it.gguf
    stream-1-mini-vocabulary-q4_k_m.gguf
```

Only create the folders for models you want to install.

**If you changed Model storage location:** use the location shown under **Home > Advanced > Model storage location** as the base instead. For example, if you selected `D:\WhisperstreamModels`, speech models belong in `D:\WhisperstreamModels\models\...` and enhancement files belong in `D:\WhisperstreamModels\local-llm\...`. Do not add another `Whisperstream` folder beneath that base.

**If you use a custom data directory:** `WHISPERSTREAM_DATA_DIR` changes the default base. A saved custom model-storage location still takes precedence. For a normal new installation without either override, use the `%LOCALAPPDATA%` paths in this guide.

## Restore models on a Mac

On a Mac, the default base folder is `~/Library/Application Support/Whisperstream`. In Finder, choose **Go > Go to Folder** and enter the path. Use the same `models` and `local-llm` layout shown above:

```text
~/Library/Application Support/Whisperstream/
  models/
    nemo-parakeet-tdt-0.6b-v2-int8/
    qwen3-asr-0.6b-int8/
    cohere-transcribe-03-2026-int8/
  local-llm/
    gemma-4-E2B-it-Q4_K_M.gguf
    gemma-4-E2B_q4_0-it.gguf
    stream-1-mini-vocabulary-q4_k_m.gguf
```

Parakeet v2, Qwen3-ASR, Cohere Transcribe, Gemma 4 E2B, and Stream-1 Mini use the same files and checksums as Windows.

Parakeet v3 is different on a Mac. It runs on an Apple silicon engine and uses a Core ML version of the model, not the ONNX bundle in this guide. The ONNX files cannot replace it. Whisperstream downloads it from [FluidInference/parakeet-tdt-0.6b-v3-coreml](https://huggingface.co/FluidInference/parakeet-tdt-0.6b-v3-coreml/tree/aed02740059203c4a87495924f685de3722ae9ce) at revision `aed02740059203c4a87495924f685de3722ae9ce` and checks every file before use. It is stored in:

```text
~/Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3/
```

If you moved it with **Model storage location**, it is under the folder you chose. To restore it without downloading, copy the complete `parakeet-tdt-0.6b-v3` folder from a working Mac's backup into that location. Otherwise, let Whisperstream download it again.

To check a file's SHA256 on a Mac, run `shasum -a 256` followed by the file path in Terminal.

## Install or restore the files

1. **Quit Whisperstream from its tray menu,** or its menu bar icon on a Mac. Closing its window only hides the app. Wait for any model download to stop before copying files.
2. **Obtain the matching files.** Use your backup, the pinned source linked in the model's section, or the listed upstream archive. You can download on another computer and transfer the files using an external drive.
3. **Extract archives before copying.** Copy the required files into the exact destination below. Preserve Qwen's `tokenizer` subfolder. Avoid adding an extra archive-named folder between the destination and the files.
4. **Check the files.** Compare their sizes and SHA256 checksums with the reference at the end of this guide. Verify every manual copy, including files recovered from an interrupted download.
5. **Start Whisperstream.** Choose the corresponding language and speech model, or complete Local enhancement setup for the cleanup model you restored.

For a new offline installation, install the app, leave it closed, and place the files before first launch. During onboarding, choose a language supported by the speech model you copied. Automatic selection expects Parakeet v2 for English, Parakeet v3 for its other supported languages, or Qwen for languages outside Parakeet's range. If you intend to use Cohere, finish language setup with the recommended model available, then choose Cohere on Home.

Whisperstream checks the expected filenames in its model folders. It reuses existing nonempty files and downloads missing or empty files when you request a model. It does not search Downloads, scan arbitrary folders, or import an archive you select. Its automatic GitHub backup unpacks only the approved archive through the verified downloader.

**A file being detected does not prove it is complete.** Existing files are not fully rehashed during ordinary detection. An interrupted file can be nonempty and still fail to load. If that happens, quit the app and replace the affected file with a verified copy. Do not replace a working model merely to fix an unrelated failed download.

## NVIDIA Parakeet v2: English dictation

NVIDIA Parakeet TDT 0.6B v2, converted for sherpa-onnx with INT8 ONNX encoder, decoder and joiner.

**Destination folder**

```text
%LOCALAPPDATA%\Whisperstream\models\nemo-parakeet-tdt-0.6b-v2-int8\
```

**Required files**

```text
encoder.int8.onnx
decoder.int8.onnx
joiner.int8.onnx
tokens.txt
```

**Pinned source:** [csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8/tree/1ab9323565ddb038682214b292f588070a538ce2)

**Revision:** `1ab9323565ddb038682214b292f588070a538ce2`

**Automatic backup and manual recovery source:** [sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2) on the sherpa-onnx GitHub release. Check the extracted required files against the checksums below before using them.

**Use it:** On Home, choose English. Automatic selection uses this exact v2 model.

## NVIDIA Parakeet v3: multilingual dictation

NVIDIA Parakeet TDT 0.6B v3, converted for sherpa-onnx with INT8 ONNX encoder, decoder and joiner. It supports 25 languages, including English.

**Destination folder**

```text
%LOCALAPPDATA%\Whisperstream\models\nemo-parakeet-tdt-0.6b-v3-int8\
```

**Required files**

```text
encoder.int8.onnx
decoder.int8.onnx
joiner.int8.onnx
tokens.txt
```

**Pinned source:** [csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8](https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8/tree/2bda32ec70b097a55adaa07d9a7173915b43cc78)

**Revision:** `2bda32ec70b097a55adaa07d9a7173915b43cc78`

**Automatic backup and manual recovery source:** [sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2) on the sherpa-onnx GitHub release. Check the extracted required files against the checksums below before using them.

**Use it:** On Home, choose a supported non-English language. Automatic selection uses v3 for those languages. A new English setup expects v2; copying v3 alone does not satisfy that v2 download check.

## Qwen3-ASR: multilingual dictation

Qwen3-ASR 0.6B, using the sherpa-onnx INT8 export dated 2026-03-25. This is the speech-recognition model, not a Qwen text-generation model.

**Destination folder**

```text
%LOCALAPPDATA%\Whisperstream\models\qwen3-asr-0.6b-int8\
```

**Required files**

```text
conv_frontend.onnx
encoder.int8.onnx
decoder.int8.onnx
tokenizer/vocab.json
tokenizer/merges.txt
tokenizer/tokenizer_config.json
```

Keep the three tokenizer files inside `tokenizer`. Do not flatten that subfolder or substitute the older export from a different repository.

**Pinned source:** [csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25](https://huggingface.co/csukuangfj2/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25/tree/68818b2313fe77bd06f6a7c5068ff3ef59d02b8a)

**Revision:** `68818b2313fe77bd06f6a7c5068ff3ef59d02b8a`

**Automatic backup and manual recovery source:** [sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-qwen3-asr-0.6B-int8-2026-03-25.tar.bz2) on the sherpa-onnx GitHub release. Check the extracted required files against the checksums below before using them.

**Use it:** On Home, choose a supported language. Automatic selection uses Qwen when Parakeet does not support that language. To use Qwen for another compatible language, open Advanced, turn on Choose speech model manually, and choose Qwen3 ASR.

## Cohere Transcribe: optional dictation model

Cohere Transcribe, using the sherpa-onnx 14-language INT8 ONNX export dated 2026-04-01.

**Destination folder**

```text
%LOCALAPPDATA%\Whisperstream\models\cohere-transcribe-03-2026-int8\
```

**Required files**

```text
encoder.int8.onnx
encoder.int8.onnx.data
decoder.int8.onnx
tokens.txt
```

Keep `encoder.int8.onnx.data` beside `encoder.int8.onnx`. The `.data` file contains most of the encoder weights and is required. The local folder name contains `03-2026`, even though the export source is dated `2026-04-01`. Use the destination shown above.

**Pinned source:** [csukuangfj2/sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01](https://huggingface.co/csukuangfj2/sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01/tree/156a470cf08eefe706a0004f3c52d9ee567ca7a0)

**Revision:** `156a470cf08eefe706a0004f3c52d9ee567ca7a0`

**Automatic backup and manual recovery source:** [sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01.tar.bz2](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-cohere-transcribe-14-lang-int8-2026-04-01.tar.bz2) on the sherpa-onnx GitHub release. Check the extracted required files against the checksums below before using them.

**Use it:** On Home, choose a supported Dictation Language, then select Cohere Transcribe in the model picker. This enters Manual selection. Cohere uses the selected language for recognition. Automatic selection does not choose Cohere.

## Gemma 4 E2B: Customizable local enhancement

Google Gemma 4 E2B Instruct (the E2B-it model), using Unsloth's Q4_K_M GGUF. Whisperstream calls it Customizable in the Local model picker.

**Destination folder**

```text
%LOCALAPPDATA%\Whisperstream\local-llm\
```

**Required files**

```text
gemma-4-E2B-it-Q4_K_M.gguf
```

Download the single `Q4_K_M` file listed above. Whisperstream uses this model for text only, so no vision projector (`mmproj`) file is needed. Its runtime and chat template come with the app.

**Pinned source:** [unsloth/gemma-4-E2B-it-GGUF](https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/tree/0314792d7f1f7e229411f620751375812bb9faf2)

**Revision:** `0314792d7f1f7e229411f620751375812bb9faf2`

**Use it:** Open Enhancement, choose Local, and run the speed check for Customizable (Gemma 4 E2B). This file is for text cleanup; it does not replace a speech-recognition model.

## Gemma 4 E2B QAT: automatic Google backup

Google's official Gemma 4 E2B Instruct QAT Q4_0 GGUF is the automatic backup for Customizable when its primary source is unavailable. Its weights, size and checksum differ from Unsloth's Q4_K_M file above, and results may differ. Both appear as the same Customizable choice in the app.

**Destination folder**

```text
%LOCALAPPDATA%\Whisperstream\local-llm\
```

**Required files**

```text
gemma-4-E2B_q4_0-it.gguf
```

Keep this exact filename. Do not rename it to the Unsloth model's filename or overwrite that model. No vision projector (`mmproj`) file is needed for text cleanup; the app supplies the runtime and chat template.

**Pinned source:** [google/gemma-4-E2B-it-qat-q4_0-gguf](https://huggingface.co/google/gemma-4-E2B-it-qat-q4_0-gguf/tree/675cff42a74c774d6cb76f76d8eacb49b48c9b93)

**Revision:** `675cff42a74c774d6cb76f76d8eacb49b48c9b93`

**Use it:** Open Enhancement, choose Local, and run the speed check for **Customizable**. The app checks the Gemma artifact it will use; a result for the other artifact does not qualify this file. An automatic Google fallback remembers that file. If no saved artifact choice exists and both files are present, the app prefers Unsloth. If only one supported file is present, it uses that file. Keep the file you intend to use and verify its checksum; do not overwrite or rename a working model to force a different variant.

## Stream-1 Mini: local enhancement

Whisperstream's own dictation cleanup model, fine-tuned from Qwen3-0.6B-Base. It is the default local enhancement model.

**Destination folder**

```text
%LOCALAPPDATA%\Whisperstream\local-llm\
```

**Required files**

```text
stream-1-mini-vocabulary-q4_k_m.gguf
```

Keep the exact filename, including lowercase `q4_k_m`. The vocabulary revision has a distinct filename so an older E model is not reused with new prompts. Do not rename an older Mini file to this name. Plain Qwen3-0.6B is not Stream-1 Mini. Whisperstream supplies the model's built-in cleanup prompts and settings.

**Pinned source:** [whisperstream/stream-1-mini](https://huggingface.co/whisperstream/stream-1-mini/tree/ee30dd90bd346d781d0e420d3c6c23244798e7b9)

**Revision:** `ee30dd90bd346d781d0e420d3c6c23244798e7b9`

**Use it:** Open Enhancement and choose Local. The speed check runs for Stream-1 Mini. It supports English Chat and Email cleanup. It does not provide speech recognition or Code-profile enhancement.

## If a model link stops working

Try these options in order:

1. **Restore your own backup.** A verified copy from a working installation is sufficient. You do not need to register its download with Whisperstream.
2. **Look for the exact export or filename.** The repository names, revisions and file lists above identify the expected artifacts even if the original page moves. For speech models, the linked sherpa-onnx release archives provide another place to look.
3. **Verify a mirror against this guide.** Matching SHA256 values establish that the required files are the same bytes as the supported payload. Keep accompanying model licenses and notices with your backup.

A similar model name is not enough. A PyTorch or Safetensors download cannot replace an ONNX bundle or GGUF file. FP16, another quantization, a different model size, a different ONNX conversion, or a newer revision may require app changes. Renaming those files does not make them compatible.

If the exact payload is unavailable, choose another supported model for your language. If only enhancement is affected, leave AI Enhancement off and continue using ordinary dictation. Whisperstream's built-in download links cannot be changed in Settings; manually copying the supported files avoids relying on those links.

## Use one local enhancement model

Stream-1 Mini and Customizable can each be set up alone. Customizable needs one of the supported Gemma files, not both.

1. Copy the GGUF file for the model you want into `local-llm`.
2. Restart Whisperstream and open **Enhancement**.
3. Choose **Local**. Turning on local cleanup checks Stream-1 Mini. To use Customizable instead, choose **Customizable** in the model picker; Whisperstream checks only that model.
4. Select **Start speed check**. Whisperstream reuses the file you copied and checks that model on your computer. It does not download the other model.
5. Follow the result to select the model. A passing result lets you use it. A slow-but-usable result requires **Use it anyway**. A model that is too slow has no override.

Each model needs its own speed check on the computer where you will use it. Restoring a GGUF file does not transfer another computer's performance result. A failed download does not mean your computer is too slow, and one model's result does not qualify the other.

The app supplies the local `llama-server` engine. If the engine itself is missing or damaged, restore or reinstall the official app. Copying more GGUF files will not repair it. If local cleanup cannot run, turn off **AI Enhancement** to keep dictating with your installed speech model.

## Troubleshoot a restored model

| What you see | What to check |
| --- | --- |
| Whisperstream still offers a download | Confirm the selected model and version, the model-storage base, exact folder name, and every required file. A v3 folder does not satisfy a v2 selection. Restart after copying files. |
| A model is detected but will not load | Check the SHA256 values. The files may be incomplete, from different exports, or the wrong format. |
| Cohere will not load | Check that the large `encoder.int8.onnx.data` file is present beside its ONNX file. |
| Qwen will not load | Check `conv_frontend.onnx` and all three files under `tokenizer`. |
| Files are unexpectedly tiny | Check that you downloaded the actual weights, not a Git LFS pointer, web page, or shortcut. |
| Local cleanup tries to download an unwanted model | Cancel and start a speed check for only the model you have installed. |
| Recognition uses the wrong language | Check Dictation Language. Cohere requires the chosen language; keep each recording to one language. |
| Dictation works but cleanup does not | Speech recognition and enhancement use different models. Verify the GGUF location and complete that cleanup model's speed check. |

## Verify downloaded files

Windows PowerShell can calculate SHA256 without installing another tool. For example, to check Stream-1 Mini in the default location:

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath "$env:LOCALAPPDATA\Whisperstream\local-llm\stream-1-mini-vocabulary-q4_k_m.gguf"
```

Compare the returned `Hash` with the reference below. Uppercase and lowercase letters mean the same thing. Substitute the actual file path when checking another model or a custom storage location. Hashing a large file can take a little time.

The byte sizes below help spot an incomplete download, but matching size alone is not verification. The checksums apply to each extracted file, not to a containing ZIP or tar archive.

### Checksums: NVIDIA Parakeet v2

**`encoder.int8.onnx`** (652,184,296 bytes)

```text
a32b12d17bbbc309d0686fbbcc2987b5e9b8333a7da83fa6b089f0a2acd651ab
```

**`decoder.int8.onnx`** (7,257,753 bytes)

```text
b6bb64963457237b900e496ee9994b59294526439fbcc1fecf705b31a15c6b4e
```

**`joiner.int8.onnx`** (1,739,080 bytes)

```text
7946164367946e7f9f29a122407c3252b680dbae9a51343eb2488d057c3c43d2
```

**`tokens.txt`** (9,384 bytes)

```text
ec182b70dd42113aff6c5372c75cac58c952443eb22322f57bbd7f53977d497d
```

### Checksums: NVIDIA Parakeet v3

**`encoder.int8.onnx`** (652,184,281 bytes)

```text
acfc2b4456377e15d04f0243af540b7fe7c992f8d898d751cf134c3a55fd2247
```

**`decoder.int8.onnx`** (11,845,275 bytes)

```text
179e50c43d1a9de79c8a24149a2f9bac6eb5981823f2a2ed88d655b24248db4e
```

**`joiner.int8.onnx`** (6,355,277 bytes)

```text
3164c13fc2821009440d20fcb5fdc78bff28b4db2f8d0f0b329101719c0948b3
```

**`tokens.txt`** (93,939 bytes)

```text
d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d
```

### Checksums: Qwen3-ASR

**`conv_frontend.onnx`** (44,148,281 bytes)

```text
d22dc4423e0940e49884e903d2ea2f7e5567c14fc1aed97e4e26d6b8f208ef9e
```

**`encoder.int8.onnx`** (182,491,662 bytes)

```text
60748d3e6744a57c9c91e1b17424a6c2990567e8adceb0783940c03ed98fa9d9
```

**`decoder.int8.onnx`** (755,914,231 bytes)

```text
4f6885be5959ae26af3089d38ee7972c5fafbeeb1cf8d5e76eab6d8b61ca5771
```

**`tokenizer/vocab.json`** (2,776,833 bytes)

```text
ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910
```

**`tokenizer/merges.txt`** (1,671,853 bytes)

```text
8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5
```

**`tokenizer/tokenizer_config.json`** (12,487 bytes)

```text
4942d005604266809309cabc9f4e9cb89ce855d59b14681fdc0e1cc62ea26c4c
```

### Checksums: Cohere Transcribe

**`encoder.int8.onnx`** (3,090,822 bytes)

```text
cf704f8cfa90e3f0a76f9ffc05998bdf00ba9ae983192c14a85a3a5eb008b367
```

**`encoder.int8.onnx.data`** (2,731,503,072 bytes)

```text
bcf1b7148c8518ae52df1ad2d2fc2b4e89261ea23e6c874eef1d9f55bcbaa4a3
```

**`decoder.int8.onnx`** (153,250,705 bytes)

```text
8372ca6c8ff4db8b916ca3592f5c757a715e691b9edec751ba19b29fc854baf9
```

**`tokens.txt`** (207,437 bytes)

```text
013ede043ae2480e3a9205cc34550d9686100cc682bacc90f702facdfbb93035
```

### Checksums: Gemma 4 E2B

**`gemma-4-E2B-it-Q4_K_M.gguf`** (3,106,738,272 bytes)

```text
740185b21d22ceb83a11c3aa62ad5842ef32c70f6096d756bbee85a1e4ec34b8
```

### Checksums: Gemma 4 E2B QAT (Google)

`gemma-4-E2B_q4_0-it.gguf` (3,349,516,256 bytes)

```text
fa401b55b07ee70a54c6dae3903c783a6e65064312529ea57175cb5f8dec6634
```

### Checksums: Stream-1 Mini

**`stream-1-mini-vocabulary-q4_k_m.gguf`** (484,215,648 bytes)

```text
32bef61bdbc98787ac278d96a2aad120a9fc61f588f1f0a4746441e9d6d89ae7
```

## Keep a recovery backup

After a successful setup, quit Whisperstream and copy the model folders you use to an external drive. Keep them with your installer or source revision, build instructions and this documentation. Back up both `models` and `local-llm` if you use local cleanup. On a new computer, restore the files to that computer's model-storage location and complete its own setup and enhancement speed check.
