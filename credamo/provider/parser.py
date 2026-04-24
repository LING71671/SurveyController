"""Credamo 见数问卷解析实现。"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from software.app.config import DEFAULT_FILL_TEXT
from software.core.engine.driver_factory import create_playwright_driver
from software.providers.common import SURVEY_PROVIDER_CREDAMO

_QUESTION_NUMBER_RE = re.compile(r"^\s*(?:Q|题目?)\s*(\d+)\b", re.IGNORECASE)
_TYPE_ONLY_TITLE_RE = re.compile(r"^\s*\[[^\]]+\]\s*$")
_MAX_PARSE_PAGES = 20
_PARSE_POLL_SECONDS = 0.2
_PARSE_PAGE_WAIT_SECONDS = 8.0
_NEXT_BUTTON_MARKERS = ("下一页", "next", "继续")
_SUBMIT_BUTTON_MARKERS = ("提交", "完成", "交卷", "submit", "finish", "done")


class CredamoParseError(RuntimeError):
    """Credamo 页面结构无法解析时抛出的业务异常。"""


def _normalize_text(value: Any) -> str:
    try:
        text = str(value or "").strip()
    except Exception:
        return ""
    return re.sub(r"\s+", " ", text)


def _normalize_question_number(raw: Any, fallback_num: int) -> int:
    try:
        match = re.search(r"\d+", str(raw or ""))
        if match:
            return max(1, int(match.group(0)))
    except Exception:
        pass
    return max(1, int(fallback_num or 1))


def _infer_type_code(question: Dict[str, Any]) -> str:
    question_kind = str(question.get("question_kind") or "").strip().lower()
    input_types = {str(item or "").strip().lower() for item in question.get("input_types") or []}
    option_count = int(question.get("options") or 0)
    text_input_count = int(question.get("text_inputs") or 0)

    if question_kind == "multiple" or "checkbox" in input_types:
        return "4"
    if question_kind == "dropdown":
        return "7"
    if question_kind == "scale":
        return "5"
    if question_kind == "order":
        return "11"
    if question_kind == "single" or "radio" in input_types:
        return "3"
    if question_kind in {"text", "multi_text"} or text_input_count > 0:
        return "1"
    if option_count >= 2:
        return "3"
    return "1"


def _normalize_question(raw: Dict[str, Any], fallback_num: int) -> Dict[str, Any]:
    raw_title = _normalize_text(raw.get("title"))
    question_num = _normalize_question_number(raw.get("question_num"), fallback_num)
    title = raw_title
    match = _QUESTION_NUMBER_RE.match(raw_title)
    if match:
        question_num = _normalize_question_number(match.group(1), fallback_num)
        stripped_title = _normalize_text(raw_title[match.end():])
        if stripped_title and not _TYPE_ONLY_TITLE_RE.fullmatch(stripped_title):
            title = stripped_title
        else:
            title = raw_title or f"Q{question_num}"
    elif not title:
        title = f"Q{question_num}"

    option_texts = [_normalize_text(text) for text in raw.get("option_texts") or []]
    option_texts = [text for text in option_texts if text]
    text_inputs = max(0, int(raw.get("text_inputs") or 0))
    question_kind = str(raw.get("question_kind") or "").strip().lower()
    type_code = _infer_type_code({**raw, "options": len(option_texts), "text_inputs": text_inputs})

    normalized: Dict[str, Any] = {
        "num": question_num,
        "title": title or raw_title or f"Q{question_num}",
        "description": "",
        "type_code": type_code,
        "options": len(option_texts),
        "rows": 1,
        "row_texts": [],
        "page": max(1, int(raw.get("page") or 1)),
        "option_texts": option_texts,
        "provider": SURVEY_PROVIDER_CREDAMO,
        "provider_question_id": str(raw.get("question_id") or question_num),
        "provider_page_id": str(raw.get("page") or 1),
        "provider_type": str(raw.get("provider_type") or question_kind or type_code).strip(),
        "required": bool(raw.get("required")),
        "text_inputs": text_inputs,
        "text_input_labels": [],
        "is_text_like": question_kind in {"text", "multi_text"} or (text_inputs > 0 and not option_texts),
        "is_multi_text": question_kind == "multi_text" or text_inputs > 1,
        "is_rating": False,
        "rating_max": 0,
    }
    if normalized["type_code"] == "5":
        normalized["rating_max"] = max(len(option_texts), 1)
    return normalized


def _extract_questions_from_current_page(page: Any, *, page_number: int) -> List[Dict[str, Any]]:
    script = r"""
