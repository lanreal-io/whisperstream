"""Kernel identity remains exact when AppKit omits launchDate."""
from unittest.mock import MagicMock, patch

import psutil
import pytest

from whisperstream.macos_process_identity import process_launch_token


def test_pid_reuse_returns_a_new_token_without_caching_process_objects():
    first = MagicMock()
    first.create_time.return_value = 1000.25
    second = MagicMock()
    second.create_time.return_value = 1001.5
    with patch('whisperstream.macos_process_identity.psutil.Process', side_effect=[first, second]) as factory:
        assert process_launch_token(123) == 1000250000
        assert process_launch_token(123) == 1001500000
        assert factory.call_count == 2
        first.name.assert_not_called()
        first.exe.assert_not_called()


@pytest.mark.parametrize('failure', [psutil.NoSuchProcess(123), psutil.AccessDenied(123)])
def test_unobservable_identity_fails_closed(failure):
    with patch('whisperstream.macos_process_identity.psutil.Process', side_effect=failure):
        assert process_launch_token(123) is None


@pytest.mark.parametrize('created', [0, -1, float('nan'), float('inf')])
def test_invalid_creation_time_fails_closed(created):
    with patch('whisperstream.macos_process_identity.psutil.Process') as factory:
        factory.return_value.create_time.return_value = created
        assert process_launch_token(123) is None


def test_terminated_process_fails_closed():
    with patch('whisperstream.macos_process_identity.psutil.Process') as factory:
        factory.return_value.create_time.return_value = 1000
        factory.return_value.is_running.return_value = False
        assert process_launch_token(123) is None
