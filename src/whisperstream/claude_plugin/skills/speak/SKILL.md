---
name: speak
description: Activate Whisperstream Agent Speech for this Claude session. Add a name to label it.
disable-model-invocation: true
---

Activate Whisperstream Agent Speech for this session.

For this response, and for every later response while Whisperstream supplies the
same instruction, end your final response with one concise, human-readable spoken
summary on its own final line. Begin that line with `🔊 ` (the speaker emoji
followed by one space). Keep the line visible so the response remains readable
without speech.

This final line is required on every response, not only long ones. Short
confirmations, follow-ups, and answers to a single question all end with it. If
you omit it, nothing is spoken.

If the command includes a request, handle that request normally. Otherwise,
briefly confirm that Agent Speech is active.
