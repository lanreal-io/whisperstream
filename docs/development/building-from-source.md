# Build from source

You'll need Python 3.12 to 3.14 with [uv](https://docs.astral.sh/uv/), Node.js
22.13+, and Rust.

- **Windows 10 or 11 (x64):** also install the Rust MSVC toolchain and Visual
  Studio C++ Build Tools with the **Desktop development with C++** workload.
- **macOS (beta):** Apple Silicon with macOS 14 or later, native arm64 Python
  and Node, and the Xcode Command Line Tools.

From the repository root on Windows:

```powershell
uv sync --locked --extra dev
uv run --locked python scripts/check-sherpa-source.py
powershell -NoProfile -File scripts/run_dev.ps1
```

On macOS:

```sh
uv sync --locked --extra dev
uv run --locked python scripts/check-sherpa-source.py
bash scripts/run_dev.sh
```

The launcher installs the frontend, fetches the local cleanup engine, starts the
backend and opens the app. Keep its terminal open while you use the app. It
stores data in a separate development folder, so it won't touch an installed
copy. Speech models download during first-run setup.
