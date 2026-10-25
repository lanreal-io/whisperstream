"""Deterministic Darwin model-lifecycle conformance for pinned acquisition and relocation.

Every test uses a fresh temporary app/home/destination and tiny public-fixture
bytes. The production inventory remains the reviewed 23-file FluidAudio pin;
the injected miniature inventory makes failure transitions cheap to exercise.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from whisperstream.macos_model_lifecycle import (
    FLUIDAUDIO_MODEL_REPOSITORY,
    FLUIDAUDIO_MODEL_REVISION,
    FLUIDAUDIO_MODEL_INVENTORY,
    MACOS_FLUIDAUDIO_ASSET_ID,
    MacosModelArtifact,
    MacosModelLifecycleAdapter,
    MacosModelStorage,
    fluid_model_root,
)
from whisperstream.model_lifecycle import PlatformModelLifecycleFactory
from whisperstream.model_lifecycle_contract import (
    ModelAssetRef,
    ModelFailure,
    ModelFamily,
    ModelLifecycleAdapter,
    ModelOperation,
    ModelOperationRequest,
    ModelOperationResult,
    ModelState,
)


PUBLIC_FILES = {
    "Preprocessor.mlmodelc/model.mil": b"public-preprocessor",
    "Encoder.mlmodelc/model.mil": b"public-encoder",
    "Decoder.mlmodelc/model.mil": b"public-decoder",
    "JointDecisionv3.mlmodelc/model.mil": b"public-joint",
    "parakeet_v3_vocab.json": b"{}",
}
MINI_INVENTORY = tuple(
    MacosModelArtifact(path, len(body), hashlib.sha256(body).hexdigest())
    for path, body in PUBLIC_FILES.items()
)


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, _size: int):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _Client:
    def __init__(self, files: dict[str, bytes], *, corrupt: str | None = None) -> None:
        self._files = files
        self._corrupt = corrupt
        self.urls: list[str] = []

    def stream(self, method: str, url: str) -> _Response:
        assert method == "GET"
        self.urls.append(url)
        relative = url.split(f"/resolve/{FLUIDAUDIO_MODEL_REVISION}/", 1)[1]
        body = self._files[relative]
        if relative == self._corrupt:
            body += b"corrupt"
        return _Response(body)

    def close(self) -> None:
        return None


def _asset() -> ModelAssetRef:
    return ModelAssetRef(MACOS_FLUIDAUDIO_ASSET_ID, ModelFamily.STT)


def _request(
    generation: int,
    operation: ModelOperation = ModelOperation.ACQUIRE,
    *,
    destination_token: str | None = None,
) -> ModelOperationRequest:
    return ModelOperationRequest(
        generation,
        operation,
        _asset(),
        destination_token=destination_token,
    )


@pytest.fixture
def storage(tmp_path: Path) -> MacosModelStorage:
    return MacosModelStorage(
        default_home=tmp_path / "default-home",
        state_path=tmp_path / "app" / "macos-model-storage.json",
    )


def _adapter(
    storage: MacosModelStorage,
    *,
    corrupt: str | None = None,
) -> tuple[MacosModelLifecycleAdapter, _Client]:
    client = _Client(PUBLIC_FILES, corrupt=corrupt)
    return (
        MacosModelLifecycleAdapter(
            storage=storage,
            inventory=MINI_INVENTORY,
            client_factory=lambda: client,
        ),
        client,
    )


def test_factory_selects_real_darwin_arm64_adapter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CFFIXED_USER_HOME", str(tmp_path))
    adapter = PlatformModelLifecycleFactory(
        system="Darwin",
        machine="arm64",
    ).create()
    assert isinstance(adapter, MacosModelLifecycleAdapter)
    assert isinstance(adapter, ModelLifecycleAdapter)


def test_busy_relocation_discards_unconsumed_native_destination(
    storage: MacosModelStorage,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "isolated-data"
    monkeypatch.setenv("WHISPERSTREAM_DATA_DIR", str(data_dir))
    destination = tmp_path / "selected-storage"
    destination.mkdir()
    token = f"macos-native:{'5' * 48}"
    registry = data_dir / "macos-native-selections-v1.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "selections": {
                    token: {
                        "kind": "select-model-storage",
                        "path": str(destination),
                        "display_name": "Selected model storage",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    adapter, _client = _adapter(storage)
    adapter._active_generation = 77

    result = adapter.execute_relocation(
        _request(78, ModelOperation.RELOCATE, destination_token=token),
        activate=lambda: None,
    )

    assert result.failure is ModelFailure.BUSY
    assert not registry.exists()
    assert token not in storage._destinations
    destination.rename(tmp_path / "released-storage")


def test_production_inventory_is_pinned_to_reviewed_public_source() -> None:
    from whisperstream.macos_model_lifecycle import FLUIDAUDIO_MODEL_INVENTORY

    assert FLUIDAUDIO_MODEL_REPOSITORY == "FluidInference/parakeet-tdt-0.6b-v3-coreml"
    assert FLUIDAUDIO_MODEL_REVISION == "aed02740059203c4a87495924f685de3722ae9ce"
    assert len(FLUIDAUDIO_MODEL_INVENTORY) == 23
    assert sum(item.size for item in FLUIDAUDIO_MODEL_INVENTORY) == 483_256_769
    assert len({item.path for item in FLUIDAUDIO_MODEL_INVENTORY}) == 23


@pytest.fixture
def production_http_transport(monkeypatch: pytest.MonkeyPatch):
    """Replace transport only, retaining the adapter's real default and hooks."""
    real_client = httpx.Client

    def install(handler):
        def client(**options):
            assert "transport" not in options
            return real_client(**options, transport=httpx.MockTransport(handler))

        monkeypatch.setattr(httpx, "Client", client)

    return install


