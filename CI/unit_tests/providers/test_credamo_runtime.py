from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from credamo.provider import parser, runtime
from software.core.questions.normalization import configure_probabilities
from software.core.questions.schema import QuestionEntry


class CredamoRuntimeTests(unittest.TestCase):
    class _FakeChoiceElement:
        def __init__(self) -> None:
            self.checked = False

        def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
            return None

        def click(self, timeout: int = 0) -> None:
            return None

    class _FakeDropdownInput:
        def __init__(self) -> None:
            self.value = ""

        def scroll_into_view_if_needed(self, timeout: int = 0) -> None:
            return None

        def click(self, timeout: int = 0) -> None:
            return None

        def focus(self) -> None:
            return None

    class _FakeDropdownLocator:
        def __init__(self, count_value: int) -> None:
            self._count_value = count_value

        def count(self) -> int:
            return self._count_value

    def test_click_submit_waits_until_dynamic_button_appears(self) -> None:
        attempts = iter([False, False, True])

        with patch("credamo.provider.runtime._click_submit_once", side_effect=lambda _page: next(attempts)), \
             patch("credamo.provider.runtime.time.sleep") as sleep_mock:
            clicked = runtime._click_submit(object(), timeout_ms=2000)

        self.assertTrue(clicked)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_click_submit_stops_waiting_when_abort_requested(self) -> None:
        stop_signal = threading.Event()

        def abort_after_first_wait(_seconds: float | None = None) -> bool:
            stop_signal.set()
            return True

        with patch("credamo.provider.runtime._click_submit_once", return_value=False):
            setattr(stop_signal, "wait", abort_after_first_wait)
            clicked = runtime._click_submit(object(), stop_signal, timeout_ms=2000)

        self.assertFalse(clicked)

    def test_brush_credamo_walks_next_pages_before_submit(self) -> None:
        stop_signal = threading.Event()
        state = SimpleNamespace(
            stop_event=stop_signal,
            update_thread_step=lambda *args, **kwargs: None,
            update_thread_status=lambda *args, **kwargs: None,
        )
        config = SimpleNamespace(
            question_config_index_map={
                1: ("single", 0),
                2: ("dropdown", 0),
                3: ("order", -1),
            },
            single_prob=[-1],
            droplist_prob=[-1],
            scale_prob=[],
            multiple_prob=[],
            texts=[],
            answer_duration_range_seconds=[0, 0],
        )
        driver = SimpleNamespace(page=object())
        roots_page1 = [object(), object()]
        roots_page2 = [object()]

        with patch("credamo.provider.runtime._wait_for_question_roots", side_effect=[roots_page1, roots_page2]), \
             patch("credamo.provider.runtime._question_number_from_root", side_effect=[1, 2, 3]), \
             patch("credamo.provider.runtime._navigation_action", side_effect=["next", "submit"]), \
             patch("credamo.provider.runtime._question_signature", side_effect=[(("question-1", "page1"),)]), \
             patch("credamo.provider.runtime._wait_for_page_change", return_value=True), \
             patch("credamo.provider.runtime._click_navigation", return_value=True) as click_navigation_mock, \
             patch("credamo.provider.runtime._click_submit", return_value=True) as click_submit_mock, \
             patch("credamo.provider.runtime._answer_single_like", return_value=True) as single_mock, \
             patch("credamo.provider.runtime._answer_dropdown", return_value=True) as dropdown_mock, \
             patch("credamo.provider.runtime._answer_order", return_value=True) as order_mock, \
             patch("credamo.provider.runtime.simulate_answer_duration_delay", return_value=False), \
             patch("credamo.provider.runtime.time.sleep"):
            result = runtime.brush_credamo(
                driver,
                config,
                state,
                stop_signal=stop_signal,
                thread_name="Worker-1",
            )

        self.assertTrue(result)
        self.assertEqual(single_mock.call_count, 1)
        self.assertEqual(dropdown_mock.call_count, 1)
        self.assertEqual(order_mock.call_count, 1)
        click_navigation_mock.assert_called_once_with(driver.page, "next")
        click_submit_mock.assert_called_once_with(driver.page, stop_signal)

    def test_answer_single_like_does_not_report_success_when_target_stays_unchecked(self) -> None:
        input_element = self._FakeChoiceElement()
        root = SimpleNamespace()
        page = SimpleNamespace(
            evaluate=lambda script, element: bool(getattr(element, "checked", False)),
        )

        with patch("credamo.provider.runtime._option_inputs", return_value=[input_element]), \
             patch("credamo.provider.runtime._option_click_targets", return_value=[]), \
             patch("credamo.provider.runtime._click_element", return_value=True), \
             patch("credamo.provider.runtime.normalize_droplist_probs", return_value=[100.0]), \
             patch("credamo.provider.runtime.weighted_index", return_value=0):
            answered = runtime._answer_single_like(page, root, [100.0], 1)

        self.assertFalse(answered)

    def test_answer_dropdown_uses_keyboard_selection_for_credamo_select(self) -> None:
        trigger = self._FakeDropdownInput()
        value_input = self._FakeDropdownInput()
        locator = self._FakeDropdownLocator(4)

        class _FakeKeyboard:
            def __init__(self, input_element: "CredamoRuntimeTests._FakeDropdownInput") -> None:
                self.input_element = input_element
                self.arrow_down_count = 0

            def press(self, key: str) -> None:
                if key == "ArrowDown":
                    self.arrow_down_count += 1
                elif key == "Enter" and self.arrow_down_count > 0:
                    self.input_element.value = f"选项 {self.arrow_down_count}"

        class _FakeRoot:
            def query_selector(self, selector: str):
                if selector in {".pc-dropdown .el-input", ".el-input"}:
                    return trigger
                if selector == ".el-input__inner":
                    return value_input
                return None

        def _evaluate(script: str, element) -> object:
            if "el.value" in script:
                return getattr(element, "value", "")
            return True

        page = SimpleNamespace(
            evaluate=_evaluate,
            wait_for_timeout=lambda _ms: None,
            locator=lambda _selector: locator,
            keyboard=_FakeKeyboard(value_input),
        )

        with patch("credamo.provider.runtime._click_element", return_value=True), \
             patch("credamo.provider.runtime.normalize_droplist_probs", return_value=[0.0, 100.0, 0.0, 0.0]), \
             patch("credamo.provider.runtime.weighted_index", return_value=1):
            answered = runtime._answer_dropdown(page, _FakeRoot(), [0.0, 100.0, 0.0, 0.0])

        self.assertTrue(answered)
        self.assertEqual(value_input.value, "选项 2")


