Strip leading filler "Okay, so" while preserving "sorry" as a real apology at the end.

**Scope note:** The original fixture (pre-recording) also tested "um" filler removal. That part is moot — Whisperstream's word_filters config strips "um" from the transcription before the LLM ever sees it (see config_manager.py), and Parakeet's recorded output here doesn't contain "um" at all. The LLM's job here is just the "Okay, so" → "" transform.
