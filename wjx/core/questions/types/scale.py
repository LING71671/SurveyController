"""量表题处理"""
from typing import Any, List, Optional

from wjx.network.browser import By, BrowserDriver
from wjx.core.persona.context import record_answer
from wjx.core.questions.tendency import get_tendency_index
from wjx.core.questions.consistency import apply_single_like_consistency


def scale(
    driver: BrowserDriver,
    current: int,
    index: int,
    scale_prob_config: List,
    dimension: Optional[str] = None,
    is_reverse: bool = False,
    psycho_plan: Optional[Any] = None,
    question_index: Optional[int] = None,
) -> None:
    """量表题处理主函数"""
    scale_items_xpath = f'//*[@id="div{current}"]/div[2]/div/ul/li'
    scale_options = driver.find_elements(By.XPATH, scale_items_xpath)
    probabilities = scale_prob_config[index] if index < len(scale_prob_config) else -1
    if not scale_options:
        return
    # 将概率转为列表，以便应用作答规则约束
    if isinstance(probabilities, list):
        probs = [float(p) for p in probabilities]
    else:
        probs = [1.0] * len(scale_options)
    probs = apply_single_like_consistency(probs, current)
    selected_index = get_tendency_index(
        len(scale_options),
        probs,
        dimension=dimension,
        is_reverse=is_reverse,
        psycho_plan=psycho_plan,
        question_index=(question_index if question_index is not None else current),
    )
    scale_options[selected_index].click()
    # 记录作答上下文
    record_answer(current, "scale", selected_indices=[selected_index])
