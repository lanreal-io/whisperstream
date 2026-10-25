"""Resolution policy for the reviewed official native dependency graph.

Installed native-file integrity is covered by test_sherpa_source. These checks
cover project/lock selection so a supported host cannot resolve another payload.
"""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from packaging.markers import Marker
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SHERPA_VERSION = "1.13.8"
SHERPA_PACKAGES = {"sherpa-onnx", "sherpa-onnx-core"}


def _toml(path: str) -> dict:
    return tomllib.loads((REPO_ROOT / path).read_text(encoding="utf-8"))


def _package(lock: dict, name: str) -> dict:
    matches = [package for package in lock["package"] if package["name"] == name]
    assert len(matches) == 1, f"expected exactly one locked {name} package"
    return matches[0]


def _matches(marker: str, environment: dict[str, str]) -> bool:
    return Marker(marker).evaluate(environment)


def test_project_pins_official_binding_and_core_without_private_indexes() -> None:
    project = _toml("pyproject.toml")
    dependencies = project["project"]["dependencies"]
    uv = project["tool"]["uv"]

    assert project["project"]["requires-python"] == ">=3.12,<3.15"
    assert {dep for dep in dependencies if dep.startswith("sherpa-")} == {
        f"{name}=={SHERPA_VERSION}" for name in SHERPA_PACKAGES
    }
    assert set(uv["sources"]) == SHERPA_PACKAGES
    assert not uv.get("index")
    assert not uv.get("index-url")
    assert not uv.get("extra-index-url")
    assert not uv.get("find-links")
    assert "pygame>=2.6.0; python_version < '3.14'" in dependencies
    assert "pygame-ce>=2.5.7; python_version >= '3.14'" in dependencies
    assert "pygame>=2.6.0" not in dependencies
    assert "pycaw>=20230407; sys_platform == 'win32'" in dependencies


@pytest.mark.parametrize(("system", "machine", "wheel_platform"), [
    ("win32", "AMD64", "win_amd64"),
    ("darwin", "arm64", "macosx_11_0_arm64"),
])
@pytest.mark.parametrize("minor", [12, 13, 14])
def test_supported_host_resolves_exact_reviewed_binding_and_matching_core(
    system, machine, wheel_platform, minor,
) -> None:
    project = _toml("pyproject.toml")
    lock = _toml("uv.lock")
    inventory = json.loads((REPO_ROOT / "scripts/sherpa-source-wheels.json").read_text(encoding="utf-8"))
    assert inventory["version"] == SHERPA_VERSION
    assert lock["requires-python"] == ">=3.12, <3.15"
    environment = {
        "sys_platform": system, "platform_machine": machine,
        "python_version": f"3.{minor}", "python_full_version": f"3.{minor}.1",
    }
    for markers in (project["tool"]["uv"]["environments"], lock["supported-markers"]):
        assert sum(_matches(marker, environment) for marker in markers) == 1
    root = _package(lock, "whisperstream")
    selected = {}
    for name in sorted(SHERPA_PACKAGES):
        abi = f"cp3{minor}-cp3{minor}" if name == "sherpa-onnx" else "py3-none"
        filename = f"{name.replace('-', '_')}-{SHERPA_VERSION}-{abi}-{wheel_platform}.whl"
        reviewed = [row for row in inventory["wheels"] if row["filename"] == filename]
        assert len(reviewed) == 1
        wheel = reviewed[0]
        assert wheel["distribution"] == name
        url = wheel["url"]
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "files.pythonhosted.org"
        assert parsed.path.endswith("/" + filename) and not parsed.query and not parsed.fragment
        sources = [row for row in project["tool"]["uv"]["sources"][name]
                   if _matches(row.get("marker", "python_version >= '0'"), environment)]
        assert len(sources) == 1 and set(sources[0]) == {"url", "marker"}
        assert sources[0]["url"] == url
        # Check both the resolver edge and lock metadata. The latter can retain
        # registry requirements for excluded platforms, but never this host.
        for key, rows in (("source", root["dependencies"]),
                          ("url", root["metadata"]["requires-dist"])):
            rows = [row for row in rows if row["name"] == name
                    and _matches(row.get("marker", "python_version >= '0'"), environment)]
            assert len(rows) == 1
            assert rows[0][key] == ({"url": url} if key == "source" else url)
            if key == "source":
                assert rows[0]["version"] == SHERPA_VERSION
        packages = [row for row in lock["package"] if row["name"] == name
                    and any(_matches(marker, environment) for marker in row.get("resolution-markers", ["python_version >= '0'"]))]
        assert len(packages) == 1
        package = packages[0]
        assert package["version"] == SHERPA_VERSION
        assert package["source"] == {"url": url}
        assert "sdist" not in package
        assert package["wheels"] == [{"url": url, "hash": "sha256:" + wheel["sha256"]}]
        selected[name] = package
    binding_core = selected["sherpa-onnx"]["dependencies"]
    assert len(binding_core) == 1
    assert binding_core[0]["name"] == "sherpa-onnx-core"
    assert binding_core[0]["version"] == SHERPA_VERSION
    assert binding_core[0]["source"] == selected["sherpa-onnx-core"]["source"]
    assert _matches(binding_core[0]["marker"], environment)


