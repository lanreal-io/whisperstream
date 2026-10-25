"""Darwin owner for the verified FluidAudio Parakeet model lifecycle.

The shared contract sees logical assets, generations, progress, and opaque
tokens only. This module alone owns the FluidAudio cache layout, pinned public
artifact URLs, backend-private storage selection, staging paths, and cleanup.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import secrets
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

import httpx

from .atomic_io import atomic_write_json
from .model_downloader import (
    DownloadCancelled,
    copy_tree_with_progress,
    download_file_with_sha256,
)
from .model_lifecycle_contract import (
    ModelAssetRef,
    ModelFailure,
    ModelFamily,
    ModelLifecycleAdapter,
    ModelOperation,
    ModelOperationRequest,
    ModelOperationResult,
    ModelProgress,
    ModelSnapshot,
    ModelState,
    ProgressCallback,
)


class _CleanupFailure(OSError):
    """An exact lifecycle-owned staging/backup cleanup did not complete."""


class _ActivationFailure(OSError):
    """The relocated model could not replace the live worker generation."""


class _ClientFactoryFailure(Exception):
    """The pinned public model source client could not be constructed."""


def _remove_tree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _CleanupFailure("model lifecycle cleanup failed") from exc


MACOS_FLUIDAUDIO_ASSET_ID = "nemo-parakeet-tdt-0.6b-v3"
FLUIDAUDIO_MODEL_REPOSITORY = "FluidInference/parakeet-tdt-0.6b-v3-coreml"
FLUIDAUDIO_MODEL_REVISION = "aed02740059203c4a87495924f685de3722ae9ce"
_FLUIDAUDIO_MODEL_FOLDER = "parakeet-tdt-0.6b-v3"


@dataclass(frozen=True)
class MacosModelArtifact:
    path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        relative = Path(self.path)
        if (
            not self.path
            or relative.is_absolute()
            or ".." in relative.parts
            or self.size < 0
            or len(self.sha256) != 64
        ):
            raise ValueError("invalid model artifact metadata")


# Reviewed public inventory from mac-stt-loop Unit 01 evidence. The resolved
# Hugging Face revision, byte counts, and digests are immutable acquisition
# inputs. Do not replace them with FluidAudio's moving `main` downloader.
FLUIDAUDIO_MODEL_INVENTORY = (
    MacosModelArtifact("Decoder.mlmodelc/analytics/coremldata.bin", 243, "4238c4e81ecd0dc94bd7dfbb60f7e2cc824107c1ffe0387b8607b72833dba350"),
    MacosModelArtifact("Decoder.mlmodelc/coremldata.bin", 554, "18647af085d87bd8f3121c8a9b4d4564c1ede038dab63d295b4e745cf2d7fb99"),
    MacosModelArtifact("Decoder.mlmodelc/metadata.json", 3427, "a39e93cd8371b8ded92635c7804fcd0590f0d1dd9415c6d19a0484be073077d9"),
    MacosModelArtifact("Decoder.mlmodelc/model.mil", 13110, "ef2a0a281695398a62fde86ac269c68f73d5b578d7ed3b31f2ba91a2d1ea1f35"),
    MacosModelArtifact("Decoder.mlmodelc/weights/weight.bin", 23604992, "48adf0f0d47c406c8253d4f7fef967436a39da14f5a65e66d5a4b407be355d41"),
    MacosModelArtifact("Encoder.mlmodelc/analytics/coremldata.bin", 243, "42e638870d73f26b332918a3496ce36793fbb413a81cbd3d16ba01328637a105"),
    MacosModelArtifact("Encoder.mlmodelc/coremldata.bin", 485, "d48034a167a82e88fc3df64f60af963ab3983538271175b8319e7d5720a0fb86"),
    MacosModelArtifact("Encoder.mlmodelc/metadata.json", 2921, "da24da9cca943fb29d7fa8e376d57fca7cb3aa08ca51b956b0b0e56813f087e9"),
    MacosModelArtifact("Encoder.mlmodelc/model.mil", 959769, "ed7b19156ca29fa7dfd6891deb9fda4b0e8893f68597c985d135736546a43808"),
    MacosModelArtifact("Encoder.mlmodelc/weights/weight.bin", 445187200, "e2020f323703477a5b21d7c2d282c403e371afb5962e79877e3033e73ba6f421"),
    MacosModelArtifact("JointDecisionv3.mlmodelc/analytics/coremldata.bin", 243, "26def4bf73dd56d29dee21c8ef97cb8969e62f6120ed1adc91e46828e2737b6c"),
    MacosModelArtifact("JointDecisionv3.mlmodelc/coremldata.bin", 521, "f5fc08b741400f0088492c9e839418b1e18522f19cba28d361dd030c5f398342"),
    MacosModelArtifact("JointDecisionv3.mlmodelc/metadata.json", 3453, "d9307211b9a37e0f0ac260c7660b1571a3de25841035cfdf9b58fd40425f890f"),
    MacosModelArtifact("JointDecisionv3.mlmodelc/model.mil", 11775, "be60732943389a047175111a83f8839f3eb39d4803adafa828a0871b2f39818d"),
    MacosModelArtifact("JointDecisionv3.mlmodelc/weights/weight.bin", 12642764, "4e0e63d840032f7f07ddb1d64446051166281e5491bf22da8a945c41f6eedb3e"),
    MacosModelArtifact("Preprocessor.mlmodelc/analytics/coremldata.bin", 243, "c9beeb989c8d66f8be11df59bc6df277ec76cee404f6865b46243835ef562f6d"),
    MacosModelArtifact("Preprocessor.mlmodelc/coremldata.bin", 486, "dbde3f2300842c1fd51ef3ff948a0bcffe65ffd2dca10707f2509f32c1d65b1d"),
    MacosModelArtifact("Preprocessor.mlmodelc/metadata.json", 2841, "2a98699e22d279dd37fa1d238aeb1c6db1df0d6fad687775324157689d8f3acf"),
    MacosModelArtifact("Preprocessor.mlmodelc/model.mil", 28181, "4b8518a956450fec57f06c2a21bdffc26973f7f1fa6842fb38fe917f896b6b93"),
    MacosModelArtifact("Preprocessor.mlmodelc/weights/weight.bin", 491072, "129b76e3aeafa8afa3ea76d995b964b145fe83700d579f6ff42c4c38fa0968ea"),
    MacosModelArtifact("config.json", 2, "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    MacosModelArtifact("parakeet_v3_vocab.json", 151122, "7ec60e05f1b24480736ec0eed40900f4626bce1fa9a60fd700ec7e2a59198735"),
    MacosModelArtifact("parakeet_vocab.json", 151122, "7ec60e05f1b24480736ec0eed40900f4626bce1fa9a60fd700ec7e2a59198735"),
)


_FLUIDAUDIO_CDN_HOSTS = frozenset({
    "us.aws.cdn.hf.co",
    "cas-bridge.xethub.hf.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co",
    "cdn-lfs-eu-1.huggingface.co",
})


def _validate_fluidaudio_request(request: httpx.Request) -> None:
    """Keep the native artifact source independent of registry edition policy."""
    url = request.url
    if (
        request.method != "GET"
        or url.scheme != "https"
        or url.port not in (None, 443)
        or url.userinfo
        or "authorization" in request.headers
        or "cookie" in request.headers
    ):
        raise RuntimeError("FluidAudio model source request is not allowed")
    if url.host in _FLUIDAUDIO_CDN_HOSTS:
        return
    if url.host == "huggingface.co":
        # HF may redirect small files to its resolve-cache endpoint. Both
        # forms still name the exact reviewed repository, revision and file.
        prefixes = (
            f"/{FLUIDAUDIO_MODEL_REPOSITORY}/resolve/{FLUIDAUDIO_MODEL_REVISION}/",
            f"/api/resolve-cache/models/{FLUIDAUDIO_MODEL_REPOSITORY}/{FLUIDAUDIO_MODEL_REVISION}/",
        )
        if any(
            url.path == prefix + item.path
            for prefix in prefixes
            for item in FLUIDAUDIO_MODEL_INVENTORY
        ):
            return
    raise RuntimeError("FluidAudio model source request is not allowed")


def _new_fluidaudio_http_client() -> httpx.Client:
    # This existing native source is not the registry's HF fallback. Keep its
    # pinned acquisition usable without enabling public registry providers.
    return httpx.Client(
        timeout=httpx.Timeout(connect=30.0, read=60.0, write=None, pool=None),
        follow_redirects=True,
        event_hooks={"request": [_validate_fluidaudio_request]},
    )


def fluid_model_root(home: Path) -> Path:
    return (
        Path(home)
        / "Library"
        / "Application Support"
        / "FluidAudio"
        / "Models"
        / _FLUIDAUDIO_MODEL_FOLDER
    )


def _default_home() -> Path:
    value = os.environ.get("CFFIXED_USER_HOME", "").strip() or os.environ.get(
        "HOME", ""
    ).strip()
    if not value:
        raise RuntimeError("macOS model storage requires a user home")
    return Path(value).expanduser().resolve()


class MacosModelStorage:
    """Persist one backend-private FluidAudio home behind opaque tokens."""

    def __init__(self, *, default_home: Path, state_path: Path) -> None:
        self.default_home = Path(default_home).expanduser().resolve()
        self.state_path = Path(state_path)
        self._destinations: dict[str, Path] = {}
        self._active_home = self.default_home
        self._active_token = "macos-storage-default"
        self._load()

    def _load(self) -> None:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                return
            raw_home = payload.get("active_home")
            raw_token = payload.get("active_token")
            if isinstance(raw_home, str) and isinstance(raw_token, str) and raw_token:
                self._active_home = Path(raw_home).expanduser().resolve()
                self._active_token = raw_token
                self._destinations[raw_token] = self._active_home
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return

    @property
    def active_home(self) -> Path:
        return self._active_home

    @property
    def active_token(self) -> str:
        return self._active_token

    @property
    def active_model_root(self) -> Path:
        return fluid_model_root(self._active_home)

    def worker_environment(self) -> dict[str, str]:
        home = str(self._active_home)
        return {"HOME": home, "CFFIXED_USER_HOME": home}

    def register_destination(self, destination_home: Path) -> str:
        destination = Path(destination_home).expanduser().resolve()
        self._validate_destination(destination)
        if destination == self._active_home:
            return self._active_token
        token = f"macos-storage-{secrets.token_hex(12)}"
        self._destinations[token] = destination
        return token

    def _validate_destination(self, destination: Path) -> None:
        current = self._active_home
        if destination == current:
            return
        if current in destination.parents or destination in current.parents:
            raise ValueError("model storage destinations cannot be nested")
        if fluid_model_root(destination).exists():
            raise ValueError("model storage destination is occupied")

    def resolve_destination(self, token: str) -> Path | None:
        if token == self._active_token:
            return self._active_home
        destination = self._destinations.get(token)
        if destination is not None:
            return destination
        if not token.startswith("macos-native:"):
            return None
        from .history_security_contract import FileWorkflowKind
        from .macos_history_security import MacosNativeSelectionRegistry

        native = MacosNativeSelectionRegistry().consume(
            token,
            expected_kind=FileWorkflowKind.SELECT_MODEL_STORAGE,
        )
        if native is None:
            return None
        destination = Path(native.path).expanduser().resolve()
        self._validate_destination(destination)
        self._destinations[token] = destination
        return destination

    def release_destination(self, token: str) -> None:
        if token != self._active_token:
            self._destinations.pop(token, None)
            if token.startswith("macos-native:"):
                from .macos_history_security import MacosNativeSelectionRegistry

                try:
                    MacosNativeSelectionRegistry().discard([token])
                except OSError:
                    # The process-local destination is already gone. A corrupt
                    # or inaccessible registry remains fail-closed and must not
                    # turn a typed terminal result into an unhandled exception.
                    pass

    def commit(self, token: str) -> None:
        destination = self.resolve_destination(token)
        if destination is None:
            raise ValueError("unknown storage token")
        self._commit_location(token, destination)

    def _commit_location(self, token: str, destination: Path) -> None:
        payload = {
            "schema_version": 1,
            "active_token": token,
            "active_home": str(destination),
        }
        atomic_write_json(self.state_path, payload)
        self._active_token = token
        self._active_home = destination

    def restore(self, token: str, destination_home: Path) -> None:
        destination = Path(destination_home).expanduser().resolve()
        self._destinations[token] = destination
        self._commit_location(token, destination)


def create_macos_model_storage() -> MacosModelStorage:
    from .config_manager import get_app_dir

    return MacosModelStorage(
        default_home=_default_home(),
        state_path=get_app_dir() / "macos-model-storage-v1.json",
    )


class MacosModelLifecycleAdapter:
    """Verified acquisition and transactional storage for the Mac STT asset."""

    stt_asset_id = MACOS_FLUIDAUDIO_ASSET_ID

    def __init__(
        self,
        *,
        storage: MacosModelStorage | None = None,
        inventory: Iterable[MacosModelArtifact] = FLUIDAUDIO_MODEL_INVENTORY,
        client_factory: Callable[[], httpx.Client] = _new_fluidaudio_http_client,
    ) -> None:
        self.storage = storage or create_macos_model_storage()
        self._inventory = tuple(inventory)
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._active_generation: int | None = None
        self._active_request: ModelOperationRequest | None = None
        self._cancel_event: threading.Event | None = None

    @property
    def active_generation(self) -> int | None:
        with self._lock:
            return self._active_generation

    def register_destination(self, destination_home: Path | str) -> str:
        return self.storage.register_destination(Path(destination_home))

    @staticmethod
    def _supported(asset: ModelAssetRef) -> bool:
        return (
            asset.family is ModelFamily.STT
            and asset.asset_id == MACOS_FLUIDAUDIO_ASSET_ID
        )

    def _verified(self, root: Path) -> tuple[ModelState, bool]:
        if not root.exists():
            return ModelState.ABSENT, False
        saw_entry = False
        try:
            for item in self._inventory:
                path = root / item.path
                if not path.is_file():
                    return ModelState.PARTIAL, False
                saw_entry = True
                if path.stat().st_size != item.size:
                    return ModelState.CORRUPT, False
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != item.sha256:
                    return ModelState.CORRUPT, False
        except (OSError, PermissionError):
            return ModelState.CORRUPT, False
        return (ModelState.READY, True) if saw_entry else (ModelState.PARTIAL, False)

    def snapshot(self, asset: ModelAssetRef) -> ModelSnapshot:
        if not self._supported(asset):
            return ModelSnapshot(asset=asset, state=ModelState.UNAVAILABLE)
        with self._lock:
            active = self._active_generation
            active_request = self._active_request
        if active_request is not None and active_request.asset == asset:
            return ModelSnapshot(
                asset=asset,
                state=ModelState.BUSY,
                operation_generation=active,
            )
        state, verified = self._verified(self.storage.active_model_root)
        return ModelSnapshot(
            asset=asset,
            state=state,
            location_token=(self.storage.active_token if state is ModelState.READY else None),
            verified=verified,
        )

    def execute(
        self,
        request: ModelOperationRequest,
        on_progress: ProgressCallback | None = None,
    ) -> ModelOperationResult:
        return self._execute(request, on_progress=on_progress)

    def execute_relocation(
        self,
        request: ModelOperationRequest,
        *,
        on_progress: ProgressCallback | None = None,
        activate: Callable[[], None],
    ) -> ModelOperationResult:
        """Relocate while retaining the verified source through live activation."""

        if request.operation is not ModelOperation.RELOCATE:
            return self._result(request, ModelState.UNAVAILABLE, ModelFailure.UNSUPPORTED)
        return self._execute(
            request,
            on_progress=on_progress,
            activation_callback=activate,
        )

    def _execute(
        self,
        request: ModelOperationRequest,
        *,
        on_progress: ProgressCallback | None = None,
        activation_callback: Callable[[], None] | None = None,
    ) -> ModelOperationResult:
        if not self._supported(request.asset):
            return self._result(request, ModelState.UNAVAILABLE, ModelFailure.UNSUPPORTED)
        if request.operation is ModelOperation.VERIFY:
            snapshot = self.snapshot(request.asset)
            failure = (
                ModelFailure.INTEGRITY
                if snapshot.state in {ModelState.CORRUPT, ModelState.PARTIAL}
                else ModelFailure.NONE
            )
            return self._result(request, snapshot.state, failure)
        if request.operation not in {
            ModelOperation.ACQUIRE,
            ModelOperation.RELOCATE,
            ModelOperation.DELETE,
        }:
            return self._result(request, ModelState.UNAVAILABLE, ModelFailure.UNSUPPORTED)

        event = threading.Event()
        with self._lock:
            if self._active_generation is not None:
                if request.operation is ModelOperation.RELOCATE:
                    assert request.destination_token is not None
                    self.storage.release_destination(request.destination_token)
                return self._result(request, ModelState.BUSY, ModelFailure.BUSY)
            self._active_generation = request.generation
            self._active_request = request
            self._cancel_event = event

        total = sum(item.size for item in self._inventory)

        def report(completed: int) -> None:
            if on_progress is not None:
                on_progress(
                    ModelProgress(
                        request.generation,
                        request.asset,
                        min(completed, total),
                        total,
                    )
                )

        try:
            if request.operation is ModelOperation.ACQUIRE:
                state, changed = self._acquire(request.generation, event, report)
            elif request.operation is ModelOperation.RELOCATE:
                assert request.destination_token is not None
                state, changed = self._relocate(
                    request.generation,
                    request.destination_token,
                    event,
                    report,
                    activation_callback,
                )
            else:
                state, changed = self._delete()
            return self._result(request, state, changed=changed)
        except DownloadCancelled:
            return self._result(
                request,
                self._verified(self.storage.active_model_root)[0],
                ModelFailure.CANCELLED,
            )
        except _CleanupFailure:
            failure = ModelFailure.CLEANUP
        except _ClientFactoryFailure:
            failure = ModelFailure.SOURCE_UNAVAILABLE
        except _ActivationFailure:
            failure = ModelFailure.IO
        except PermissionError:
            failure = ModelFailure.PERMISSION
        except httpx.TransportError:
            failure = ModelFailure.NETWORK
        except httpx.HTTPStatusError:
            failure = ModelFailure.SOURCE_UNAVAILABLE
        except ValueError:
            failure = ModelFailure.DESTINATION_UNAVAILABLE
        except FileExistsError:
            failure = ModelFailure.DESTINATION_UNAVAILABLE
        except OSError as exc:
            failure = ModelFailure.NO_SPACE if exc.errno == errno.ENOSPC else ModelFailure.IO
        except RuntimeError:
            failure = ModelFailure.INTEGRITY
        except Exception:
            failure = ModelFailure.SOURCE_UNAVAILABLE
        finally:
            if request.operation is ModelOperation.RELOCATE:
                assert request.destination_token is not None
                self.storage.release_destination(request.destination_token)
            with self._lock:
                self._active_generation = None
                self._active_request = None
                self._cancel_event = None
        return self._result(
            request,
            self._verified(self.storage.active_model_root)[0],
            failure,
        )

    def _acquire(
        self,
        generation: int,
        cancel_event: threading.Event,
        report: Callable[[int], None],
    ) -> tuple[ModelState, bool]:
        root = self.storage.active_model_root
        if self._verified(root) == (ModelState.READY, True):
            return ModelState.READY, False
        staging = root.parent / f".{root.name}.staging-{generation}"
        backup = root.parent / f".{root.name}.replaced-{generation}"
        _remove_tree(staging)
        _remove_tree(backup)
        staging.mkdir(parents=True, exist_ok=False)
        completed = 0
        client = None
        try:
            try:
                client = self._client_factory()
            except Exception as exc:  # noqa: BLE001 - content-free typed boundary
                raise _ClientFactoryFailure("model source client unavailable") from exc
            for item in self._inventory:
                if cancel_event.is_set():
                    raise DownloadCancelled("macOS model acquisition cancelled")
                destination = staging / item.path
                destination.parent.mkdir(parents=True, exist_ok=True)
                encoded = quote(item.path, safe="/")
                url = (
                    f"https://huggingface.co/{FLUIDAUDIO_MODEL_REPOSITORY}/resolve/"
                    f"{FLUIDAUDIO_MODEL_REVISION}/{encoded}"
                )

                def on_chunk(size: int) -> None:
                    nonlocal completed
                    completed += size
                    report(completed)

                download_file_with_sha256(
                    client,
                    url,
                    destination,
                    item.sha256,
                    on_chunk,
                    cancel_event,
                )
                if destination.stat().st_size != item.size:
                    raise RuntimeError("model artifact size mismatch")
            if self._verified(staging) != (ModelState.READY, True):
                raise RuntimeError("model staging inventory failed")
            if root.exists():
                os.replace(root, backup)
            try:
                os.replace(staging, root)
            except BaseException:
                if backup.exists() and not root.exists():
                    os.replace(backup, root)
                raise
            _remove_tree(backup)
            report(sum(item.size for item in self._inventory))
            return ModelState.READY, True
        finally:
            close_failure: Exception | None = None
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001 - cleanup still must continue
                    close_failure = exc

            cleanup_failure: _CleanupFailure | None = None
            try:
                _remove_tree(staging)
            except _CleanupFailure as exc:
                cleanup_failure = exc
            try:
                if backup.exists() and not root.exists():
                    try:
                        os.replace(backup, root)
                    except OSError as exc:
                        raise _CleanupFailure("model backup restore failed") from exc
                else:
                    _remove_tree(backup)
            except _CleanupFailure as exc:
                cleanup_failure = cleanup_failure or exc
            if cleanup_failure is not None:
                raise cleanup_failure
            if close_failure is not None:
                raise _CleanupFailure("model source client cleanup failed") from close_failure

    def _relocate(
        self,
        generation: int,
        token: str,
        cancel_event: threading.Event,
        report: Callable[[int], None],
        activation_callback: Callable[[], None] | None,
    ) -> tuple[ModelState, bool]:
        destination_home = self.storage.resolve_destination(token)
        if destination_home is None:
            raise ValueError("unknown destination token")
        source = self.storage.active_model_root
        source_home = self.storage.active_home
        source_token = self.storage.active_token
        if destination_home == self.storage.active_home:
            return self._verified(source)[0], False
        if self._verified(source) != (ModelState.READY, True):
            raise RuntimeError("source model is not verified")
        final = fluid_model_root(destination_home)
        staging = final.parent / f".{final.name}.staging-{generation}"
        _remove_tree(staging)
        completed = 0

        def on_bytes(size: int) -> None:
            nonlocal completed
            completed += size
            report(completed)

        try:
            copy_tree_with_progress(
                source,
                staging,
                on_bytes=on_bytes,
                cancel_event=cancel_event,
                cancel_message="macOS model relocation cancelled",
            )
            if self._verified(staging) != (ModelState.READY, True):
                raise RuntimeError("relocation staging inventory failed")
            final.parent.mkdir(parents=True, exist_ok=True)
            # Atomically reserve the exact target. Never replace an occupied
            # tree, including one created after picker validation.
            reserved_target = False
            try:
                final.mkdir(exist_ok=False)
                reserved_target = True
                for child in staging.iterdir():
                    os.replace(child, final / child.name)
                staging.rmdir()
                self.storage.commit(token)
            except BaseException:
                # Remove only the exact tree this generation successfully
                # reserved. A FileExistsError means another owner won the race,
                # so its tree must remain byte-for-byte untouched.
                if reserved_target:
                    _remove_tree(final)
                raise
            if activation_callback is not None:
                try:
                    activation_callback()
                except Exception as exc:  # noqa: BLE001 - mapped without detail
                    _remove_tree(final)
                    self.storage.restore(source_token, source_home)
                    try:
                        activation_callback()
                    except Exception as restore_exc:  # noqa: BLE001 - exact source remains
                        raise _ActivationFailure(
                            "model activation and source restore failed"
                        ) from restore_exc
                    raise _ActivationFailure("model activation failed") from exc
            _remove_tree(source)
            report(sum(item.size for item in self._inventory))
            return ModelState.READY, True
        finally:
            _remove_tree(staging)

    def _delete(self) -> tuple[ModelState, bool]:
        root = self.storage.active_model_root
        changed = root.exists()
        if changed:
            shutil.rmtree(root, ignore_errors=False)
        return ModelState.ABSENT, changed

    def cancel(self, generation: int) -> ModelOperationResult:
        with self._lock:
            active = self._active_generation
            request = self._active_request
            event = self._cancel_event
            if active != generation or request is None or event is None:
                return ModelOperationResult(
                    generation,
                    ModelOperation.CANCEL,
                    ModelAssetRef(MACOS_FLUIDAUDIO_ASSET_ID, ModelFamily.STT),
                    ModelState.UNAVAILABLE,
                    ModelFailure.UNSUPPORTED,
                )
            event.set()
            asset = request.asset
        return ModelOperationResult(
            generation,
            ModelOperation.CANCEL,
            asset,
            ModelState.BUSY,
            ModelFailure.CANCELLED,
        )

    @staticmethod
    def _result(
        request: ModelOperationRequest,
        state: ModelState,
        failure: ModelFailure = ModelFailure.NONE,
        *,
        changed: bool = False,
    ) -> ModelOperationResult:
        return ModelOperationResult(
            request.generation,
            request.operation,
            request.asset,
            state,
            failure,
            changed,
        )


__all__ = [
    "FLUIDAUDIO_MODEL_INVENTORY",
    "FLUIDAUDIO_MODEL_REPOSITORY",
    "FLUIDAUDIO_MODEL_REVISION",
    "MACOS_FLUIDAUDIO_ASSET_ID",
    "MacosModelArtifact",
    "MacosModelLifecycleAdapter",
    "MacosModelStorage",
    "create_macos_model_storage",
    "fluid_model_root",
]
