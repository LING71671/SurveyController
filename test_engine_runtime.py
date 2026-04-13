import threading
import unittest
import importlib

from software.core.engine.execution_loop import ExecutionLoop
from software.core.engine.failure_reason import FailureReason
from software.core.engine.submission_service import SubmissionService
from software.core.task import ExecutionConfig, ExecutionState


class _FakeDriver:
    def __init__(self):
        self.current_url = "https://example.com/complete"

    def get(self, url):
        self.current_url = str(url or "")


class _FakeGui:
    def __init__(self):
        self.random_ip_submission_calls = 0
        self._paused = False

    def wait_if_paused(self, stop_signal):
        return None

    def handle_random_ip_submission(self, stop_signal):
        self.random_ip_submission_calls += 1


class _BaseFakeSession:
    behavior = "success"
    created = 0

    def __init__(self, config, state, gui_instance, thread_name):
        self.config = config
        self.state = state
        self.gui_instance = gui_instance
        self.thread_name = thread_name
        self.driver = None
        self.proxy_address = "http://127.0.0.1:8888"
        self.disposed = 0
        self.shutdown_called = 0
        self.create_called = 0

    def create_browser(self, preferred_browsers, window_x_pos, window_y_pos):
        self.create_called += 1
        type(self).created += 1
        if self.behavior == "raise":
            raise RuntimeError("browser boom")
        if self.behavior == "none":
            return None
        self.driver = _FakeDriver()
        return "edge"

    def dispose(self):
        self.disposed += 1
        self.driver = None

    def shutdown(self):
        self.shutdown_called += 1
        self.driver = None


class _Patch:
    def __init__(self, target: str, value):
        parts = target.split(".")
        module = None
        attr_parts = []
        for index in range(len(parts), 0, -1):
            module_name = ".".join(parts[:index])
            try:
                module = importlib.import_module(module_name)
                attr_parts = parts[index:]
                break
            except ModuleNotFoundError:
                continue
        if module is None or not attr_parts:
            raise ModuleNotFoundError(target)
        parent = module
        for part in attr_parts[:-1]:
            parent = getattr(parent, part)
        self.module = parent
        self.attr_name = attr_parts[-1]
        self.value = value
        self.original = None

    def __enter__(self):
        self.original = getattr(self.module, self.attr_name)
        setattr(self.module, self.attr_name, self.value)
        return self.value

    def __exit__(self, exc_type, exc_val, exc_tb):
        setattr(self.module, self.attr_name, self.original)
        return False