_CONFIG_ARTIFACT = next(
    item for item in FLUIDAUDIO_MODEL_INVENTORY if item.path == "config.json"
)
_CONFIG_URL = (
    f"https://huggingface.co/{FLUIDAUDIO_MODEL_REPOSITORY}/resolve/"
    f"{FLUIDAUDIO_MODEL_REVISION}/config.json"
)
_CACHE_URL = (
    f"https://huggingface.co/api/resolve-cache/models/"
    f"{FLUIDAUDIO_MODEL_REPOSITORY}/{FLUIDAUDIO_MODEL_REVISION}/config.json"
)


@pytest.mark.parametrize("redirect", [
    _CACHE_URL,
    "https://us.aws.cdn.hf.co/pinned-object?signature=synthetic",
    "https://cas-bridge.xethub.hf.co/pinned-object?signature=synthetic",
    "https://cdn-lfs.huggingface.co/pinned-object",
    "https://cdn-lfs-us-1.huggingface.co/pinned-object",
    "https://cdn-lfs-eu-1.huggingface.co/pinned-object",
])
def test_default_client_acquires_verified_pinned_file_through_allowed_redirect(
    storage: MacosModelStorage, production_http_transport, redirect: str,
) -> None:
    requests = []

    def transport(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            assert str(request.url) == _CONFIG_URL
            return httpx.Response(302, headers={"location": redirect})
        assert str(request.url) == redirect
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=b"{}")

    production_http_transport(transport)
    adapter = MacosModelLifecycleAdapter(storage=storage, inventory=(_CONFIG_ARTIFACT,))
    result = adapter.execute(_request(101))

    assert result.state is ModelState.READY
    assert result.failure is ModelFailure.NONE
    assert adapter.snapshot(_asset()).verified is True
    assert requests == [_CONFIG_URL, redirect]
    assert (storage.active_model_root / "config.json").read_bytes() == b"{}"


