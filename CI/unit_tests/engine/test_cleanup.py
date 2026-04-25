from __future__ import annotations

import unittest
from unittest.mock import patch

from software.core.engine.cleanup import CleanupRunner


class _FakeThread:
    def __init__(self, *, target=None, daemon: bool = False, name: str = "") -> None:
        self.target = target
        self.daemon = daemon
        self.name = name
        self.started = False
        self.alive = False

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return self.alive


class CleanupRunnerTests(unittest.TestCase):
    def test_submit_starts_background_thread_when_idle(self) -> None:
        runner = CleanupRunner()
        created_threads: list[_FakeThread] = []

        def build_thread(*, target=None, daemon: bool = False, name: str = "") -> _FakeThread:
            thread = _FakeThread(target=target, daemon=daemon, name=name)
            created_threads.append(thread)
            return thread

        with patch("software.core.engine.cleanup.threading.Thread", side_effect=build_thread):
            runner.submit(lambda: None, delay_seconds=0.2)

        self.assertEqual(len(created_threads), 1)
        self.assertTrue(created_threads[0].started)
        self.assertEqual(created_threads[0].name, "CleanupWorker")
        self.assertEqual(len(runner._queue), 1)

    def test_submit_reuses_existing_alive_thread(self) -> None:
        runner = CleanupRunner()
        existing_thread = _FakeThread()
        existing_thread.alive = True
        runner._thread = existing_thread

        with patch("software.core.engine.cleanup.threading.Thread") as thread_mock:
            runner.submit(lambda: None)

        thread_mock.assert_not_called()
        self.assertEqual(len(runner._queue), 1)

    def test_worker_runs_task_with_delay_and_clears_thread_reference(self) -> None:
        runner = CleanupRunner()
        events: list[str] = []
        runner._queue.append((lambda: events.append("done"), 0.5))
        runner._thread = object()

        with patch("software.core.engine.cleanup.time.sleep") as sleep_mock:
            runner._worker()

        self.assertEqual(events, ["done"])
        sleep_mock.assert_called_once_with(0.5)
        self.assertIsNone(runner._thread)

    def test_worker_logs_suppressed_exception_when_task_fails(self) -> None:
        runner = CleanupRunner()
        runner._queue.append((lambda: (_ for _ in ()).throw(RuntimeError("boom")), 0.0))
        runner._thread = object()

        with patch("software.core.engine.cleanup.log_suppressed_exception") as log_mock:
            runner._worker()

        log_mock.assert_called_once()
        self.assertIsNone(runner._thread)


if __name__ == "__main__":
    unittest.main()