class EngineRuntimeTests(unittest.TestCase):
    def _make_config(self, **overrides):
        config = ExecutionConfig(
            url="https://example.com/survey",
            survey_provider="wjx",
            target_num=1,
            num_threads=1,
            fail_threshold=1,
            stop_on_fail_enabled=True,
        )
        for key, value in overrides.items():
            setattr(config, key, value)
        return config

    def _make_state(self, config=None):
        return ExecutionState(config=config or self._make_config(), stop_event=threading.Event())

    def test_browser_start_failure_increments_fail_and_stops(self):
        fake_gui = _FakeGui()
        config = self._make_config()
        state = self._make_state(config)
        stop_signal = threading.Event()

        class _RaisingSession(_BaseFakeSession):
            behavior = "raise"

        with _Patch("software.core.engine.execution_loop.BrowserSessionService", _RaisingSession):
            loop = ExecutionLoop(config, state, fake_gui)
            loop.run_thread(0, 0, stop_signal)

        self.assertEqual(state.cur_fail, 1)
        self.assertTrue(stop_signal.is_set())
        rows = state.snapshot_thread_progress()
        self.assertEqual(rows[0]["status_text"], "已停止")

    def test_proxy_unavailable_marks_failure(self):
        fake_gui = _FakeGui()
        config = self._make_config(random_proxy_ip_enabled=True)
        state = self._make_state(config)
        stop_signal = threading.Event()

        class _ProxyMissingSession(_BaseFakeSession):
            behavior = "none"

        with _Patch("software.core.engine.execution_loop.BrowserSessionService", _ProxyMissingSession):
            loop = ExecutionLoop(config, state, fake_gui)
            loop.run_thread(0, 0, stop_signal)

        self.assertEqual(state.cur_fail, 1)
        self.assertTrue(stop_signal.is_set())

    def test_submission_success_resets_fail_and_reaches_target(self):
        config = self._make_config(headless_mode=True, target_num=1)
        state = self._make_state(config)
        state.cur_fail = 2
        stop_signal = threading.Event()
        service = SubmissionService(config, state, ExecutionLoop(config, state, _FakeGui()).stop_policy)

        with _Patch("software.core.engine.submission_service._provider_consume_submission_success_signal", lambda *args, **kwargs: True):
            outcome = service.finalize_after_submit(
                _FakeDriver(),
                stop_signal=stop_signal,
                gui_instance=_FakeGui(),
                thread_name="Worker-1",
            )

        self.assertEqual(outcome.status, "success")
        self.assertIsNone(outcome.failure_reason)
        self.assertEqual(state.cur_num, 1)
        self.assertEqual(state.cur_fail, 0)
        self.assertTrue(stop_signal.is_set())

    def test_submission_verification_marks_failure(self):
        config = self._make_config()
        state = self._make_state(config)
        stop_signal = threading.Event()
        service = SubmissionService(config, state, ExecutionLoop(config, state, _FakeGui()).stop_policy)

        with _Patch("software.core.engine.submission_service.random.uniform", lambda a, b: 0.0), \
             _Patch("software.core.engine.submission_service._provider_handle_submission_verification_detected", lambda *args, **kwargs: None), \
             _Patch("software.core.engine.submission_service._provider_submission_validation_message", lambda *args, **kwargs: "需要验证"), \
             _Patch("software.core.engine.submission_service._provider_submission_requires_verification", lambda *args, **kwargs: True), \
             _Patch("software.core.engine.submission_service._provider_consume_submission_success_signal", lambda *args, **kwargs: False):
            outcome = service.finalize_after_submit(
                _FakeDriver(),
                stop_signal=stop_signal,
                gui_instance=_FakeGui(),
                thread_name="Worker-1",
            )

        self.assertEqual(outcome.status, "failure")
        self.assertEqual(outcome.failure_reason, FailureReason.SUBMISSION_VERIFICATION_REQUIRED)
        self.assertEqual(state.cur_fail, 1)
        self.assertEqual(state.cur_num, 0)

    def test_device_quota_limit_path_counts_failure(self):
        fake_gui = _FakeGui()
        config = self._make_config()
        state = self._make_state(config)
        stop_signal = threading.Event()

        class _SuccessSession(_BaseFakeSession):
            behavior = "success"

        with _Patch("software.core.engine.execution_loop.BrowserSessionService", _SuccessSession), \
             _Patch("software.core.engine.execution_loop._provider_is_device_quota_limit_page", lambda *args, **kwargs: True):
            loop = ExecutionLoop(config, state, fake_gui)
            loop.run_thread(0, 0, stop_signal)

        self.assertEqual(state.device_quota_fail_count, 1)
        self.assertEqual(state.cur_fail, 1)
        self.assertTrue(stop_signal.is_set())

    def test_manual_stop_marks_thread_finished(self):
        fake_gui = _FakeGui()
        config = self._make_config()
        state = self._make_state(config)
        stop_signal = threading.Event()
        stop_signal.set()

        class _SuccessSession(_BaseFakeSession):
            behavior = "success"
        _SuccessSession.created = 0

        with _Patch("software.core.engine.execution_loop.BrowserSessionService", _SuccessSession):
            loop = ExecutionLoop(config, state, fake_gui)
            loop.run_thread(0, 0, stop_signal)

        rows = state.snapshot_thread_progress()
        self.assertEqual(rows[0]["status_text"], "已停止")
        self.assertEqual(_SuccessSession.created, 0)


if __name__ == "__main__":
    unittest.main()