@pytest.mark.parametrize("corrupt", [False, True])
def test_default_client_first_inventory_path_uses_aws_cdn_and_verifies_bytes(
    storage: MacosModelStorage, production_http_transport, corrupt: bool,
) -> None:
    # config.json uses a different redirect. Exercise the production inventory's
    # first path with tiny fixture bytes and the real default request hooks.
    path = FLUIDAUDIO_MODEL_INVENTORY[0].path
    assert path == "Decoder.mlmodelc/analytics/coremldata.bin"
    body = b"public-cold-artifact"
    artifact = MacosModelArtifact(path, len(body), hashlib.sha256(body).hexdigest())
    url = (
        f"https://huggingface.co/{FLUIDAUDIO_MODEL_REPOSITORY}/resolve/"
        f"{FLUIDAUDIO_MODEL_REVISION}/{path}"
    )
    redirect = "https://us.aws.cdn.hf.co/pinned-object?signature=synthetic"
    requests = []

    def transport(request):
        requests.append(str(request.url))
        if len(requests) == 1:
            assert str(request.url) == url
            return httpx.Response(302, headers={"location": redirect})
        assert str(request.url) == redirect
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=body[:-1] + b"X" if corrupt else body)

    production_http_transport(transport)
    adapter = MacosModelLifecycleAdapter(storage=storage, inventory=(artifact,))
    result = adapter.execute(_request(107))

    assert requests == [url, redirect]
    assert not list(storage.active_model_root.parent.glob(".*.staging-*"))
    assert not list(storage.active_model_root.parent.rglob("*.tmp"))
    if corrupt:
        assert result.failure is ModelFailure.INTEGRITY
        assert result.state is ModelState.ABSENT
        assert not storage.active_model_root.exists()
    else:
        assert result.failure is ModelFailure.NONE
        assert result.state is ModelState.READY
        assert adapter.snapshot(_asset()).verified is True
        assert (storage.active_model_root / path).read_bytes() == body


@pytest.mark.parametrize("url,headers", [
    (_CONFIG_URL.replace("https:", "http:"), {}),
    (_CONFIG_URL.replace("huggingface.co", "huggingface.co:8443"), {}),
    (_CONFIG_URL.replace("huggingface.co", "user:password@huggingface.co"), {}),
    (_CONFIG_URL, {"Authorization": "Bearer synthetic"}),
    (_CONFIG_URL, {"Cookie": "synthetic=value"}),
    (_CONFIG_URL.replace(FLUIDAUDIO_MODEL_REVISION, "main"), {}),
    (_CONFIG_URL.replace(FLUIDAUDIO_MODEL_REPOSITORY, "unrelated/model"), {}),
    (_CONFIG_URL.replace("config.json", "unreviewed.bin"), {}),
    (_CONFIG_URL.replace("huggingface.co", "subdomain.huggingface.co"), {}),
    (_CONFIG_URL.replace("huggingface.co", "huggingface.co.invalid"), {}),
    ("https://unrelated.invalid/model", {}),
    ("https://cas-bridge.xethub.hf.co.invalid/model", {}),
    ("https://us.aws.cdn.hf.co.invalid/model", {}),
    ("https://us.aws.cdn.hf.co/model", {"Authorization": "Bearer synthetic"}),
    ("https://us.aws.cdn.hf.co/model", {"Cookie": "synthetic=value"}),
])
def test_default_client_rejects_unpinned_or_credentialed_requests_before_transport(
    storage: MacosModelStorage, production_http_transport, url: str, headers: dict,
) -> None:
    requests = []
    production_http_transport(lambda request: requests.append(request) or httpx.Response(200))
    adapter = MacosModelLifecycleAdapter(storage=storage, inventory=(_CONFIG_ARTIFACT,))

    with adapter._client_factory() as client:
        assert client.timeout.connect == 30.0
        assert client.timeout.read == 60.0
        assert client.follow_redirects is True
        with pytest.raises(RuntimeError, match="source request is not allowed"):
            client.get(url, headers=headers)

    assert requests == []


