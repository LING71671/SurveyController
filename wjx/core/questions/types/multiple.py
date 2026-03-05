"""多选题处理"""
import json
import logging
import math
import random
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from wjx.network.browser import By, BrowserDriver, NoSuchElementException
from wjx.utils.app.config import (
    _MULTI_LIMIT_ATTRIBUTE_NAMES,
    _MULTI_LIMIT_VALUE_KEYSET,
    _MULTI_MIN_LIMIT_ATTRIBUTE_NAMES,
    _MULTI_MIN_LIMIT_VALUE_KEYSET,
    _SELECTION_KEYWORDS_CN,
    _SELECTION_KEYWORDS_EN,
    _CHINESE_MULTI_LIMIT_PATTERNS,
    _CHINESE_MULTI_RANGE_PATTERNS,
    _CHINESE_MULTI_MIN_PATTERNS,
    _ENGLISH_MULTI_LIMIT_PATTERNS,
    _ENGLISH_MULTI_RANGE_PATTERNS,
    _ENGLISH_MULTI_MIN_PATTERNS,
)
from wjx.core.questions.utils import (
    get_fill_text_from_config,
    fill_option_additional_text,
    extract_text_from_element,
)
from wjx.core.persona.context import apply_persona_boost, record_answer

# 缓存检测到的多选限制
_DETECTED_MULTI_LIMITS: Dict[Tuple[str, int], Optional[int]] = {}
_DETECTED_MULTI_LIMIT_RANGES: Dict[Tuple[str, int], Tuple[Optional[int], Optional[int]]] = {}
_REPORTED_MULTI_LIMITS: Set[Tuple[str, int]] = set()
# 缓存已警告过概率不匹配的题目（避免重复警告）
_WARNED_PROB_MISMATCH: Set[int] = set()
_WARNED_OPTION_LOCATOR: Set[int] = set()


def clear_multiple_choice_cache() -> None:
    """清理多选题缓存（在任务结束时调用）"""
    global _DETECTED_MULTI_LIMITS, _DETECTED_MULTI_LIMIT_RANGES, _REPORTED_MULTI_LIMITS, _WARNED_PROB_MISMATCH, _WARNED_OPTION_LOCATOR
    _DETECTED_MULTI_LIMITS.clear()
    _DETECTED_MULTI_LIMIT_RANGES.clear()
    _REPORTED_MULTI_LIMITS.clear()
    _WARNED_PROB_MISMATCH.clear()
    _WARNED_OPTION_LOCATOR.clear()


