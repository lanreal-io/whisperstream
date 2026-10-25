"""Restore exact native inputs from an existing local wheel archive, without network."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def restore(source: Path, destination: Path = ROOT / '.local-dependencies' / 'sherpa-onnx') -> None:
    records = json.loads((ROOT / 'scripts' / 'sherpa-wheel-inputs.json').read_text())
    # Validate the complete set before modifying the destination.
    for row in records:
        wheel = source / row['filename']
        if not wheel.is_file():
            raise ValueError(f"Missing archived wheel: {wheel.name}")
        with wheel.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != row['sha256'] or wheel.stat().st_size != row['size']:
            raise ValueError(f"Archived wheel checksum mismatch: {wheel.name}")
    destination.mkdir(parents=True, exist_ok=True)
    for row in records:
        wheel = source / row['filename']
        target = destination / row['filename']
        if wheel.resolve() != target.resolve():
            shutil.copy2(wheel, target)
    print(f"Verified {len(records)} exact native wheels in {destination}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    try:
        restore(args.archive)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'{exc}\n')