@pytest.mark.parametrize("redirect", [
    "http://cas-bridge.xethub.hf.co/pinned-object",
    "https://cas-bridge.xethub.hf.co:8443/pinned-object",
    "https://user:password@cas-bridge.xethub.hf.co/pinned-object",
    "https://unrelated.invalid/pinned-object",
    "http://us.aws.cdn.hf.co/pinned-object",
    "https://us.aws.cdn.hf.co:8443/pinned-object",
    "https://user:password@us.aws.cdn.hf.co/pinned-object",
    _CACHE_URL.replace(FLUIDAUDIO_MODEL_REVISION, "main"),
])
def test_default_client_rechecks_every_redirect_and_cleans_failed_staging(
    storage: MacosModelStorage, production_http_transport, redirect: str, monkeypatch,
) -> None:
    requests = []

    def transport(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": redirect})

    production_http_transport(transport)
    adapter = MacosModelLifecycleAdapter(storage=storage, inventory=(_CONFIG_ARTIFACT,))
    result = adapter.execute(_request(102))

    assert result.failure is ModelFailure.INTEGRITY
    assert result.state is ModelState.ABSENT
    assert requests == [_CONFIG_URL]
    assert not list(storage.active_model_root.parent.glob(".*.staging-*"))


def test_default_client_rejects_cookies_set_during_redirect(
    storage: MacosModelStorage, production_http_transport,
) -> None:
    requests = []

    def transport(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={
            "location": _CACHE_URL,
            "set-cookie": "synthetic=value; Path=/",
        })

    production_http_transport(transport)
    adapter = MacosModelLifecycleAdapter(storage=storage, inventory=(_CONFIG_ARTIFACT,))
    result = adapter.execute(_request(103))

    assert result.failure is ModelFailure.INTEGRITY
    assert requests == [_CONFIG_URL]
    assert not storage.active_model_root.exists()


def test_default_client_hash_failure_never_publishes_ready(
    storage: MacosModelStorage, production_http_transport,
) -> None:
    # Equal length exercises the SHA256 guard independently of the size check.
    production_http_transport(lambda request: httpx.Response(200, content=b"[]"))
    adapter = MacosModelLifecycleAdapter(storage=storage, inventory=(_CONFIG_ARTIFACT,))
    result = adapter.execute(_request(104))

    assert result.failure is ModelFailure.INTEGRITY
    assert result.state is ModelState.ABSENT
    assert not storage.active_model_root.exists()
    assert not list(storage.active_model_root.parent.glob(".*.staging-*"))
    assert not list(storage.active_model_root.parent.rglob("*.tmp"))


def test_default_client_cancel_after_bytes_cleans_up_and_retry_verifies(
    storage: MacosModelStorage, production_http_transport,
) -> None:
    requests = []

    def transport(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=b"{}")

    production_http_transport(transport)
    adapter = MacosModelLifecycleAdapter(storage=storage, inventory=(_CONFIG_ARTIFACT,))
    progress = []

    def cancel_on_progress(item):
        progress.append(item)
        assert adapter.cancel(105).failure is ModelFailure.CANCELLED

    cancelled = adapter.execute(_request(105), cancel_on_progress)
    assert progress and progress[-1].completed_bytes == 2
    assert cancelled.failure is ModelFailure.CANCELLED
    assert cancelled.state is ModelState.ABSENT
    assert not storage.active_model_root.exists()
    assert not list(storage.active_model_root.parent.glob(".*.staging-*"))
    assert not list(storage.active_model_root.parent.rglob("*.tmp"))

    retry = adapter.execute(_request(106))
    assert retry.failure is ModelFailure.NONE
    assert adapter.snapshot(_asset()).verified is True
    assert requests == [_CONFIG_URL, _CONFIG_URL]


def test_fluidaudio_redirects_do_not_widen_registry_github_client(
    storage: MacosModelStorage, production_http_transport,
) -> None:
    from whisperstream.model_downloader import new_http_client

    requests = []
    production_http_transport(lambda request: requests.append(str(request.url)) or httpx.Response(200))
    github_url = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/test.tar.bz2"
    with new_http_client(github_url=github_url) as client:
        assert client.get(github_url).status_code == 200
        with pytest.raises(RuntimeError, match="unsupported host"):
            client.get(_CONFIG_URL)
        with pytest.raises(RuntimeError, match="unsupported host"):
            client.get("https://us.aws.cdn.hf.co/pinned-object")

    assert requests == [github_url]


def test_verified_acquisition_reports_progress_and_warm_offline_ready(
    storage: MacosModelStorage,
) -> None:
    adapter, client = _adapter(storage)
    progress = []

    result = adapter.execute(_request(1), progress.append)

    assert result.state is ModelState.READY
    assert result.failure is ModelFailure.NONE
    assert result.changed is True
    snapshot = adapter.snapshot(_asset())
    assert snapshot.state is ModelState.READY
    assert snapshot.verified is True
    assert snapshot.location_token and str(storage.active_home) not in snapshot.location_token
    assert progress[-1].completed_bytes == sum(map(len, PUBLIC_FILES.values()))
    assert progress[-1].total_bytes == sum(map(len, PUBLIC_FILES.values()))
    assert all(FLUIDAUDIO_MODEL_REVISION in url for url in client.urls)

    offline = MacosModelLifecycleAdapter(
        storage=storage,
        inventory=MINI_INVENTORY,
        client_factory=lambda: pytest.fail("warm readiness must not use network"),
    )
    assert offline.snapshot(_asset()).state is ModelState.READY
    assert offline.execute(_request(2, ModelOperation.VERIFY)).failure is ModelFailure.NONE


def test_integrity_failure_is_typed_and_staging_is_removed(
    storage: MacosModelStorage,
) -> None:
    adapter, _client = _adapter(storage, corrupt="Encoder.mlmodelc/model.mil")

    result = adapter.execute(_request(3))

    assert result.failure is ModelFailure.INTEGRITY
    assert result.state is ModelState.ABSENT
    assert not fluid_model_root(storage.active_home).exists()
    assert not list(fluid_model_root(storage.active_home).parent.glob("*.staging-*"))


def test_client_factory_failure_is_typed_and_staging_is_removed(
    storage: MacosModelStorage,
) -> None:
    adapter = MacosModelLifecycleAdapter(
        storage=storage,
        inventory=MINI_INVENTORY,
        client_factory=lambda: (_ for _ in ()).throw(
            RuntimeError("synthetic client factory failure")
        ),
    )

    result = adapter.execute(_request(4))

    assert result.failure is ModelFailure.SOURCE_UNAVAILABLE
    assert result.state is ModelState.ABSENT
    assert not list(fluid_model_root(storage.active_home).parent.glob("*.staging-*"))


def test_client_close_failure_is_typed_after_exact_tree_cleanup(
    storage: MacosModelStorage,
) -> None:
    class CloseFailingClient(_Client):
        def close(self) -> None:
            raise OSError("synthetic client close failure")

    client = CloseFailingClient(PUBLIC_FILES)
    adapter = MacosModelLifecycleAdapter(
        storage=storage,
        inventory=MINI_INVENTORY,
        client_factory=lambda: client,
    )

    result = adapter.execute(_request(5))

    assert result.failure is ModelFailure.CLEANUP
    assert result.state is ModelState.READY
    parent = fluid_model_root(storage.active_home).parent
    assert not list(parent.glob("*.staging-*"))
    assert not list(parent.glob("*.replaced-*"))


def test_cancel_is_generation_exact_and_never_publishes_partial(
    storage: MacosModelStorage,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingResponse(_Response):
        def iter_bytes(self, _size: int):
            midpoint = max(1, len(self._body) // 2)
            yield self._body[:midpoint]
            entered.set()
            release.wait(timeout=5)
            yield self._body[midpoint:]

    class BlockingClient(_Client):
        def stream(self, method: str, url: str) -> _Response:
            response = super().stream(method, url)
            return BlockingResponse(response._body)

    client = BlockingClient(PUBLIC_FILES)
    adapter = MacosModelLifecycleAdapter(
        storage=storage,
        inventory=MINI_INVENTORY,
        client_factory=lambda: client,
    )
    results = []
    thread = threading.Thread(target=lambda: results.append(adapter.execute(_request(8))))
    thread.start()
    assert entered.wait(timeout=2)

    wrong = adapter.cancel(9)
    exact = adapter.cancel(8)
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert wrong.failure is ModelFailure.UNSUPPORTED
    assert exact.failure is ModelFailure.CANCELLED
    assert results[0].failure is ModelFailure.CANCELLED
    assert adapter.snapshot(_asset()).state is ModelState.ABSENT
    assert not list(fluid_model_root(storage.active_home).parent.glob("*.staging-*"))


def test_relocation_commits_verified_tree_and_updates_worker_home(
    storage: MacosModelStorage,
    tmp_path: Path,
) -> None:
    adapter, _client = _adapter(storage)
    assert adapter.execute(_request(10)).state is ModelState.READY
    old_root = fluid_model_root(storage.active_home)
    sentinel = old_root.parent / "unrelated-sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    destination = tmp_path / "custom-home"
    token = storage.register_destination(destination)

    result = adapter.execute(
        _request(11, ModelOperation.RELOCATE, destination_token=token)
    )

    assert result.state is ModelState.READY
    assert storage.active_home == destination.resolve()
    assert storage.worker_environment() == {
        "HOME": str(destination.resolve()),
        "CFFIXED_USER_HOME": str(destination.resolve()),
    }
    assert adapter.snapshot(_asset()).state is ModelState.READY
    assert not old_root.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    state = json.loads(storage.state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == 1
    assert state["active_token"] == token


def test_relocation_failure_preserves_source_and_cleans_destination(
    storage: MacosModelStorage,
    tmp_path: Path,
) -> None:
    adapter, _client = _adapter(storage)
    assert adapter.execute(_request(12)).state is ModelState.READY
    source = fluid_model_root(storage.active_home)
    destination = tmp_path / "failing-home"
    token = storage.register_destination(destination)

    with patch(
        "whisperstream.macos_model_lifecycle.copy_tree_with_progress",
        side_effect=OSError("private-destination-marker"),
    ):
        result = adapter.execute(
            _request(13, ModelOperation.RELOCATE, destination_token=token)
        )

    assert result.failure is ModelFailure.IO
    assert "private-destination-marker" not in repr(result)
    assert source.exists()
    assert storage.active_home != destination.resolve()
    assert storage.resolve_destination(token) is None
    assert not list(fluid_model_root(destination).parent.glob("*.staging-*"))


def test_relocation_rejects_raced_occupied_target_without_touching_it(
    storage: MacosModelStorage,
    tmp_path: Path,
) -> None:
    adapter, _client = _adapter(storage)
    assert adapter.execute(_request(14)).state is ModelState.READY
    source = storage.active_model_root
    destination = tmp_path / "occupied-home"
    token = storage.register_destination(destination)
    occupied = fluid_model_root(destination)
    occupied.mkdir(parents=True)
    sentinel = occupied / "owner-sentinel"
    sentinel.write_bytes(b"keep-owner-data-byte-for-byte")

    result = adapter.execute(
        _request(15, ModelOperation.RELOCATE, destination_token=token)
    )

    assert result.failure is ModelFailure.DESTINATION_UNAVAILABLE
    assert sentinel.read_bytes() == b"keep-owner-data-byte-for-byte"
    assert source.exists()
    assert storage.active_model_root == source
    assert not list(occupied.parent.glob("*.staging-*"))


def test_activation_failure_restores_source_location_and_cleans_new_tree(
    storage: MacosModelStorage,
    tmp_path: Path,
) -> None:
    adapter, _client = _adapter(storage)
    assert adapter.execute(_request(15)).state is ModelState.READY
    source_home = storage.active_home
    source = storage.active_model_root
    destination = tmp_path / "activation-failure-home"
    token = storage.register_destination(destination)
    activations = 0

    def activate() -> None:
        nonlocal activations
        activations += 1
        if activations == 1:
            raise RuntimeError("synthetic new-worker load failure")

    result = adapter.execute_relocation(
        _request(16, ModelOperation.RELOCATE, destination_token=token),
        activate=activate,
    )

    assert result.failure is ModelFailure.IO
    assert activations == 2
    assert storage.active_home == source_home
    assert source.exists()
    assert not fluid_model_root(destination).exists()
    assert storage.resolve_destination(token) is None


def test_cleanup_failure_is_typed_and_does_not_start_acquisition(
    storage: MacosModelStorage,
) -> None:
    adapter, client = _adapter(storage)
    staging = storage.active_model_root.parent / ".parakeet-tdt-0.6b-v3.staging-14"
    staging.mkdir(parents=True)

    with patch(
        "whisperstream.macos_model_lifecycle.shutil.rmtree",
        side_effect=OSError("synthetic cleanup denial"),
    ):
        result = adapter.execute(_request(14))

    assert result.failure is ModelFailure.CLEANUP
    assert client.urls == []
    assert staging.exists()


def test_delete_is_idempotent_exact_and_reacquisition_works(
    storage: MacosModelStorage,
) -> None:
    adapter, _client = _adapter(storage)
    assert adapter.execute(_request(20)).state is ModelState.READY
    root = fluid_model_root(storage.active_home)
    sentinel = root.parent / "unrelated"
    sentinel.write_text("keep", encoding="utf-8")

    first = adapter.execute(_request(21, ModelOperation.DELETE))
    second = adapter.execute(_request(22, ModelOperation.DELETE))
    reacquired = adapter.execute(_request(23))

    assert first.state is second.state is ModelState.ABSENT
    assert first.changed is True
    assert second.changed is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert reacquired.state is ModelState.READY


def test_storage_rejects_unknown_or_unsafe_destination_tokens(
    storage: MacosModelStorage,
    tmp_path: Path,
) -> None:
    adapter, _client = _adapter(storage)
    unknown = adapter.execute(
        _request(30, ModelOperation.RELOCATE, destination_token="storage:unknown")
    )
    assert unknown.failure is ModelFailure.DESTINATION_UNAVAILABLE

    with pytest.raises(ValueError):
        storage.register_destination(storage.active_home / "nested")
    with pytest.raises(ValueError):
        storage.register_destination(tmp_path / "default-home" / "nested-again")


class _ServerLifecycle:
    stt_asset_id = MACOS_FLUIDAUDIO_ASSET_ID

    def __init__(self, failure: ModelFailure) -> None:
        self.failure = failure
        self.registered_paths: list[str] = []
        self.requests: list[ModelOperationRequest] = []
        self.storage = MagicMock()

    def register_destination(self, destination: str) -> str:
        self.registered_paths.append(destination)
        return "macos-storage-test-token"

    def execute(self, request, on_progress=None):
        del on_progress
        self.requests.append(request)
        state = ModelState.READY
        return ModelOperationResult(
            request.generation,
            request.operation,
            request.asset,
            state,
            self.failure,
        )

    def execute_relocation(self, request, on_progress=None, activate=None):
        del on_progress
        self.requests.append(request)
        failure = self.failure
        if failure is ModelFailure.NONE and activate is not None:
            try:
                activate()
            except Exception:
                failure = ModelFailure.IO
        return ModelOperationResult(
            request.generation,
            request.operation,
            request.asset,
            ModelState.READY,
            failure,
        )


def _server_with_lifecycle(failure: ModelFailure):
    from whisperstream.server import WhisperstreamServer

    app = MagicMock()
    app.config.stt_backend = "parakeet"
    app.config.parakeet_model = MACOS_FLUIDAUDIO_ASSET_ID
    app.set_event_broadcaster = MagicMock()
    app.license_manager.status.return_value = {
        "is_pro": True,
        "trial_remaining_seconds": 0,
    }
    server = WhisperstreamServer(app, port=0, logger=None, register_hotkeys=False)
    server._model_lifecycle = _ServerLifecycle(failure)
    return server, app


def test_active_delete_is_refused_before_touching_the_mac_stt_engine() -> None:
    server, app = _server_with_lifecycle(ModelFailure.PERMISSION)
    server._reload_models = MagicMock()
    client = TestClient(server.fastapi, raise_server_exceptions=True)
    original_engine = app.stt_engine

    response = client.post("/api/stt/delete", json={"model": "parakeet"})

    assert response.status_code == 409
    assert response.json()["reason"] == "active"
    original_engine.shutdown.assert_not_called()
    server._reload_models.assert_not_called()


def test_mac_qwen_delete_never_executes_fluidaudio_owner(
    tmp_path: Path,
) -> None:
    server, _app = _server_with_lifecycle(ModelFailure.NONE)
    lifecycle = server._model_lifecycle
    sentinel = tmp_path / "fluid-model-sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    original_execute = lifecycle.execute

    def destructive_execute(*args, **kwargs):
        sentinel.unlink()
        return original_execute(*args, **kwargs)

    lifecycle.execute = destructive_execute
    client = TestClient(server.fastapi, raise_server_exceptions=True)

    response = client.post("/api/stt/delete", json={"model": "qwen"})

    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "model": "qwen"}
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert lifecycle.requests == []


def test_mac_raw_path_relocation_endpoints_are_unavailable() -> None:
    server, _app = _server_with_lifecycle(ModelFailure.NONE)
    lifecycle = server._model_lifecycle
    client = TestClient(server.fastapi, raise_server_exceptions=True)

    check = client.post(
        "/api/models/relocate/check",
        json={"destination": "/private/raw-path-must-not-cross"},
    )
    start = client.post(
        "/api/models/relocate",
        json={"destination": "/private/raw-path-must-not-cross"},
    )

    assert check.status_code == 409
    assert start.status_code == 409
    assert check.json() == {
        "status": "unavailable",
        "reason": "native-picker-required",
    }
    assert start.json() == check.json()
    assert lifecycle.registered_paths == []
    assert lifecycle.requests == []
    assert server._download_thread is None


def test_mac_raw_path_worker_defensively_rejects_adapter_registration() -> None:
    server, _app = _server_with_lifecycle(ModelFailure.NONE)
    lifecycle = server._model_lifecycle
    events = []
    server.broadcast = events.append

    server._run_model_relocation("/private/raw-path-must-not-cross")

    assert lifecycle.registered_paths == []
    assert lifecycle.requests == []
    assert events[-1] == {
        "type": "download.error",
        "kind": "relocate",
        "message": ModelFailure.DESTINATION_UNAVAILABLE.value,
    }


@pytest.mark.parametrize(
    ("failure", "reload_fails", "terminal"),
    [
        (ModelFailure.INTEGRITY, False, ModelFailure.INTEGRITY.value),
        (ModelFailure.NONE, True, ModelFailure.IO.value),
    ],
)
def test_mac_opaque_relocation_never_completes_until_source_or_destination_loads(
    failure: ModelFailure,
    reload_fails: bool,
    terminal: str,
) -> None:
    server, _app = _server_with_lifecycle(failure)
    server._unload_models = MagicMock()
    server._reload_models = MagicMock(
        side_effect=RuntimeError("synthetic reload failure") if reload_fails else None
    )
    events = []
    server.broadcast = events.append

    server._run_registered_model_relocation("macos-storage-test-token")

    assert not any(event.get("type") == "download.complete" for event in events)
    assert events[-1] == {
        "type": "download.error",
        "kind": "relocate",
        "message": terminal,
    }
    assert server._reload_models.call_count == (2 if reload_fails else 1)
    assert server._model_lifecycle.requests[0].destination_token == (
        "macos-storage-test-token"
    )


def test_mac_opaque_relocation_cancelled_during_unload_never_reaches_storage() -> None:
    server, _app = _server_with_lifecycle(ModelFailure.NONE)
    lifecycle = server._model_lifecycle
    events = []
    server.broadcast = events.append
    server._reload_models = MagicMock()

    def cancel_during_unload() -> None:
        server._download_cancel_event.set()

    server._unload_models = MagicMock(side_effect=cancel_during_unload)

    server._run_registered_model_relocation("macos-native:cancel-race")

    assert lifecycle.requests == []
    server._reload_models.assert_called_once()
    lifecycle.storage.release_destination.assert_called_once_with(
        "macos-native:cancel-race"
    )
    assert events[-1] == {"type": "download.cancelled", "kind": "relocate"}
    assert server._download_cancel_event is None

@pytest.mark.parametrize("busy", ["recording", "model-operation"])
def test_registered_relocation_does_not_unload_a_busy_model(busy):
    server, app = _server_with_lifecycle(ModelFailure.NONE)
    server._unload_models = MagicMock()
    server.broadcast = MagicMock()
    app.stt_activation_busy.return_value = busy == "recording"
    if busy == "model-operation":
        server._model_operation_lock.acquire()
    try:
        server._run_registered_model_relocation("macos-native:busy")
        server._unload_models.assert_not_called()
        assert not server._model_lifecycle.requests
        assert server.broadcast.call_args.args[0]["type"] == "download.error"
    finally:
        if busy == "model-operation":
            server._model_operation_lock.release()
