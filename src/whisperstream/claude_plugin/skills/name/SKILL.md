---
name: name
description: Set or replace this Claude session's name in the Whisperstream HUD. Run it with no name to clear.
disable-model-invocation: true
---

Set or replace this Claude session's name in the Whisperstream HUD.

Whisperstream reads the name argument when this command expands and applies it
directly, so you do not need to call anything or take any action to set it. The
name then appears in place of the session in the HUD, and is spoken before the
summary when Agent Speech is active.

Running the command with no name clears it and returns the session to
automatic naming.

If the command includes a further request, handle that request normally.
Otherwise, briefly confirm the name change.
