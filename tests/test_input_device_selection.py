import logging
import threading
import tomllib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from whisperstream.config_manager import ConfigManager, WhisperstreamConfig
from whisperstream.input_device_selection import (
    macos_input_selection_available,
    reconcile_macos_input_selection,
)
from whisperstream.server import WhisperstreamServer


def _device(logical_id: str, name: str, *, default: bool = False) -> dict[str, object]:
    return {
        "id": logical_id,
        "name": name,
        "channels": 1,
        "sample_rate": 48_000,
        "is_default": default,
    }


def test_unique_exact_legacy_name_migrates_to_id_and_label() -> None:
    result = reconcile_macos_input_selection(
        "Studio Mic",
        "",
        [_device("macos-input-stable", "Studio Mic")],
    )

    assert result.value == "macos-input-stable"
    assert result.label == "Studio Mic"


def test_duplicate_and_absent_legacy_names_retain_runtime_fallback_value() -> None:
    duplicate = reconcile_macos_input_selection(
        "Twin Mic",
        "",
        [
            _device("macos-input-a", "Twin Mic", default=True),
            _device("macos-input-b", "Twin Mic"),
        ],
    )
    absent = reconcile_macos_input_selection(
        "Travel Mic",
        "",
        [_device("macos-input-a", "Built-in Mic", default=True)],
    )

    assert (duplicate.value, duplicate.label) == ("Twin Mic", "Twin Mic")
    assert (absent.value, absent.label) == ("Travel Mic", "Travel Mic")
    assert not macos_input_selection_available(duplicate.value, [])
    assert not macos_input_selection_available(absent.value, [])


def test_stable_identity_survives_rename_reorder_and_default_change() -> None:
    selected_id = "macos-input-selected"
    devices = [
        _device("macos-input-other", "Other", default=True),
        _device(selected_id, "Renamed Studio Mic"),
    ]

    result = reconcile_macos_input_selection(selected_id, "Studio Mic", devices)

    assert result.value == selected_id
    assert result.label == "Renamed Studio Mic"
    assert macos_input_selection_available(result.value, list(reversed(devices)))


def test_unplug_preserves_stale_identity_and_label_until_exact_replug() -> None:
    selected_id = "macos-input-selected"
    unplugged = reconcile_macos_input_selection(selected_id, "Studio Mic", [])
    replugged = reconcile_macos_input_selection(
        unplugged.value,
        unplugged.label,
        [_device(selected_id, "Studio Mic", default=True)],
    )

    assert unplugged.value == selected_id
    assert unplugged.label == "Studio Mic"
    assert not macos_input_selection_available(unplugged.value, [])
    assert replugged == unplugged
    assert macos_input_selection_available(replugged.value, [_device(selected_id, "Studio Mic")])


def test_empty_default_clears_presentation_label() -> None:
    result = reconcile_macos_input_selection(
        "",
        "Old Mic",
        [_device("macos-input-a", "Built-in Mic", default=True)],
    )

    assert (result.value, result.label) == ("", "")


def test_migrated_identity_and_label_survive_restart(tmp_path) -> None:
    path = tmp_path / "config.toml"
    manager = ConfigManager(config_path=path, models_root=tmp_path / "models")
    legacy = WhisperstreamConfig(
        audio_input_device="Studio Mic",
        audio_input_device_label="",
    )
    with patch("whisperstream.config_manager.platform.system", return_value="Darwin"):
        manager.save(legacy)
        migrated = reconcile_macos_input_selection(
            legacy.audio_input_device,
            legacy.audio_input_device_label,
            [_device("macos-input-stable", "Studio Mic")],
        )
        manager.save(
            replace(
                legacy,
                audio_input_device=migrated.value,
                audio_input_device_label=migrated.label,
            )
        )
        restarted = ConfigManager(
            config_path=path,
            models_root=tmp_path / "models-restarted",
        ).load()

    assert restarted.audio_input_device == "macos-input-stable"
    assert restarted.audio_input_device_label == "Studio Mic"


def test_windows_toml_and_client_shape_exclude_mac_only_fields(tmp_path) -> None:
    path = tmp_path / "config.toml"
    manager = ConfigManager(config_path=path, models_root=tmp_path / "models")
    config = WhisperstreamConfig(
        audio_input_device="Studio Mic",
        audio_input_device_label="Mac-only label",
    )

    with patch("whisperstream.config_manager.platform.system", return_value="Windows"):
        manager.save(config)
        payload = ConfigManager.client_dict(config)
        decoded = manager.config_from_client_dict(
            {
                **payload,
                "audio_input_device_label": "injected label",
                "audio_input_device_selection_mode": "opaque-id",
            },
            current=config,
        )

    with path.open("rb") as handle:
        persisted = tomllib.load(handle)
    assert persisted["audio_input_device"] == "Studio Mic"
    assert "audio_input_device_label" not in persisted
    assert "audio_input_device_label" not in payload
    assert "audio_input_device_selection_mode" not in payload
    assert decoded.audio_input_device == "Studio Mic"
    assert decoded.audio_input_device_label == ""


