"""Disposable Tk target used only by the Unit 08 native Windows probe."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tkinter as tk


def _write_status(path: Path, *, root: tk.Tk, entry: tk.Entry) -> None:
    value = entry.get()
    payload = {
        "pid": os.getpid(),
        "hwnd": int(root.winfo_id()),
        "length": len(value),
        "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "entry_focused": root.focus_get() is entry,
    }
    replacement = path.with_suffix(".next")
    replacement.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    replacement.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--command", type=Path, required=True)
    parser.add_argument("--role", choices=("context", "focus"), required=True)
    args = parser.parse_args()

    root = tk.Tk()
    root.title(f"Whisperstream Unit 08 {args.role}")
    root.geometry("520x160+80+80")
    entry = tk.Entry(root, width=56)
    entry.pack(padx=24, pady=48)
    entry.focus_set()
    last_command = ""

    def poll() -> None:
        nonlocal last_command
        try:
            command = args.command.read_text(encoding="utf-8").strip()
        except OSError:
            command = ""
        if command and command != last_command:
            last_command = command
            if command.startswith("focus:"):
                root.deiconify()
                root.lift()
                root.focus_force()
                entry.focus_force()
            elif command == "exit":
                root.destroy()
                return
            elif command == "clear":
                entry.delete(0, tk.END)
        try:
            _write_status(args.status, root=root, entry=entry)
        except (OSError, tk.TclError):
            pass
        root.after(50, poll)

    root.after(0, poll)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
