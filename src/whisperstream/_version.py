"""Build-time version constant.

`scripts/build.sh` overwrites this file before invoking Nuitka, embedding the
version from `src-tauri/tauri.conf.json` into the frozen sidecar. The sentinel
value below is what's checked into git; the build script restores it after the
build so dev work doesn't see a dirty working tree.

In dev mode (running from source, not Nuitka), `_read_software_version()` in
`server.py` falls back to reading `tauri.conf.json` directly when this module
still holds the sentinel.
"""

__version__ = "0.0.0+dev"
