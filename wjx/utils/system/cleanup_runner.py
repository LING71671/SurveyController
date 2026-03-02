"""异步清理任务执行器 - 后台回收浏览器实例等资源"""
from __future__ import annotations
import logging
from wjx.utils.logging.log_utils import log_suppressed_exception


import subprocess
import threading
import time
from collections import deque
from typing import Callable, Deque, Optional, Tuple

logger = logging.getLogger(__name__)

# 需要清理的浏览器进程名
_BROWSER_PROCESS_NAMES = ("chrome.exe", "msedge.exe", "chromium.exe")

# Windows 隐藏控制台窗口标志
_NO_WINDOW = 0x08000000


class CleanupRunner:
    """Run cleanup tasks in a single background worker to avoid blocking the UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: Deque[Tuple[Callable[[], None], float]] = deque()
        self._thread: Optional[threading.Thread] = None

    def submit(self, task: Callable[[], None], delay_seconds: float = 0.0) -> None:
        """提交普通清理任务（非 PID 清理）"""
        delay = max(0.0, float(delay_seconds or 0.0))
        with self._lock:
            self._queue.append((task, delay))
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._worker, daemon=True, name="CleanupWorker")
            self._thread.start()

    def _worker(self) -> None:
        """后台工作线程：处理普通清理任务队列"""
        while True:
            with self._lock:
                if not self._queue:
                    self._thread = None
                    return
                task, delay = self._queue.popleft()
            if delay > 0:
                time.sleep(delay)
            try:
                task()
            except Exception as exc:
                log_suppressed_exception("_worker: task()", exc, level=logging.WARNING)


# TODO(清理): 疑似未使用，先保留，确认外部是否有引用再决定删除。
def kill_browser_processes() -> None:
    """使用 taskkill 强制关闭所有浏览器进程（异步执行，不阻塞调用线程）。"""

    def _do_kill():
        logger.info("开始清理浏览器进程: %s", ", ".join(_BROWSER_PROCESS_NAMES))
        for name in _BROWSER_PROCESS_NAMES:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    creationflags=_NO_WINDOW,
                )
            except Exception as exc:
                log_suppressed_exception("_do_kill: subprocess.run( [\"taskkill\", \"/F\", \"/IM\", name], stdout=subprocess.DEVNULL, s...", exc, level=logging.WARNING)
        logger.info("浏览器进程清理完成")

    threading.Thread(target=_do_kill, daemon=True, name="BrowserKiller").start()
