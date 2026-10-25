from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

from importlib import resources


class AudioFeedback:
    def __init__(
        self,
        *,
        logger: logging.Logger,
        enabled: bool = True,
        volume: float = 1.0,
    ) -> None:
        self._logger = logger
        self._volume = max(0.0, min(1.0, volume))
        self._cache: Dict[str, Any] = {}  # cache pygame.mixer.Sound objects
        self._enabled = enabled
        self._mixer_ready = False

        if self._enabled:
            try:
                import pygame.mixer

                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=256)
                self._mixer_ready = True
                self._logger.debug("audio feedback mixer initialized buffer=256")
            except Exception as exc:
                self._logger.warning("audio feedback mixer init failed reason=%s", type(exc).__name__)

    def play_start(self, override_path: Optional[str] = None) -> bool:
        if not self._enabled:
            self._logger.warning("audio feedback skipped reason=muted")
            return True
        if not self._mixer_ready:
            self._logger.warning("audio feedback skipped reason=platform")
            return False
        return self._play_sound("start.wav", override_path)

    def play_start_blocking(
        self,
        override_path: Optional[str] = None,
        *,
        on_complete: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Play the start chime and block until it finishes."""
        if not self._enabled:
            self._logger.warning("audio feedback skipped reason=muted")
            if on_complete is not None:
                on_complete()
            return True
        if not self._mixer_ready:
            self._logger.warning("audio feedback skipped reason=platform")
            if on_complete is not None:
                on_complete()
            return False

        asset_name = "start.wav"
        cache_key = override_path or asset_name
        try:
            if cache_key in self._cache:
                sound = self._cache[cache_key]
            else:
                with self._get_sound_path(asset_name, override_path) as path:
                    resolved = str(path)
                    import pygame.mixer

                    sound = pygame.mixer.Sound(resolved)
                    self._cache[cache_key] = sound

            sound.set_volume(self._volume)
            sound.play()
            self._logger.debug("audio feedback played event=start")
            duration = sound.get_length()
            if duration > 0:
                time.sleep(duration)
            return True
        except FileNotFoundError:
            self._logger.warning("audio feedback skipped reason=file_missing")
            return False
        except Exception as exc:
            self._logger.exception("audio feedback blocking play failed asset=%s reason=%s", asset_name, type(exc).__name__)
            return False
        finally:
            if on_complete is not None:
                on_complete()

    def play_stop(self, override_path: Optional[str] = None) -> bool:
        if not self._enabled:
            self._logger.warning("audio feedback skipped reason=muted")
            return True
        if not self._mixer_ready:
            self._logger.warning("audio feedback skipped reason=platform")
            return False
        return self._play_sound("stop.wav", override_path)

    def play_error(self, override_path: Optional[str] = None) -> None:
        """Play error sound. Uses custom path, or falls back to system beep."""
        if not self._enabled:
            return

        if override_path:
            self._play_sound("error.wav", override_path)
            return

        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception as exc:
            self._logger.exception("audio feedback error beep failed reason=%s", type(exc).__name__)

    def update_settings(self, *, enabled: bool, volume: float) -> None:
        """Apply new enabled/volume settings without tearing down the mixer.

        Closing pygame.mixer while the hotkey thread is mid-play on a cached
        Sound object causes a native SDL abort with no Python traceback. Volume
        is read on every play_* call, so updating self._volume in place is
        enough; the next play picks it up. Lazy-init the mixer if feedback was
        disabled at construction and is now being turned on.
        """
        self._volume = max(0.0, min(1.0, volume))

        if enabled and not self._mixer_ready:
            try:
                import pygame.mixer

                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=256)
                self._mixer_ready = True
                self._logger.debug("audio feedback mixer initialized buffer=256")
            except Exception as exc:
                self._logger.warning("audio feedback mixer init failed reason=%s", type(exc).__name__)

        self._enabled = enabled

    def close(self) -> None:
        """Release SDL audio device."""
        if self._mixer_ready:
            try:
                import pygame.mixer

                pygame.mixer.quit()
                self._mixer_ready = False
                self._logger.debug("audio feedback mixer closed")
            except Exception:
                pass
        self._cache.clear()

    def _play_sound(self, asset_name: str, override_path: Optional[str]) -> bool:
        if not self._enabled or not self._mixer_ready:
            return False

        # Determine the event label for logging: start.wav -> start, stop.wav -> stop, etc.
        event = asset_name.rsplit(".", 1)[0]

        cache_key = override_path or asset_name
        try:
            if cache_key in self._cache:
                sound = self._cache[cache_key]
                sound.set_volume(self._volume)
                sound.play()
                self._logger.debug("audio feedback played event=%s", event)
                return True

            with self._get_sound_path(asset_name, override_path) as path:
                resolved = str(path)
                import pygame.mixer

                sound = pygame.mixer.Sound(resolved)
                sound.set_volume(self._volume)
                self._cache[cache_key] = sound
            sound.play()
            self._logger.debug("audio feedback played event=%s", event)
            return True
        except FileNotFoundError:
            self._logger.warning("audio feedback skipped reason=file_missing")
            return False
        except Exception as exc:
            self._logger.exception("audio feedback play failed asset=%s reason=%s", asset_name, type(exc).__name__)
            return False

    @contextmanager
    def _get_sound_path(
        self, asset_name: str, override_path: Optional[str]
    ) -> Iterator[Path]:
        if override_path:
            yield Path(override_path)
            return
        resource = resources.files("whisperstream.assets").joinpath(asset_name)
        with resources.as_file(resource) as file_path:
            yield file_path