def test_macos_toml_and_client_shape_include_identity_presentation_fields(
    tmp_path,
) -> None:
    path = tmp_path / "config.toml"
    manager = ConfigManager(config_path=path, models_root=tmp_path / "models")
    config = WhisperstreamConfig(
        audio_input_device="macos-input-stable",
        audio_input_device_label="Studio Mic",
    )

    with patch("whisperstream.config_manager.platform.system", return_value="Darwin"):
        manager.save(config)
        payload = ConfigManager.client_dict(config)

    with path.open("rb") as handle:
        persisted = tomllib.load(handle)
    assert persisted["audio_input_device"] == "macos-input-stable"
    assert persisted["audio_input_device_label"] == "Studio Mic"
    assert payload["audio_input_device_label"] == "Studio Mic"
    assert payload["audio_input_device_selection_mode"] == "opaque-id"


def test_server_reconciliation_saves_and_reloads_identity_pair_atomically() -> None:
    config = WhisperstreamConfig(audio_input_device="Studio Mic")
    manager = MagicMock()
    app = SimpleNamespace(
        config=config,
        config_manager=manager,
        _lock=threading.Lock(),
        _recording=False,
    )
    app._reload_config_locked = MagicMock(
        side_effect=lambda updated: setattr(app, "config", updated)
    )
    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._app = app
    server._logger = logging.getLogger(__name__)

    with patch("whisperstream.server.app.platform.system", return_value="Darwin"):
        updated, _devices = server._reconcile_macos_input_selection(
            [_device("macos-input-stable", "Studio Mic")]
        )

    assert updated.audio_input_device == "macos-input-stable"
    assert updated.audio_input_device_label == "Studio Mic"
    manager.save.assert_called_once_with(updated)
    app._reload_config_locked.assert_called_once_with(updated)


def test_server_reconciliation_is_a_byte_for_byte_windows_noop() -> None:
    config = WhisperstreamConfig(audio_input_device="Studio Mic")
    manager = MagicMock()
    app = SimpleNamespace(config=config, config_manager=manager, reload_config=MagicMock())
    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._app = app
    server._logger = logging.getLogger(__name__)

    with patch("whisperstream.server.app.platform.system", return_value="Windows"):
        updated, devices = server._reconcile_macos_input_selection(
            [_device("windows-input-runtime", "Studio Mic")]
        )

    assert updated is config
    assert devices is None
    assert updated.audio_input_device == "Studio Mic"
    manager.save.assert_not_called()
    app.reload_config.assert_not_called()


def test_server_reconciliation_defers_during_an_active_recording() -> None:
    config = WhisperstreamConfig(audio_input_device="Studio Mic")
    manager = MagicMock()
    app = SimpleNamespace(
        config=config,
        config_manager=manager,
        reload_config=MagicMock(),
        _lock=threading.Lock(),
        _recording=True,
    )
    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._app = app
    server._logger = logging.getLogger(__name__)

    with patch("whisperstream.server.app.platform.system", return_value="Darwin"):
        updated, devices = server._reconcile_macos_input_selection(
            [_device("macos-input-stable", "Studio Mic")]
        )

    assert updated is config
    assert devices is None
    manager.save.assert_not_called()
    app.reload_config.assert_not_called()


def test_start_attempt_racing_enumeration_defers_reconciliation() -> None:
    config = WhisperstreamConfig(audio_input_device="Studio Mic")
    manager = MagicMock()
    app = SimpleNamespace(
        config=config,
        config_manager=manager,
        reload_config=MagicMock(),
        _lock=threading.Lock(),
        _recording=False,
    )
    server = WhisperstreamServer.__new__(WhisperstreamServer)
    server._app = app
    server._logger = logging.getLogger(__name__)
    enumeration_started = threading.Event()
    release_enumeration = threading.Event()
    result: list[tuple[WhisperstreamConfig, list[dict[str, object]] | None]] = []

    def enumerate_devices() -> list[dict[str, object]]:
        enumeration_started.set()
        assert release_enumeration.wait(timeout=2)
        return [_device("macos-input-stable", "Studio Mic")]

    def reconcile() -> None:
        result.append(server._reconcile_macos_input_selection())

    def start_attempt() -> None:
        with app._lock:
            app._recording = True

    with (
        patch("whisperstream.server.app.platform.system", return_value="Darwin"),
        patch(
            "whisperstream.capture.list_input_devices",
            side_effect=enumerate_devices,
        ),
    ):
        reconciliation_thread = threading.Thread(target=reconcile)
        reconciliation_thread.start()
        assert enumeration_started.wait(timeout=2)
        start_thread = threading.Thread(target=start_attempt)
        start_thread.start()
        start_thread.join(timeout=2)
        assert not start_thread.is_alive()
        release_enumeration.set()
        reconciliation_thread.join(timeout=2)

    assert not reconciliation_thread.is_alive()
    assert result == [(config, None)]
    manager.save.assert_not_called()
    app.reload_config.assert_not_called()