() => {
  const visible = (el, minWidth = 8, minHeight = 8) => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden') return false;
    const rect = el.getBoundingClientRect();
    return rect.width >= minWidth && rect.height >= minHeight;
  };
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const uniqueTexts = (values) => {
    const seen = new Set();
    const result = [];
    for (const raw of values || []) {
      const text = clean(raw);
      if (!text || seen.has(text)) continue;
      seen.add(text);
      result.push(text);
    }
    return result;
  };
  const data = [];
  const roots = Array.from(document.querySelectorAll('.answer-page .question'));
  roots.forEach((root, index) => {
    if (!visible(root)) return;

    const editableInputs = Array.from(
      root.querySelectorAll(
        'textarea, input:not([readonly])[type="text"], input:not([readonly])[type="search"], input:not([readonly])[type="number"], input:not([readonly])[type="tel"], input:not([readonly])[type="email"], input:not([readonly]):not([type])'
      )
    ).filter((node) => visible(node, 4, 4));
    const allInputs = Array.from(root.querySelectorAll('input, textarea, [role="radio"], [role="checkbox"]'));

    let kind = '';
    if (root.querySelector('.multi-choice') || root.querySelector('input[type="checkbox"]') || root.querySelector('[role="checkbox"]')) {
      kind = 'multiple';
    } else if (root.querySelector('.pc-dropdown') || root.querySelector('.el-select')) {
      kind = 'dropdown';
    } else if (root.querySelector('.scale') || root.querySelector('.nps-item') || root.querySelector('.el-rate__item')) {
      kind = 'scale';
    } else if (root.querySelector('.rank-order')) {
      kind = 'order';
    } else if (editableInputs.length > 1) {
      kind = 'multi_text';
    } else if (editableInputs.length > 0) {
      kind = 'text';
    } else if (root.querySelector('.single-choice') || root.querySelector('input[type="radio"]') || root.querySelector('[role="radio"]')) {
      kind = 'single';
    }

    const qstNoNode = root.querySelector('.question-title .qstNo');
    const titleTextNode = root.querySelector('.question-title .title-text');
    const titleInnerNode = root.querySelector('.question-title .title-inner');
    const tipNode = root.querySelector('.question-title .tip');
    const qstNo = clean(qstNoNode ? qstNoNode.textContent : '');
    let titleText = clean((titleTextNode && titleTextNode.innerText) || (titleInnerNode && titleInnerNode.innerText) || '');
    const tipText = clean((tipNode && tipNode.innerText) || '');
    if (tipText && titleText === tipText) {
      titleText = '';
    }
    const fullTitle = clean([qstNo, titleText, tipText].filter(Boolean).join(' '));

    const choiceTexts = uniqueTexts(Array.from(root.querySelectorAll('.choice-text')).map((node) => node.innerText || node.textContent || ''));
    const dropdownTexts = uniqueTexts(Array.from(root.querySelectorAll('.el-select-dropdown__item, option')).map((node) => node.innerText || node.textContent || ''));
    const scaleTexts = uniqueTexts(Array.from(root.querySelectorAll('.scale .nps-item, .el-rate__item')).map((node) => node.innerText || node.textContent || ''));
    let optionTexts = [];
    if (kind === 'dropdown' && dropdownTexts.length) optionTexts = dropdownTexts;
    else if (kind === 'scale' && scaleTexts.length) optionTexts = scaleTexts;
    else if (choiceTexts.length) optionTexts = choiceTexts;
    else if (dropdownTexts.length) optionTexts = dropdownTexts;
    else optionTexts = scaleTexts;

    const inputTypes = allInputs.map((input) => {
      const role = clean(input.getAttribute('role')).toLowerCase();
      if (role) return role;
      if (input.tagName.toLowerCase() === 'textarea') return 'textarea';
      return clean(input.getAttribute('type')).toLowerCase() || 'text';
    });
    const bodyText = clean(root.innerText || '');
    if (!kind && !optionTexts.length && editableInputs.length <= 0 && !bodyText) return;

    data.push({
      question_id: root.getAttribute('data-id') || root.getAttribute('id') || String(index + 1),
      question_num: qstNo,
      title: fullTitle || bodyText.split(' ').slice(0, 12).join(' '),
      option_texts: optionTexts,
      input_types: inputTypes,
      text_inputs: editableInputs.length,
      required: /必答|必须|required/i.test(bodyText),
      provider_type: kind || Array.from(new Set(inputTypes)).join(','),
      question_kind: kind,
    });
  });
  return data;
}
"""
    try:
        data = page.evaluate(script)
    except Exception as exc:
        raise CredamoParseError(f"无法读取 Credamo 页面题目结构：{exc}") from exc
    if not isinstance(data, list):
        return []

    questions: List[Dict[str, Any]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            continue
        normalized = _normalize_question({**item, "page": page_number}, fallback_num=index)
        questions.append(normalized)
    return questions


def _locator_count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return 0


def _text_content(locator: Any) -> str:
    try:
        return _normalize_text(locator.text_content(timeout=500))
    except Exception:
        return ""


def _detect_navigation_action(page: Any) -> Optional[str]:
    locator = page.locator("button, a, [role='button'], input[type='button'], input[type='submit']")
    count = _locator_count(locator)
    found_next = False
    for index in range(count):
        item = locator.nth(index)
        text = _text_content(item) or _normalize_text(item.get_attribute("value"))
        lowered = text.casefold()
        if any(marker in lowered for marker in _SUBMIT_BUTTON_MARKERS):
            return "submit"
        if any(marker in lowered for marker in _NEXT_BUTTON_MARKERS):
            found_next = True
    return "next" if found_next else None


def _click_navigation(page: Any, action: str) -> bool:
    primary_button = page.locator("#credamo-submit-btn").first
    if _locator_count(primary_button) > 0:
        try:
            primary_text = (_text_content(primary_button) or _normalize_text(primary_button.get_attribute("value"))).casefold()
        except Exception:
            primary_text = ""
        if action == "next" and any(marker in primary_text for marker in _NEXT_BUTTON_MARKERS):
            try:
                primary_button.click(timeout=3000)
                return True
            except Exception:
                try:
                    handle = primary_button.element_handle(timeout=1000)
                    if handle is not None and bool(page.evaluate("el => { el.click(); return true; }", handle)):
                        return True
                except Exception:
                    pass
        if action == "submit" and any(marker in primary_text for marker in _SUBMIT_BUTTON_MARKERS):
            try:
                primary_button.click(timeout=3000)
                return True
            except Exception:
                try:
                    handle = primary_button.element_handle(timeout=1000)
                    if handle is not None and bool(page.evaluate("el => { el.click(); return true; }", handle)):
                        return True
                except Exception:
                    pass

    targets = _NEXT_BUTTON_MARKERS if action == "next" else _SUBMIT_BUTTON_MARKERS
    locator = page.locator("button, a, [role='button'], input[type='button'], input[type='submit']")
    count = _locator_count(locator)
    for index in range(count):
        item = locator.nth(index)
        text = (_text_content(item) or _normalize_text(item.get_attribute("value"))).casefold()
        if not any(marker in text for marker in targets):
            continue
        try:
            item.scroll_into_view_if_needed(timeout=1000)
        except Exception:
            pass
        try:
            item.click(timeout=3000)
            return True
        except Exception:
            try:
                handle = item.element_handle(timeout=1000)
                if handle is not None and bool(page.evaluate("el => { el.click(); return true; }", handle)):
                    return True
            except Exception:
                continue
    return False


def _extract_page_signature(questions: List[Dict[str, Any]]) -> Tuple[Tuple[str, str], ...]:
    return tuple(
        (str(item.get("provider_question_id") or ""), str(item.get("title") or ""))
        for item in questions
    )


def _wait_for_page_change(page: Any, previous_signature: Tuple[Tuple[str, str], ...], *, page_number: int) -> bool:
    deadline = time.monotonic() + _PARSE_PAGE_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_PARSE_POLL_SECONDS)
        current_questions = _extract_questions_from_current_page(page, page_number=page_number)
        current_signature = _extract_page_signature(current_questions)
        if current_signature and current_signature != previous_signature:
            return True
    return False


def _prime_question_for_next(page: Any, root: Any, question: Dict[str, Any]) -> None:
    from credamo.provider.runtime import (
        _answer_dropdown,
        _answer_multiple,
        _answer_order,
        _answer_scale,
        _answer_single_like,
        _answer_text,
    )

    kind = str(question.get("provider_type") or question.get("type_code") or "").strip().lower()
    option_count = max(1, int(question.get("options") or 0))
    first_option_weights = [100.0] + [0.0] * max(0, option_count - 1)
    middle_index = min(max(option_count // 2, 0), max(option_count - 1, 0))
    middle_weights = [0.0] * option_count
    if middle_weights:
        middle_weights[middle_index] = 100.0
    if kind in {"single", "3"}:
        _answer_single_like(page, root, first_option_weights, option_count)
    elif kind in {"multiple", "4"}:
        _answer_multiple(page, root, first_option_weights)
    elif kind in {"dropdown", "7"}:
        _answer_dropdown(page, root, first_option_weights)
    elif kind in {"scale", "5", "score"}:
        _answer_scale(page, root, middle_weights)
    elif kind in {"order", "11"}:
        _answer_order(page, root)
    else:
        _answer_text(root, [DEFAULT_FILL_TEXT])


def _prime_page_for_next(page: Any, questions: List[Dict[str, Any]]) -> None:
    from credamo.provider.runtime import _question_roots

    roots = _question_roots(page)
    for question, root in zip(questions, roots):
        try:
            _prime_question_for_next(page, root, question)
        except Exception:
            logging.info("Credamo 解析翻页预填题目失败", exc_info=True)


def parse_credamo_survey(url: str) -> Tuple[List[Dict[str, Any]], str]:
    driver = None
    try:
        driver, _browser_name = create_playwright_driver(
            headless=True,
            prefer_browsers=["edge", "chrome"],
            persistent_browser=False,
            transient_launch=True,
        )
        driver.get(url, timeout=30000, wait_until="domcontentloaded")
        page = driver.page
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        try:
            page.wait_for_selector(".answer-page .question", timeout=15000)
        except Exception as exc:
            logging.info("Credamo 解析等待题目控件超时：%s", exc)
        questions: List[Dict[str, Any]] = []
        seen_question_ids: set[str] = set()
        title = _normalize_text(page.title())

        for page_number in range(1, _MAX_PARSE_PAGES + 1):
            current_questions = _extract_questions_from_current_page(page, page_number=page_number)
            if not current_questions:
                if not questions:
                    raise CredamoParseError("没有识别到 Credamo 题目，请确认链接已开放且无需登录")
                break

            for question in current_questions:
                question_id = str(question.get("provider_question_id") or "").strip()
                if question_id and question_id in seen_question_ids:
                    continue
                if question_id:
                    seen_question_ids.add(question_id)
                questions.append(question)

            navigation_action = _detect_navigation_action(page)
            if navigation_action != "next":
                break

            previous_signature = _extract_page_signature(current_questions)
            _prime_page_for_next(page, current_questions)
            if not _click_navigation(page, "next"):
                break
            _wait_for_page_change(page, previous_signature, page_number=page_number + 1)

        if not questions:
            raise CredamoParseError("没有识别到 Credamo 题目，请确认链接已开放且无需登录")
        if not title:
            try:
                title = _normalize_text(
                    page.locator("h1, .title, [class*='title'], [class*='Title']").first.text_content(timeout=1000)
                )
            except Exception:
                title = ""
        return questions, title or "Credamo 见数问卷"
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                logging.info("关闭 Credamo 解析浏览器失败", exc_info=True)
