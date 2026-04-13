"""题目配置数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Union

from software.app.config import LOCATION_QUESTION_LABEL, QUESTION_TYPE_LABELS
from software.core.questions.text_shared import MULTI_TEXT_DELIMITER
from software.core.questions.utils import describe_random_int_range, parse_random_int_token
from software.logging.log_utils import log_suppressed_exception

_TEXT_RANDOM_NAME_TOKEN = "__RANDOM_NAME__"
_TEXT_RANDOM_MOBILE_TOKEN = "__RANDOM_MOBILE__"
_TEXT_RANDOM_ID_CARD_TOKEN = "__RANDOM_ID_CARD__"
_TEXT_RANDOM_NONE = "none"
_TEXT_RANDOM_NAME = "name"
_TEXT_RANDOM_MOBILE = "mobile"
_TEXT_RANDOM_ID_CARD = "id_card"
_TEXT_RANDOM_INTEGER = "integer"
GLOBAL_RELIABILITY_DIMENSION = "__global_reliability__"

__all__ = [
    "GLOBAL_RELIABILITY_DIMENSION",
    "QuestionEntry",
    "_TEXT_RANDOM_ID_CARD",
    "_TEXT_RANDOM_ID_CARD_TOKEN",
    "_TEXT_RANDOM_INTEGER",
    "_TEXT_RANDOM_MOBILE",
    "_TEXT_RANDOM_MOBILE_TOKEN",
    "_TEXT_RANDOM_NAME",
    "_TEXT_RANDOM_NAME_TOKEN",
    "_TEXT_RANDOM_NONE",
    "_infer_option_count",
    "get_entry_type_label",
]


def _pretty_text_answer(value: Any) -> str:
    text = str(value or "").strip()
    random_int_range = parse_random_int_token(text)
    if random_int_range is not None:
        return f"随机整数({describe_random_int_range(random_int_range)})"
    if text == _TEXT_RANDOM_NAME_TOKEN:
        return "随机姓名"
    if text == _TEXT_RANDOM_MOBILE_TOKEN:
        return "随机手机号"
    if text == _TEXT_RANDOM_ID_CARD_TOKEN:
        return "随机身份证"
    return text


def _infer_option_count(entry: "QuestionEntry") -> int:
    """当配置中缺少选项数量时，尽可能从已保存的权重/文本推导。"""

    def _nested_length(raw: Any) -> Optional[int]:
        if not isinstance(raw, list):
            return None
        lengths: List[int] = []
        for item in raw:
            if isinstance(item, (list, tuple)):
                lengths.append(len(item))
        return max(lengths) if lengths else None

    if getattr(entry, "question_type", "") == "matrix":
        nested_len = _nested_length(getattr(entry, "custom_weights", None))
        if nested_len:
            return nested_len
        nested_len = _nested_length(getattr(entry, "probabilities", None))
        if nested_len:
            return nested_len

    try:
        if entry.option_count and entry.option_count > 0:
            return int(entry.option_count)
    except Exception as exc:
        log_suppressed_exception("questions.schema._infer_option_count option_count", exc)
    try:
        if entry.custom_weights and len(entry.custom_weights) > 0:
            return len(entry.custom_weights)
    except Exception as exc:
        log_suppressed_exception("questions.schema._infer_option_count custom_weights", exc)
    try:
        if isinstance(entry.probabilities, (list, tuple)) and len(entry.probabilities) > 0:
            return len(entry.probabilities)
    except Exception as exc:
        log_suppressed_exception("questions.schema._infer_option_count probabilities", exc)
    try:
        if entry.texts and len(entry.texts) > 0:
            return len(entry.texts)
    except Exception as exc:
        log_suppressed_exception("questions.schema._infer_option_count texts", exc)
    if getattr(entry, "question_type", "") in ("scale", "score"):
        return 5
    return 0


@dataclass
class QuestionEntry:
    question_type: str
    probabilities: Union[List[float], List[List[float]], int, None]
    texts: Optional[List[str]] = None
    rows: int = 1
    option_count: int = 0
    distribution_mode: str = "random"
    custom_weights: Union[List[float], List[List[float]], None] = None
    question_num: Optional[int] = None
    question_title: Optional[str] = None
    survey_provider: str = "wjx"
    provider_question_id: Optional[str] = None
    provider_page_id: Optional[str] = None
    ai_enabled: bool = False
    multi_text_blank_modes: List[str] = field(default_factory=list)
    multi_text_blank_ai_flags: List[bool] = field(default_factory=list)
    multi_text_blank_int_ranges: List[List[int]] = field(default_factory=list)
    text_random_mode: str = _TEXT_RANDOM_NONE
    text_random_int_range: List[int] = field(default_factory=list)
    option_fill_texts: Optional[List[Optional[str]]] = None
    fillable_option_indices: Optional[List[int]] = None
    attached_option_selects: List[dict] = field(default_factory=list)
    is_location: bool = False
    dimension: Optional[str] = None
    psycho_bias: str = "custom"

    def summary(self) -> str:
        def _mode_text(mode: Optional[str]) -> str:
            return {
                "random": "完全随机",
                "custom": "自定义配比",
            }.get(mode or "", "完全随机")

        if self.question_type in ("text", "multi_text"):
            text_random_mode = str(getattr(self, "text_random_mode", _TEXT_RANDOM_NONE) or _TEXT_RANDOM_NONE).strip().lower()
            if self.question_type == "text" and text_random_mode in (_TEXT_RANDOM_NAME, _TEXT_RANDOM_MOBILE, _TEXT_RANDOM_ID_CARD, _TEXT_RANDOM_INTEGER):
                if text_random_mode == _TEXT_RANDOM_NAME:
                    random_label = "随机姓名"
                elif text_random_mode == _TEXT_RANDOM_MOBILE:
                    random_label = "随机手机号"
                elif text_random_mode == _TEXT_RANDOM_ID_CARD:
                    random_label = "随机身份证"
                else:
                    random_label = f"随机整数({describe_random_int_range(getattr(self, 'text_random_int_range', []))})"
                return f"填空题: {random_label}"
            raw_samples = self.texts or []
            if self.question_type == "multi_text":
                formatted_samples: List[str] = []
                for sample in raw_samples:
                    try:
                        text_value = str(sample).strip()
                    except Exception:
                        text_value = ""
                    if not text_value:
                        continue
                    if MULTI_TEXT_DELIMITER in text_value:
                        parts = [part.strip() for part in text_value.split(MULTI_TEXT_DELIMITER)]
                        parts = [part for part in parts if part]
                        formatted_samples.append(" / ".join(parts) if parts else text_value)
                    else:
                        formatted_samples.append(text_value)
                samples = " | ".join(formatted_samples)
            else:
                pretty_samples = [_pretty_text_answer(sample) for sample in raw_samples if str(sample or "").strip()]
                samples = " | ".join(pretty_samples)
            preview = samples if samples else "未设置示例内容"
            if len(preview) > 60:
                preview = preview[:57] + "..."
            label = "位置题" if self.is_location else ("多项填空题" if self.question_type == "multi_text" else "填空题")
            return f"{label}: {preview}"

        if self.question_type == "matrix":
            return f"{max(1, self.rows)} 行 × {max(1, self.option_count)} 列 - {_mode_text(self.distribution_mode)}"
        if self.question_type == "order":
            return f"{self.option_count} 个选项 - 自动随机排序"
        if self.question_type == "multiple" and self.probabilities == -1:
            return f"{self.option_count} 个选项 - 随机多选"
        if self.probabilities == -1:
            return f"{self.option_count} 个选项 - 完全随机"

        fillable_hint = ""
        if self.option_fill_texts and any(text for text in self.option_fill_texts if text):
            fillable_hint = " | 含填空项"
        elif self.attached_option_selects:
            fillable_hint = " | 含嵌入式下拉"

        if self.question_type == "multiple" and self.custom_weights:
            weights_str = ",".join(f"{int(round(max(w, 0)))}%" for w in self.custom_weights if isinstance(w, (int, float)))
            return f"{self.option_count} 个选项 - 概率 {weights_str}{fillable_hint}"

        if self.distribution_mode == "custom" and self.custom_weights:
            def _format_ratio(value: float) -> str:
                rounded = round(value, 1)
                if abs(rounded - int(rounded)) < 1e-6:
                    return str(int(rounded))
                return f"{rounded}".rstrip("0").rstrip(".")

            def _safe_weight(raw_value: Any) -> float:
                try:
                    return max(float(raw_value), 0.0)
                except Exception:
                    return 0.0

            weights_str = ":".join(_format_ratio(_safe_weight(w)) for w in self.custom_weights if isinstance(w, (int, float)))
            return f"{self.option_count} 个选项 - 配比 {weights_str}{fillable_hint}"

        return f"{self.option_count} 个选项 - {_mode_text(self.distribution_mode)}{fillable_hint}"


def get_entry_type_label(entry: QuestionEntry) -> str:
    if getattr(entry, "is_location", False):
        return LOCATION_QUESTION_LABEL
    return QUESTION_TYPE_LABELS.get(entry.question_type, entry.question_type)
