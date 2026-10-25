## Choose a dictation language

Open **Home**, select **Dictation Language**, and choose the language you plan to speak. Whisperstream uses Automatic selection by default: it chooses the recommended speech-recognition model and, if needed, asks before downloading it. Wait for the download to finish before dictating.

The model currently in use appears under **Voice Recognition Model**. Automatic selection uses:

- NVIDIA Parakeet v2 for English.
- NVIDIA Parakeet v3 for supported non-English languages.
- Qwen3 ASR when Parakeet does not support the selected language.

If your current model also supports a new language, Whisperstream may ask whether to keep it or switch to the recommendation. Keeping it turns on Manual selection. If a manually selected model does not support the new language, Whisperstream returns to Automatic and offers the compatible recommendation.

> Important: Keep each recording to one language. Parakeet and Qwen3 ASR normally detect the language for each recording. Cohere Transcribe always uses your selected Dictation Language. Changing languages mid-recording can reduce accuracy.

## Compare speech-recognition models

| Model | Used for | Languages | Approximate download |
|---|---|---|---|
| NVIDIA Parakeet v2 | Default English recognition | English | 661 MB |
| NVIDIA Parakeet v3 | Default recognition for supported non-English languages | 25 languages | 670 MB |
| Qwen3 ASR | Recognition for languages outside Parakeet's range | 30 languages | 987 MB |
| Cohere Transcribe | Optional Manual selection | 14 languages | 2.9 GB |

After the selected model is downloaded, speech recognition runs on your computer without an internet connection. Selecting a language or model that needs another download opens a confirmation first.

These models produce the raw transcript. For optional cleanup after recognition, see [Enhancement providers](/docs/enhancement/providers).

## Choose a model manually

Automatic is recommended for most users. To choose a compatible model yourself:

1. On **Home**, open the model picker when alternatives are available.
2. Choose a compatible model. Choosing one enters Manual selection.
3. Open **Advanced** and turn on **Choose speech model manually** to reveal additional compatible choices.

The picker appears when more than one model supports the selected language. Cohere Transcribe is available only through Manual selection and requires more memory than the other choices. Choosing Parakeet automatically uses its English or multilingual version as appropriate.

On a Mac, Parakeet v3 runs on an engine built for Apple silicon and downloads in a different format, so its download size differs from the table above. Parakeet v2, Qwen3 ASR, and Cohere Transcribe use the same model files on Windows and Mac. Turn off **Choose speech model manually** to return to the recommended model for your language.

Whisperstream keeps downloaded models when you switch, so switching back does not require another download. To remove one, open the model picker and use **Delete** beside a downloaded model that is not in use. To remove every unused speech-recognition model, open **Advanced** and select **Delete unused models** under **Model storage location**. A deleted model can be downloaded again later.

## Supported languages

Whisperstream offers 39 dictation languages. **Default model** shows what Automatic selection recommends. Where multiple models are listed, Manual selection lets you choose among them. Cohere Transcribe is never selected automatically.

| Language | Supported models | Default model |
|---|---|---|
| Arabic | Qwen3 ASR, Cohere Transcribe | Qwen3 ASR |
| Bulgarian | Parakeet v3 | Parakeet v3 |
| Cantonese | Qwen3 ASR | Qwen3 ASR |
| Chinese | Qwen3 ASR, Cohere Transcribe | Qwen3 ASR |
| Croatian | Parakeet v3 | Parakeet v3 |
| Czech | Parakeet v3, Qwen3 ASR | Parakeet v3 |
| Danish | Parakeet v3, Qwen3 ASR | Parakeet v3 |
| Dutch | Parakeet v3, Qwen3 ASR, Cohere Transcribe | Parakeet v3 |
| English | Parakeet v2, Qwen3 ASR, Cohere Transcribe | Parakeet v2 |
| Estonian | Parakeet v3 | Parakeet v3 |
| Filipino | Qwen3 ASR | Qwen3 ASR |
| Finnish | Parakeet v3, Qwen3 ASR | Parakeet v3 |
| French | Parakeet v3, Qwen3 ASR, Cohere Transcribe | Parakeet v3 |
| German | Parakeet v3, Qwen3 ASR, Cohere Transcribe | Parakeet v3 |
| Greek | Parakeet v3, Qwen3 ASR, Cohere Transcribe | Parakeet v3 |
| Hindi | Qwen3 ASR | Qwen3 ASR |
| Hungarian | Parakeet v3, Qwen3 ASR | Parakeet v3 |
| Indonesian | Qwen3 ASR | Qwen3 ASR |
| Italian | Parakeet v3, Qwen3 ASR, Cohere Transcribe | Parakeet v3 |
| Japanese | Qwen3 ASR, Cohere Transcribe | Qwen3 ASR |
| Korean | Qwen3 ASR, Cohere Transcribe | Qwen3 ASR |
| Latvian | Parakeet v3 | Parakeet v3 |
| Lithuanian | Parakeet v3 | Parakeet v3 |
| Macedonian | Qwen3 ASR | Qwen3 ASR |
| Malay | Qwen3 ASR | Qwen3 ASR |
| Maltese | Parakeet v3 | Parakeet v3 |
| Persian | Qwen3 ASR | Qwen3 ASR |
| Polish | Parakeet v3, Qwen3 ASR, Cohere Transcribe | Parakeet v3 |
| Portuguese | Parakeet v3, Qwen3 ASR, Cohere Transcribe | Parakeet v3 |
| Romanian | Parakeet v3, Qwen3 ASR | Parakeet v3 |
| Russian | Parakeet v3, Qwen3 ASR | Parakeet v3 |
| Slovak | Parakeet v3 | Parakeet v3 |
| Slovenian | Parakeet v3 | Parakeet v3 |
| Spanish | Parakeet v3, Qwen3 ASR, Cohere Transcribe | Parakeet v3 |
| Swedish | Parakeet v3, Qwen3 ASR | Parakeet v3 |
| Thai | Qwen3 ASR | Qwen3 ASR |
| Turkish | Qwen3 ASR | Qwen3 ASR |
| Ukrainian | Parakeet v3 | Parakeet v3 |
| Vietnamese | Qwen3 ASR, Cohere Transcribe | Qwen3 ASR |

## If recognition chooses the wrong language

1. Confirm that **Dictation Language** matches the language you are speaking.
2. Use one language for the entire recording.
3. If Qwen3 ASR repeatedly detects the wrong language, turn on **Choose speech model manually**, select Qwen3 ASR, then turn on **Force selected language**.

**Force selected language** appears only when Qwen3 ASR is manually selected. It tells Qwen which language to listen for instead of detecting it. Returning to Automatic or choosing another model clears this setting. Cohere Transcribe already uses your selected language and does not show this control. If its output is in the wrong language, check **Dictation Language** before recording again.