def _safe_positive_int(value: Any) -> Optional[int]:
    """安全转换为正整数"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        int_value = int(value)
        return int_value if int_value > 0 else None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    if text.isdigit():
        int_value = int(text)
        return int_value if int_value > 0 else None
    match = re.search(r"(\d+)", text)
    if match:
        int_value = int(match.group(1))
        return int_value if int_value > 0 else None
    return None


def _extract_range_from_json_obj(obj: Any) -> Tuple[Optional[int], Optional[int]]:
    """从JSON对象提取范围"""
    min_limit: Optional[int] = None
    max_limit: Optional[int] = None
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized_key = str(key).lower()
            if normalized_key in _MULTI_MIN_LIMIT_VALUE_KEYSET:
                candidate = _safe_positive_int(value)
                if candidate:
                    min_limit = min_limit or candidate
            if normalized_key in _MULTI_LIMIT_VALUE_KEYSET:
                candidate = _safe_positive_int(value)
                if candidate:
                    max_limit = max_limit or candidate
            nested_min, nested_max = _extract_range_from_json_obj(value)
            if min_limit is None and nested_min is not None:
                min_limit = nested_min
            if max_limit is None and nested_max is not None:
                max_limit = nested_max
            if min_limit is not None and max_limit is not None:
                break
    elif isinstance(obj, list):
        for item in obj:
            nested_min, nested_max = _extract_range_from_json_obj(item)
            if min_limit is None and nested_min is not None:
                min_limit = nested_min
            if max_limit is None and nested_max is not None:
                max_limit = nested_max
            if min_limit is not None and max_limit is not None:
                break
    return min_limit, max_limit


def _extract_range_from_possible_json(text: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """从可能的JSON文本提取范围"""
    min_limit: Optional[int] = None
    max_limit: Optional[int] = None
    if not text:
        return min_limit, max_limit
    normalized = text.strip()
    if not normalized:
        return min_limit, max_limit
    candidates = [normalized]
    if normalized.startswith("{") and "'" in normalized and '"' not in normalized:
        candidates.append(normalized.replace("'", '"'))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        cand_min, cand_max = _extract_range_from_json_obj(parsed)
        if min_limit is None and cand_min is not None:
            min_limit = cand_min
        if max_limit is None and cand_max is not None:
            max_limit = cand_max
        if min_limit is not None and max_limit is not None:
            return min_limit, max_limit
    for key in _MULTI_MIN_LIMIT_VALUE_KEYSET:
        pattern = re.compile(rf"{re.escape(key)}\s*[:=]\s*(\d+)", re.IGNORECASE)
        match = pattern.search(normalized)
        if match:
            candidate = _safe_positive_int(match.group(1))
            if candidate:
                min_limit = min_limit or candidate
                if max_limit is not None:
                    return min_limit, max_limit
    for key in _MULTI_LIMIT_VALUE_KEYSET:
        pattern = re.compile(rf"{re.escape(key)}\s*[:=]\s*(\d+)", re.IGNORECASE)
        match = pattern.search(normalized)
        if match:
            candidate = _safe_positive_int(match.group(1))
            if candidate:
                max_limit = max_limit or candidate
                if min_limit is not None:
                    return min_limit, max_limit
    return min_limit, max_limit


def _extract_min_max_from_attributes(element) -> Tuple[Optional[int], Optional[int]]:
    """从元素属性提取最小最大值"""
    min_limit = None
    max_limit = None
    for attr in _MULTI_MIN_LIMIT_ATTRIBUTE_NAMES:
        try:
            raw_value = element.get_attribute(attr)
        except Exception:
            continue
        candidate = _safe_positive_int(raw_value)
        if candidate:
            min_limit = candidate
            break
    for attr in _MULTI_LIMIT_ATTRIBUTE_NAMES:
        try:
            raw_value = element.get_attribute(attr)
        except Exception:
            continue
        candidate = _safe_positive_int(raw_value)
        if candidate:
            max_limit = candidate
            break
    return min_limit, max_limit


def _extract_multi_limit_range_from_text(text: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """从文本提取多选限制范围"""
    if not text:
        return None, None
    normalized = text.strip()
    if not normalized:
        return None, None
    normalized_lower = normalized.lower()
    min_limit: Optional[int] = None
    max_limit: Optional[int] = None
    contains_cn_keyword = any(keyword in normalized for keyword in _SELECTION_KEYWORDS_CN)
    contains_en_keyword = any(keyword in normalized_lower for keyword in _SELECTION_KEYWORDS_EN)
    if contains_cn_keyword:
        for pattern in _CHINESE_MULTI_RANGE_PATTERNS:
            match = pattern.search(normalized)
            if match:
                first = _safe_positive_int(match.group(1))
                second = _safe_positive_int(match.group(2))
                if first and second:
                    min_limit = min(first, second)
                    max_limit = max(first, second)
                    break
    if min_limit is None and max_limit is None and contains_en_keyword:
        for pattern in _ENGLISH_MULTI_RANGE_PATTERNS:
            match = pattern.search(normalized)
            if match:
                first = _safe_positive_int(match.group(1))
                second = _safe_positive_int(match.group(2))
                if first and second:
                    min_limit = min(first, second)
                    max_limit = max(first, second)
                    break
    if min_limit is None and contains_cn_keyword:
        for pattern in _CHINESE_MULTI_MIN_PATTERNS:
            match = pattern.search(normalized)
            if match:
                candidate = _safe_positive_int(match.group(1))
                if candidate:
                    min_limit = candidate
                    break
    if max_limit is None and contains_cn_keyword:
        for pattern in _CHINESE_MULTI_LIMIT_PATTERNS:
            match = pattern.search(normalized)
            if match:
                candidate = _safe_positive_int(match.group(1))
                if candidate:
                    max_limit = candidate
                    break
    if min_limit is None and contains_en_keyword:
        for pattern in _ENGLISH_MULTI_MIN_PATTERNS:
            match = pattern.search(normalized_lower)
            if match:
                candidate = _safe_positive_int(match.group(1))
                if candidate:
                    min_limit = candidate
                    break
    if max_limit is None and contains_en_keyword:
        for pattern in _ENGLISH_MULTI_LIMIT_PATTERNS:
            match = pattern.search(normalized_lower)
            if match:
                candidate = _safe_positive_int(match.group(1))
                if candidate:
                    max_limit = candidate
                    break
    if min_limit is not None and max_limit is not None and min_limit > max_limit:
        min_limit, max_limit = max_limit, min_limit
    return min_limit, max_limit


def _get_driver_session_key(driver: BrowserDriver) -> str:
    """获取驱动会话键"""
    session_id = getattr(driver, "session_id", None)
    if session_id:
        return str(session_id)
    return f"id-{id(driver)}"


def detect_multiple_choice_limit_range(driver: BrowserDriver, question_number: int) -> Tuple[Optional[int], Optional[int]]:
    """检测多选题的选择数量限制范围"""
    cache_key = (_get_driver_session_key(driver), question_number)
    if cache_key in _DETECTED_MULTI_LIMIT_RANGES:
        return _DETECTED_MULTI_LIMIT_RANGES[cache_key]
    min_limit: Optional[int] = None
    max_limit: Optional[int] = None
    try:
        container = driver.find_element(By.CSS_SELECTOR, f"#div{question_number}")
    except NoSuchElementException:
        container = None
    if container is not None:
        attr_min, attr_max = _extract_min_max_from_attributes(container)
        if attr_min is not None:
            min_limit = attr_min
        if attr_max is not None:
            max_limit = attr_max
        if min_limit is None or max_limit is None:
            for attr_name in ("data", "data-setting", "data-validate"):
                cand_min, cand_max = _extract_range_from_possible_json(container.get_attribute(attr_name))
                if min_limit is None and cand_min is not None:
                    min_limit = cand_min
                if max_limit is None and cand_max is not None:
                    max_limit = cand_max
                if min_limit is not None and max_limit is not None:
                    break
        if min_limit is None or max_limit is None:
            fragments: List[str] = []
            for selector in (".qtypetip", ".topichtml", ".field-label"):
                try:
                    fragments.append(container.find_element(By.CSS_SELECTOR, selector).text)
                except Exception:
                    continue
            fragments.append(container.text)
            for fragment in fragments:
                cand_min, cand_max = _extract_multi_limit_range_from_text(fragment)
                if min_limit is None and cand_min is not None:
                    min_limit = cand_min
                if max_limit is None and cand_max is not None:
                    max_limit = cand_max
                if min_limit is not None and max_limit is not None:
                    break
        if min_limit is None or max_limit is None:
            html = container.get_attribute("outerHTML")
            cand_min, cand_max = _extract_range_from_possible_json(html)
            if min_limit is None and cand_min is not None:
                min_limit = cand_min
            if max_limit is None and cand_max is not None:
                max_limit = cand_max
            if min_limit is None or max_limit is None:
                cand_min, cand_max = _extract_multi_limit_range_from_text(html)
                if min_limit is None and cand_min is not None:
                    min_limit = cand_min
                if max_limit is None and cand_max is not None:
                    max_limit = cand_max
    if min_limit is not None and max_limit is not None and min_limit > max_limit:
        min_limit, max_limit = max_limit, min_limit
    _DETECTED_MULTI_LIMIT_RANGES[cache_key] = (min_limit, max_limit)
    _DETECTED_MULTI_LIMITS[cache_key] = max_limit
    return min_limit, max_limit


def detect_multiple_choice_limit(driver: BrowserDriver, question_number: int) -> Optional[int]:
    """检测多选题的最大选择数量限制"""
    _, max_limit = detect_multiple_choice_limit_range(driver, question_number)
    return max_limit


def _log_multi_limit_once(driver: BrowserDriver, question_number: int, min_limit: Optional[int], max_limit: Optional[int]) -> None:
    """仅记录一次多选限制日志"""
    if min_limit is None and max_limit is None:
        return
    cache_key = (_get_driver_session_key(driver), question_number)
    if cache_key in _REPORTED_MULTI_LIMITS:
        return
    _REPORTED_MULTI_LIMITS.add(cache_key)


def _warn_option_locator_once(question_number: int, message: str, *args: Any) -> None:
    """选项定位异常只告警一次，避免日志刷屏。"""
    if question_number in _WARNED_OPTION_LOCATOR:
        return
    _WARNED_OPTION_LOCATOR.add(question_number)
    logging.warning(message, *args)


def _looks_like_multiple_option(element: Any) -> bool:
    """判断元素是否像多选题真实选项。"""
    try:
        class_name = (element.get_attribute("class") or "").lower()
    except Exception:
        class_name = ""
    if any(token in class_name for token in ("ui-checkbox", "jqcheck", "check", "option")):
        return True
    try:
        element_type = (element.get_attribute("type") or "").lower()
    except Exception:
        element_type = ""
    if element_type == "checkbox":
        return True
    try:
        if element.find_elements(By.CSS_SELECTOR, "input[type='checkbox'], .jqcheck, .ui-checkbox"):
            return True
    except Exception:
        return False
    return False


def _collect_multiple_option_elements(driver: BrowserDriver, question_number: int) -> Tuple[List[Any], str]:
    """收集多选题选项元素，兼容不同 DOM 模板。"""
    try:
        container = driver.find_element(By.CSS_SELECTOR, f"#div{question_number}")
    except Exception:
        return [], "container-missing"

    selector_chain = [
        ("css:#div .ui-controlgroup > div", ".ui-controlgroup > div"),
        ("css:#div .ui-controlgroup li", ".ui-controlgroup li"),
        ("css:#div ul > li", "ul > li"),
        ("css:#div ol > li", "ol > li"),
        ("css:#div .option", ".option"),
        ("css:#div .ui-checkbox", ".ui-checkbox"),
        ("css:#div .jqcheck", ".jqcheck"),
    ]
    seen: Set[str] = set()

    for source, selector in selector_chain:
        try:
            found = container.find_elements(By.CSS_SELECTOR, selector)
        except Exception:
            found = []
        options: List[Any] = []
        for elem in found:
            try:
                if not elem.is_displayed():
                    continue
            except Exception:
                continue
            if not _looks_like_multiple_option(elem):
                continue
            elem_key = str(getattr(elem, "id", None) or id(elem))
            if elem_key in seen:
                continue
            seen.add(elem_key)
            options.append(elem)
        if options:
            return options, source

    # 兜底：直接用 checkbox input（某些模板没有可点击容器）
    try:
        checkbox_inputs = container.find_elements(By.CSS_SELECTOR, "input[type='checkbox']")
    except Exception:
        checkbox_inputs = []
    options = []
    for elem in checkbox_inputs:
        try:
            if not elem.is_displayed():
                continue
        except Exception:
            continue
        elem_key = str(getattr(elem, "id", None) or id(elem))
        if elem_key in seen:
            continue
        seen.add(elem_key)
        options.append(elem)
    if options:
        return options, "css:#div input[type=checkbox]"
    return [], "no-option-found"


def _is_multiple_option_selected(driver: BrowserDriver, option_element: Any) -> bool:
    """检查多选选项是否已选中。"""
    try:
        selected = driver.execute_script(
            """
            const el = arguments[0];
            if (!el) return false;
            const isCheckbox = (typeof el.matches === 'function') && el.matches("input[type='checkbox']");
            const checkbox = isCheckbox ? el : (el.querySelector ? el.querySelector("input[type='checkbox']") : null);
            if (checkbox) return !!checkbox.checked;
            if (el.classList && (el.classList.contains('checked') || el.classList.contains('on') || el.classList.contains('jqchecked'))) {
                return true;
            }
            const marked = el.querySelector
                ? el.querySelector(".jqcheck.checked, .jqcheck.jqchecked, .ui-checkbox.checked, .ui-checkbox.on")
                : null;
            return !!marked;
            """,
            option_element,
        )
        return bool(selected)
    except Exception:
        return False


def _click_multiple_option(driver: BrowserDriver, option_element: Any) -> bool:
    """稳健点击多选项，点击后必须验收选中状态。"""
    if option_element is None:
        return False
    if _is_multiple_option_selected(driver, option_element):
        return True

    click_candidates: List[Any] = [option_element]
    try:
        click_candidates.extend(
            option_element.find_elements(
                By.CSS_SELECTOR,
                ".label, label, .jqcheck, .ui-checkbox, input[type='checkbox'], a, span, div",
            )
        )
    except Exception:
        pass

    seen: Set[str] = set()
    for candidate in click_candidates:
        cand_key = str(getattr(candidate, "id", None) or id(candidate))
        if cand_key in seen:
            continue
        seen.add(cand_key)
        try:
            candidate.click()
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", candidate)
            except Exception:
                continue
        if _is_multiple_option_selected(driver, option_element):
            return True

    try:
        forced = driver.execute_script(
            """
            const el = arguments[0];
            if (!el) return false;
            const isCheckbox = (typeof el.matches === 'function') && el.matches("input[type='checkbox']");
            const checkbox = isCheckbox ? el : (el.querySelector ? el.querySelector("input[type='checkbox']") : null);
            if (checkbox) {
                try { checkbox.click(); } catch (e) {}
                if (!checkbox.checked) {
                    checkbox.checked = true;
                    try { checkbox.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}
                    try { checkbox.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
                    try { checkbox.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true })); } catch (e) {}
                }
                return !!checkbox.checked;
            }
            try { el.click(); } catch (e) {}
            return true;
            """,
            option_element,
        )
        if bool(forced) and _is_multiple_option_selected(driver, option_element):
            return True
    except Exception:
        return False
    return False


def _normalize_selected_indices(indices: List[int], option_count: int) -> List[int]:
    """去重并过滤越界索引，保持原始顺序。"""
    normalized: List[int] = []
    seen: Set[int] = set()
    for idx in indices:
        if idx in seen:
            continue
        if idx < 0 or idx >= option_count:
            continue
        seen.add(idx)
        normalized.append(idx)
    return normalized


def multiple(driver: BrowserDriver, current: int, index: int, multiple_prob_config: List, multiple_option_fill_texts_config: List) -> None:
    """多选题处理主函数"""
    option_elements, option_source = _collect_multiple_option_elements(driver, current)
    if not option_elements:
        _warn_option_locator_once(
            current,
            "第%d题（多选）：未找到可用选项容器，已跳过（定位来源=%s）。",
            current,
            option_source,
        )
        return
    if option_source != "css:#div .ui-controlgroup > div":
        _warn_option_locator_once(
            current,
            "第%d题（多选）：选项容器结构异常，已使用回退定位（%s）。",
            current,
            option_source,
        )

    min_select_limit, max_select_limit = detect_multiple_choice_limit_range(driver, current)
    max_allowed = max_select_limit if max_select_limit is not None else len(option_elements)
    max_allowed = max(1, min(max_allowed, len(option_elements)))
    min_required = min_select_limit if min_select_limit is not None else 1
    min_required = max(1, min(min_required, len(option_elements)))
    if min_required > max_allowed:
        min_required = max_allowed
    _log_multi_limit_once(driver, current, min_select_limit, max_select_limit)
    selection_probabilities = multiple_prob_config[index] if index < len(multiple_prob_config) else [50.0] * len(option_elements)
    fill_entries = multiple_option_fill_texts_config[index] if index < len(multiple_option_fill_texts_config) else None

    # 提取选项文本，用于画像约束和上下文记录
    option_texts: List[str] = []
    for elem in option_elements:
        option_texts.append(extract_text_from_element(elem))

    def _apply_selected_indices(selected_indices: List[int]) -> List[int]:
        confirmed_indices: List[int] = []
        for option_idx in selected_indices:
            option_element = option_elements[option_idx]
            if not _click_multiple_option(driver, option_element):
                logging.warning(
                    "第%d题（多选）：第 %d 个选项点击失败，已跳过。",
                    current,
                    option_idx + 1,
                )
                continue
            confirmed_indices.append(option_idx)
            fill_value = get_fill_text_from_config(fill_entries, option_idx)
            fill_option_additional_text(driver, current, option_idx, fill_value)
        return confirmed_indices

    if selection_probabilities == -1 or (isinstance(selection_probabilities, list) and len(selection_probabilities) == 1 and selection_probabilities[0] == -1):
        pool = list(range(len(option_elements)))
        max_select = min(max_allowed, len(pool))
        num_to_select = random.randint(min_required, max_select)
        selected_indices = random.sample(pool, num_to_select)
        selected_indices = _normalize_selected_indices(selected_indices, len(option_elements))
        confirmed_indices = _apply_selected_indices(selected_indices)
        if not confirmed_indices:
            logging.warning("第%d题（多选）：随机模式点击失败，未选中任何选项。", current)
            return
        # 记录统计数据
        # 记录作答上下文
        selected_texts = [option_texts[i] for i in confirmed_indices if i < len(option_texts)]
        record_answer(current, "multiple", selected_indices=confirmed_indices, selected_texts=selected_texts)
        return

    if len(option_elements) != len(selection_probabilities):
        if len(selection_probabilities) > len(option_elements):
            # 截断：配置多于实际选项，丢弃多余部分，值得提醒
            if current not in _WARNED_PROB_MISMATCH:
                _WARNED_PROB_MISMATCH.add(current)
                logging.warning(
                    "第%d题（多选）：配置概率数(%d)多于页面实际选项数(%d)，多余部分已截断。",
                    current, len(selection_probabilities), len(option_elements)
                )
            selection_probabilities = selection_probabilities[: len(option_elements)]
        else:
            # 扩展：配置少于实际选项，用0补齐（未配置的选项不会被选中），属正常行为
            padding = [0.0] * (len(option_elements) - len(selection_probabilities))
            selection_probabilities = list(selection_probabilities) + padding
    sanitized_probabilities: List[float] = []
    for raw_prob in selection_probabilities:
        try:
            prob_value = float(raw_prob)
        except Exception:
            prob_value = 0.0
        if math.isnan(prob_value) or math.isinf(prob_value):
            prob_value = 0.0
        prob_value = max(0.0, min(100.0, prob_value))
        sanitized_probabilities.append(prob_value)
    selection_probabilities = sanitized_probabilities

    # 画像约束：对匹配画像的选项概率加成
    # 多选题概率是 0-100 的百分比，加成后上限仍为 100
    boosted = apply_persona_boost(option_texts, selection_probabilities)
    selection_probabilities = [min(100.0, p) for p in boosted]

    selection_mask: List[int] = []
    attempts = 0
    max_attempts = 32
    positive_indices = [i for i, p in enumerate(selection_probabilities) if p > 0]
    if not positive_indices:
        if current not in _WARNED_PROB_MISMATCH:
            _WARNED_PROB_MISMATCH.add(current)
            logging.warning(
                "第%d题（多选）：所有选项概率都 <= 0，已跳过本题作答；请在配置中至少保留一个 > 0%% 的选项。",
                current,
            )
        return
    while sum(selection_mask) == 0 and attempts < max_attempts:
        selection_mask = [1 if random.random() < (prob / 100.0) else 0 for prob in selection_probabilities]
        attempts += 1
    if sum(selection_mask) == 0:
        # 32次尝试都没选中任何选项，从正概率选项中随机选一个
        selection_mask = [0] * len(option_elements)
        selection_mask[random.choice(positive_indices)] = 1
    selected_indices = [
        idx
        for idx, selected in enumerate(selection_mask)
        if selected == 1 and selection_probabilities[idx] > 0
    ]
    if max_select_limit is not None and len(selected_indices) > max_allowed:
        random.shuffle(selected_indices)
        selected_indices = selected_indices[:max_allowed]
    if len(selected_indices) < min_required:
        # 补齐到最低选择数时，严格只从概率 > 0 的未选选项中补充
        remaining_positive = [i for i in positive_indices if i not in selected_indices]
        random.shuffle(remaining_positive)
        needed = min_required - len(selected_indices)

        if len(remaining_positive) >= needed:
            # 有足够的正概率选项可以补齐
            selected_indices.extend(remaining_positive[:needed])
        else:
            # 正概率选项不够，只补充现有的正概率选项，不强制选择 0% 概率的选项
            selected_indices.extend(remaining_positive)
            actual_min = len(selected_indices)
            if current not in _WARNED_PROB_MISMATCH:
                _WARNED_PROB_MISMATCH.add(current)
                logging.warning(
                    "第%d题（多选）：最低选择数要求为 %d，但配置的正概率选项只有 %d 个，"
                    "实际只选择 %d 个选项（严格遵守概率配置，不会选择 0%% 概率的选项）。",
                    current, min_required, len(positive_indices), actual_min
                )
    if not selected_indices:
        selected_indices = [random.choice(positive_indices)]
    selected_indices = _normalize_selected_indices(selected_indices, len(option_elements))
    confirmed_indices = _apply_selected_indices(selected_indices)
    if len(confirmed_indices) < min_required:
        logging.warning(
            "第%d题（多选）：题目最少需选 %d 项，实际点击成功 %d 项（正概率选项 %d）。",
            current,
            min_required,
            len(confirmed_indices),
            len(positive_indices),
        )
    if not confirmed_indices:
        return

    # 记录统计数据

    # 记录作答上下文
    selected_texts = [option_texts[i] for i in confirmed_indices if i < len(option_texts)]
    record_answer(current, "multiple", selected_indices=confirmed_indices, selected_texts=selected_texts)

