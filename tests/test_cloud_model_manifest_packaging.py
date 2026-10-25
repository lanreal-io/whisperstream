from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_wheel_includes_bundled_cloud_manifest():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"cloud-models.json"' in pyproject


def test_nuitka_includes_bundled_cloud_manifest():
    build = (ROOT / "scripts" / "build.py").read_text(encoding="utf-8")
    assert (
        "--include-data-files=src/whisperstream/cloud-models.json="
        "whisperstream/cloud-models.json"
    ) in build