class CredamoParserTests(unittest.TestCase):
    def test_infer_type_code_uses_page_block_kind(self) -> None:
        self.assertEqual(parser._infer_type_code({"question_kind": "dropdown"}), "7")
        self.assertEqual(parser._infer_type_code({"question_kind": "scale"}), "5")
        self.assertEqual(parser._infer_type_code({"question_kind": "order"}), "11")
        self.assertEqual(parser._infer_type_code({"question_kind": "multiple"}), "4")

    def test_normalize_question_keeps_credamo_specific_type(self) -> None:
        question = parser._normalize_question(
            {
                "question_num": "Q3",
                "title": "Q3",
                "question_kind": "dropdown",
                "provider_type": "dropdown",
                "option_texts": ["选项 1", "选项 2", "选项 3"],
                "text_inputs": 0,
                "page": 2,
                "question_id": "question-2",
            },
            fallback_num=3,
        )

        self.assertEqual(question["num"], 3)
        self.assertEqual(question["type_code"], "7")
        self.assertEqual(question["provider_type"], "dropdown")
        self.assertEqual(question["provider_page_id"], "2")
        self.assertEqual(question["options"], 3)

    def test_order_entry_is_exposed_to_runtime_mapping(self) -> None:
        entry = QuestionEntry(
            question_type="order",
            probabilities=-1,
            option_count=4,
            question_num=6,
            question_title="排序题",
            survey_provider="credamo",
        )
        ctx = SimpleNamespace()

        configure_probabilities([entry], ctx)

        self.assertEqual(ctx.question_config_index_map[6], ("order", -1))


if __name__ == "__main__":
    unittest.main()
