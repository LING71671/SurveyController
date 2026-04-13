"""引擎入口 - 仅负责组装执行循环。"""

from __future__ import annotations

import threading
from typing import Any

from software.core.engine.execution_loop import ExecutionLoop
from software.core.task import ExecutionConfig, ExecutionState


def run(
    window_x_pos: int,
    window_y_pos: int,
    stop_signal: threading.Event,
    gui_instance: Any = None,
    *,
    config: ExecutionConfig,
    state: ExecutionState,
) -> None:
    loop = ExecutionLoop(config, state, gui_instance)
    loop.run_thread(window_x_pos, window_y_pos, stop_signal)


__all__ = ["run"]
