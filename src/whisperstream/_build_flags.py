"""Build-time feature flags (compile-time gate for dev-only code).

The sentinel value checked into git is ``True`` so dev tools work when running
from source (dev servers, pytest). ``scripts/build.py`` overwrites this module to
``DEV_TOOLS_AVAILABLE = False`` before the Nuitka compile, then restores the
sentinel on exit (the same pattern used for ``_version.py``).

``DEV_TOOLS_AVAILABLE`` gates registration of the optional ``/api/dev/*``
dev-tools routes in ``server.py``. Source checkouts may not include the routes
module at all; the server then serves only the discovery probe. When the flag
is ``False``:

* the dev-tools routes module is never imported, so the routes never register
  (the ``/api/dev/enabled`` discovery probe still answers, always ``False``); and
* ``build.py`` additionally excludes that module from Nuitka with
  ``--nofollow-import-to``, so its compiled code is physically absent from the
  production binary.

This mirrors the frontend's compile-time gate (``import.meta.env.DEV``), which
dead-code-eliminates the Fixture Test tab from production bundles.

``EXPERIMENTAL_AVAILABLE`` is the second gate. It currently gates transcript
Re-enhance, whose backend route is not registered when this flag is false and
whose frontend action also requires the matching runtime probe.

It was built for the agent-speech work (Claude HUD + speech). Those
features SHIPPED in 1.3.0, so they were taken off this gate entirely: their
``--nofollow-import-to`` exclusions were removed from ``scripts/build.py``, the
per-route ``EXPERIMENTAL_AVAILABLE and _experimental_enabled()`` early returns were
deleted from ``server.py``, and their real gate is now user config, default-off
(``claude_hud_enabled``, ``agent_speech_enabled``).
They keep an "experimental" label in the UI, which is a product decision, not this flag.

The mechanism's default behavior keeps unfinished features out of production:
``scripts/build.py`` still flips
this to ``False`` (via ``_build_lib.disable_experimental()``) before the Nuitka compile,
so anything newly placed behind it is absent from a production binary. The exclusion
list it pairs with is simply empty today. ``server.py`` still serves the always-on
``/api/experimental/enabled`` probe, which therefore answers ``False`` in prod -- correct,
when the feature is unavailable. When you gate a new feature, add any isolated
module's ``--nofollow-import-to`` entry to ``scripts/build.py`` and check
``EXPERIMENTAL_AVAILABLE and _experimental_enabled()`` at its entry points.

Do NOT assume this flag still hides Agent Speak. It does not.
"""

DEV_TOOLS_AVAILABLE = True
"""Sentinel — True in source/dev/test, overwritten to False by build.sh for Nuitka."""

EXPERIMENTAL_AVAILABLE = True
"""Sentinel — True in source/dev/test, overwritten to False by build.py for Nuitka
(unless ``--experimental`` is passed, which keeps it True for personal dogfood builds)."""
