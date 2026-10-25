**Premise broken by Parakeet — fixture became a passthrough test.**

Original intent (pre-recording): assert the LLM does NOT camelCase "user name" into "userName" without an explicit casing keyword.

But Parakeet's real ASR output for the spoken "user name" is the single token "username" (already concatenated). So the LLM never sees the two-word form. The fixture now asserts a different thing: that the LLM leaves Parakeet's "username" alone (doesn't split it back to "user name" or otherwise restructure).

To revive the original test, re-record using a multi-word identifier Parakeet won't auto-concatenate (e.g. "max age" or "default port") and update both input.txt and expected.txt accordingly.
