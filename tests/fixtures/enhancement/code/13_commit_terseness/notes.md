**Premise broken by Parakeet — fixture became a passthrough test.**

Original intent (pre-recording): assert the LLM does NOT capitalize, add a period, or rephrase commit-message-style output ("fix null check in auth handler" should stay terse and lowercase).

But Parakeet's real ASR output is already capitalized + punctuated ("Fix null check in auth handler."). The LLM has no signal that this is intended as a commit message — without an explicit "commit:" prefix or similar trigger, it should leave Parakeet's output alone.

The fixture now asserts the LLM doesn't over-process clean Parakeet output. If commit-message-style output is genuinely wanted, that's a separate mode (code-commit) rather than a chat/code fixture.
