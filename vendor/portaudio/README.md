# macOS PortAudio source pin

Apple Silicon builds PortAudio **v19.7.0 plus only the upstream CoreAudio
stop-deadlock fix**, matching the sounddevice 0.5.3 baseline without adopting
unrelated master changes:

- Base: `147dd722548358763a8b649b3e4b41dfffbcfbb6` (`v19.7.0`).
- Fix: [`1ca0eeedb678c3cca80f3306c474d69a7a4b5278`](https://github.com/PortAudio/portaudio/commit/1ca0eeedb678c3cca80f3306c474d69a7a4b5278),
  upstream [#1175](https://github.com/PortAudio/portaudio/pull/1175) for
  [#1174](https://github.com/PortAudio/portaudio/issues/1174).
- Resulting Git tree: `aac4dc5b8eb81d25ff0f1cd9be344601db2a3bf3`.

The listener defers property queries outside CoreAudio's IO-thread locks.
Finished callbacks remain exactly once; close drains the deferred handler
before releasing its resources. The fix changes only `pa_mac_core.c` and
`pa_mac_core_internal.h` against the release baseline.

`scripts/prepare_macos_portaudio.py` fetches the two immutable commits from
`https://github.com/PortAudio/portaudio.git`, checks out the base, applies the
fix without a new commit, and verifies the resulting tree before compiling.
No GitHub-generated archive hash is used. Preparation uses CMake 3.31.6 through
`uv tool run`, Apple clang, and Release (`-O3 -DNDEBUG`) for arm64/macOS 13+.
Caller compiler flags are removed, and both build and cached verification fail
if `nm -u` finds `___assert_rtn`. This preserves the shipped library's disabled
assertions rather than risking an engine abort in an unexpected IO condition.
Only the shared library is built; no tests, examples or static library are built.

Preparation replaces the target virtual environment's wheel library atomically,
without modifying uv's hardlinked cache. No system library, installed app,
version number, installer or distribution artifact is changed. Git, uv and Xcode
command-line tools are required; uv obtains the pinned CMake tool automatically.

The script also applies a checked macOS-only loader patch to the locked
sounddevice 0.5.3 Python module. It skips system-library discovery on Darwin so
a user's Homebrew installation cannot override the fixed bundled library.
Windows retains the upstream loading path. The original module SHA-256 is
`db3bfdb3b71c934a6fb169aab41e61a90fd1d1491aa03fccc97e8e895d3c2b58`;
an unknown module fails preparation rather than receiving a speculative patch.

The environment receipt records base and fix commits, resulting source tree,
CMake release settings, original and patched loader hashes, library hash,
architecture and deployment target. The
Mac builder retains it in its logs and build report. A fresh interpreter verifies
the actual library selected by sounddevice. `--check` performs verification only;
ordinary preparation reuses a matching verified installation or rebuilds it.
Reinstalling sounddevice requires rerunning preparation. Review this pin and the
loader patch when updating that dependency.

PortAudio retains its upstream MIT-style license, already included in
`scripts/license-texts/portaudio.txt` and generated third-party notices.
sounddevice retains its MIT license. The exact upstream fix is applied to the
pinned release source; no locally authored PortAudio patch is carried.

This fixes a known native lock inversion. Keep the existing bounded capture
owner and quarantine for unrelated failures; neither a timeout nor a passing
stress test proves an arbitrary native resource has been released.
