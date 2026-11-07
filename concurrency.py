# -*- coding: utf-8 -*-
"""并发下载与自适应限速（v0.32）。

设计目标：
- 多线程并发抓取章节，显著提高下载速度；
- 遇到服务器限流（429/503）或 WAF 验证码时，自动拉长请求间隔，
  “礼貌访问”，避免被封；
- 服务器恢复正常后逐步恢复速度（AIMD：乘性下降 / 加性恢复）。

本模块不依赖具体爬虫实现，只提供限速器与并发执行器，便于独立测试。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, TypeVar

T = TypeVar("T")


class RateLimiter:
    """自适应限速器：控制全局请求节奏，并在限流时降速。

    - ``acquire()``：请求前调用；若距上次请求不足当前间隔，则睡眠补齐。
    - ``report_success()``：请求成功；连续成功后逐步缩短间隔（恢复速度）。
    - ``report_throttled()``：被限流；立即拉长间隔。
    """

    def __init__(
        self,
        *,
        delay: float = 0.8,
        min_delay: float = 0.1,
        max_delay: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._delay = max(delay, min_delay)
        self._min_delay = min_delay
        self._max_delay = max(max_delay, min_delay)
        self._last_by_thread: dict[int, float] = {}
        self._consecutive_ok = 0
        self.throttle_events = 0

    def acquire(self) -> None:
        """获取一次发送许可，必要时按当前间隔等待。

        节流按线程独立计算：同一线程的连续请求之间至少间隔 ``delay`` 秒，
        但不同线程互不阻塞——这样并发才能真正提升吞吐。全局限流时，
        ``report_throttled()`` 把所有线程的间隔一起拉长。
        """
        thread_id = threading.get_ident()
        with self._lock:
            last = self._last_by_thread.get(thread_id, 0.0)
            delay = self._delay
            now = self._clock()
            wait_for = delay - (now - last)
        if wait_for > 0:
            self._sleep(wait_for)
        with self._lock:
            self._last_by_thread[thread_id] = self._clock()

    def report_success(self) -> None:
        with self._lock:
            self._consecutive_ok += 1
            if self._consecutive_ok >= 20 and self._delay > self._min_delay:
                self._delay = max(self._min_delay, self._delay * 0.9)
                self._consecutive_ok = 0

    def report_throttled(self) -> None:
        """被限流（429/503）：在现有基础上翻倍，但有上限。"""
        with self._lock:
            self._consecutive_ok = 0
            self._delay = min(self._max_delay, max(self._delay * 2, 1.0))
            self.throttle_events += 1
            logging.info("检测到限流，自动降速：请求间隔调整为 %.1f 秒", self._delay)

    def report_captcha(self) -> None:
        """遇到人机验证：只是偶发拦截，不应把后续请求永久拖慢。

        验证通过后恢复原间隔；只记录事件，用于最终汇总。
        """
        with self._lock:
            self._consecutive_ok = 0
            self.throttle_events += 1

    @property
    def delay(self) -> float:
        with self._lock:
            return self._delay


@dataclass
class RunStats:
    """并发执行统计。"""

    completed: int = 0
    failed: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, ok: bool) -> None:
        with self.lock:
            self.completed += 1
            if not ok:
                self.failed += 1


@dataclass
class RunResult:
    """并发执行结果：成功的 {index: 值} 与失败的 {index: 异常}。"""

    values: dict[int, Any] = field(default_factory=dict)
    errors: dict[int, Exception] = field(default_factory=dict)


def run_ordered(
    items: Iterable[tuple[int, T]],
    worker: Callable[[int, T], Any],
    *,
    max_workers: int = 6,
    on_result: Callable[[int, T, Any, Exception | None], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> RunResult:
    """并发执行 ``worker(index, item)``。

    - 结果与完成顺序无关，调用方按 index 自行排序；
    - 单条任务抛异常只记录在 ``result.errors``，不影响其它任务；
    - ``cancel_event`` 置位后不再提交新任务（用于遇到全局性错误时退出）；
    - worker 返回 None 视为“无结果但不抛错”，由调用方判断。
    """
    pending = list(items)
    result = RunResult()
    if not pending:
        return result

    workers = max(1, min(max_workers, len(pending)))
    cancel_event = cancel_event or threading.Event()

    def handle(index: int, item: T, value: Any, error: Exception | None) -> None:
        if error is not None:
            result.errors[index] = error
        else:
            result.values[index] = value
        if on_result is not None:
            on_result(index, item, value, error)

    if workers == 1:
        for index, item in pending:
            if cancel_event.is_set():
                break
            try:
                value = worker(index, item)
            except Exception as exc:  # noqa: BLE001
                handle(index, item, None, exc)
            else:
                handle(index, item, value, None)
        return result

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="novel") as pool:
        queue = iter(pending)
        in_flight: dict[Future, tuple[int, T]] = {}

        def submit_one() -> None:
            if cancel_event.is_set():
                return
            for index, item in queue:
                in_flight[pool.submit(worker, index, item)] = (index, item)
                return

        for _ in range(workers):
            submit_one()

        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for future in done:
                index, item = in_flight.pop(future)
                try:
                    value = future.result()
                except Exception as exc:  # noqa: BLE001
                    handle(index, item, None, exc)
                else:
                    handle(index, item, value, None)
                submit_one()

    return result


def jitter(base: float, spread: float, rng: random.Random | None = None) -> float:
    """在 base 上叠加 [0, spread) 的随机抖动，避免机械化访问节奏。"""
    if base <= 0:
        return 0.0
    rng = rng or random
    return base + rng.uniform(0, spread)
