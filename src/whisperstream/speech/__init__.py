"""Gated local speech for the agent HUD.

Direct ONNX Runtime Kokoro, sherpa-onnx Supertonic, backend-authoritative
selection, single-owner playback, and the summarizer fallback chain used to
speak Claude Code agent responses aloud. Kokoro's owned English frontend uses
the bundled neural G2P model and lexicons without importing eSpeak, phonemizer,
Misaki, PyTorch, Transformers, spaCy, NLTK, or num2words at runtime.

**Import hygiene.** This package SHIPS in production builds as of 1.3.0 (its
``--nofollow-import-to`` exclusion was removed when Agent Speak launched), so the
old "never compiled in" guarantee no longer applies. The lazy-import discipline
still matters, for load time rather than for gating: every call site outside this
package MUST import submodules lazily so starting the app does not pull in
onnxruntime, sherpa-onnx, or sounddevice. Agent Speak's real gate is the default-off
``agent_speech_enabled`` / ``claude_hud_enabled`` config. This
``__init__.py`` deliberately imports NO submodules: call sites import
submodules directly (``from whisperstream.speech import controller``, etc.),
never through this package's namespace, so importing ``whisperstream.speech``
itself never pulls in either engine runtime, sounddevice, or any model I/O.

**Privacy.** Claude Code agent response text handled anywhere in this package
is transcript-class data (see the privacy rules
in ``text_injector.py``). No module here may log it -- every function
whose parameters or locals hold response text carries a
"TRANSCRIPT DATA -- DO NOT LOG" banner comment; only content-free metadata
(char counts, durations, fallback-stage names) may be logged.
"""