def test_native_graph_cannot_add_unreviewed_fallback_or_redirect_other_dependencies() -> None:
    project = _toml("pyproject.toml")
    lock = _toml("uv.lock")
    inventory = json.loads((REPO_ROOT / "scripts/sherpa-source-wheels.json").read_text(encoding="utf-8"))
    for name, count in (("sherpa-onnx", 6), ("sherpa-onnx-core", 2)):
        expected = {row["url"] for row in inventory["wheels"] if row["distribution"] == name}
        assert len(expected) == count
        sources = project["tool"]["uv"]["sources"][name]
        assert len(sources) == count
        assert {row["url"] for row in sources} == expected
        packages = [row for row in lock["package"] if row["name"] == name]
        assert len(packages) == count
        assert {row["source"]["url"] for row in packages} == expected
    for package in lock["package"]:
        if package["name"] in SHERPA_PACKAGES:
            continue
        expected_source = {"editable": "."} if package["name"] == "whisperstream" else {"registry": "https://pypi.org/simple"}
        assert package["source"] == expected_source, package["name"]


@pytest.mark.parametrize(("system", "machine"), [
    ("win32", "ARM64"), ("darwin", "x86_64"), ("linux", "x86_64"),
])
def test_unsupported_platforms_are_excluded_from_project_and_lock(system, machine) -> None:
    environment = {"sys_platform": system, "platform_machine": machine}
    for markers in (_toml("pyproject.toml")["tool"]["uv"]["environments"],
                    _toml("uv.lock")["supported-markers"]):
        assert not any(_matches(marker, environment) for marker in markers)


def test_lock_selects_a_binary_pygame_provider_for_each_supported_python() -> None:
    lock = _toml("uv.lock")
    pygame = _package(lock, "pygame")
    pygame_ce = _package(lock, "pygame-ce")
    project = _package(lock, "whisperstream")
    requirements = project["metadata"]["requires-dist"]

    assert pygame["version"] == "2.6.1"
    assert pygame_ce["version"] == "2.5.7"
    assert any(
        "pygame-2.6.1-cp312-cp312-win_amd64.whl" in wheel["url"]
        for wheel in pygame["wheels"]
    )
    assert any(
        "pygame_ce-2.5.7-cp314-cp314-win_amd64.whl" in wheel["url"]
        and wheel["hash"]
        == "sha256:595f4257c15fed11cd816ae0bfabad3d99f8f04a000d7d164a22c0f9afd6cb65"
        for wheel in pygame_ce["wheels"]
    )

    pygame_requirements = [
        requirement
        for requirement in requirements
        if requirement["name"] in {"pygame", "pygame-ce"}
    ]
    assert pygame_requirements == [
        {
            "name": "pygame",
            "marker": "python_full_version < '3.14'",
            "specifier": ">=2.6.0",
        },
        {
            "name": "pygame-ce",
            "marker": "python_full_version >= '3.14'",
            "specifier": ">=2.5.7",
        },
    ]
