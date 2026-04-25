from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from software.core.engine.runtime_control import _is_headless_mode, _sleep_with_stop, _wait_if_paused


class RuntimeControlTests(unittest.TestCase):
    def test_is_headless_mode_reads_flag_from_context(self) -> None:
        self.assertFalse(_is_headless_mode(None))
        self.assertTrue(_is_headless_mode(SimpleNamespace(headless_mode=True)))

    def test_wait_if_paused_calls_gui_handler(self) -> None:
        gui = MagicMock()
        stop_signal = object()

        _wait_if_paused(gui, stop_signal)

        gui.wait_if_paused.assert_called_once_with(stop_signal)

    def test_wait_if_paused_swallows_gui_exception(self) -> None:
        gui = MagicMock()
        gui.wait_if_paused.side_effect = RuntimeError("boom")

        with patch("software.core.engine.runtime_control.log_suppressed_exception") as log_mock:
            _wait_if_paused(gui, None)

        log_mock.assert_called_once()

    def test_sleep_with_stop_returns_false_for_non_positive_seconds(self) -> None:
        self.assertFalse(_sleep_with_stop(None, 0))

    def test_sleep_with_stop_returns_true_only_when_signal_is_set_after_wait(self) -> None:
        stop_signal = MagicMock()
        stop_signal.wait.return_value = True
        stop_signal.is_set.return_value = True

        self.assertTrue(_sleep_with_stop(stop_signal, 0.2))

    def test_sleep_with_stop_returns_false_when_wait_completes_without_stop(self) -> None:
        stop_signal = MagicMock()
        stop_signal.wait.return_value = False

        self.assertFalse(_sleep_with_stop(stop_signal, 0.2))

    def test_sleep_with_stop_uses_time_sleep_without_signal(self) -> None:
        with patch("software.core.engine.runtime_control.time.sleep") as sleep_mock:
            self.assertFalse(_sleep_with_stop(None, 0.3))

        sleep_mock.assert_called_once_with(0.3)


if __name__ == "__main__":
    unittest.main()
