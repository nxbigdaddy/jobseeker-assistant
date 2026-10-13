# -*- coding: utf-8 -*-
"""通用异步任务执行器：QThread + asyncio 事件循环（GUI 与服务层的接线基础）。

线程模型
========
所有服务方法均为 async（Playwright 生态），而 Qt GUI 只能由主线程操作：

- **Qt 主线程**：创建 worker、连接信号；信号槽内可以直接更新 UI（QWidget 操作只允许在这里）；
- **worker 线程**：``QThread::run()`` 内调用 ``asyncio.run(coro_factory())`` 跑协程，
  任何情况下都不直接触碰 QWidget；
- **跨线程通信**：worker 线程 emit 信号（succeeded / failed / progress），Qt 默认以
  QueuedConnection 把信号投递到接收者所在线程（主线程）的槽，因此信号槽天然线程安全，
  槽里可以放心更新 UI —— 这是本模块唯一的跨线程数据通道。

用法::

    AsyncWorker.run_async(
        lambda: AuthService.get().login("51job", mode="qr", qr_callback=cb),
        on_success=lambda ok: ...,
        on_failure=lambda msg: ...,
    )

worker 对象生命周期：全局 ``_alive`` 集合持有引用防止被 GC；
``finished`` 信号（run 结束、信号投递完成后触发）把 worker 从集合移除。
"""

from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import QThread, Signal

logger = logging.getLogger(__name__)


class AsyncWorker(QThread):
    """在独立线程的 asyncio 事件循环里执行一个协程，结果经 Qt 信号回主线程。"""

    #: 协程正常返回：result 为协程返回值（任意 Python 对象）
    succeeded = Signal(object)
    #: 协程抛出异常：str 形式的异常信息
    failed = Signal(str)
    #: 进度消息：str 文本（协程内部通过 report_progress 主动发出）
    progress = Signal(str)

    #: 全局存活集合：防止 worker 被 GC；run 结束后由 finished 信号移除（主线程操作）
    _alive: set["AsyncWorker"] = set()

    def __init__(self, coro_factory):
        super().__init__()
        #: 无参函数，调用后返回一个 coroutine
        self._coro_factory = coro_factory

    # ------------------------------------------------------------------ #
    # QThread 入口（worker 线程内执行）
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        """worker 线程入口：在新的事件循环里执行协程，结果/异常经信号回主线程。"""
        try:
            result = asyncio.run(self._coro_factory())
        except Exception as exc:  # noqa: BLE001 —— 异常信息交给 failed 信号
            logger.exception("异步任务执行失败")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.succeeded.emit(result)

    def report_progress(self, text: str) -> None:
        """协程内部调用：发送一条进度消息（跨线程经信号投递到主线程的 on_progress）。"""
        self.progress.emit(str(text))

    # ------------------------------------------------------------------ #
    # 便捷入口（主线程调用）
    # ------------------------------------------------------------------ #
    @classmethod
    def run_async(
        cls,
        coro_factory,
        on_success=None,
        on_failure=None,
        on_progress=None,
    ) -> "AsyncWorker":
        """创建并启动一个异步任务 worker，回调经信号连接（槽在主线程执行）。

        :param coro_factory: 无参函数，调用后返回 coroutine
        :param on_success: 成功回调（主线程执行，参数为协程返回值）
        :param on_failure: 失败回调（主线程执行，参数为异常信息字符串）
        :param on_progress: 进度回调（主线程执行，参数为进度文本）
        :return: 已 start 的 worker（可持有引用 / 查询 isRunning）
        """
        worker = cls(coro_factory)
        if on_success is not None:
            worker.succeeded.connect(on_success)
        if on_failure is not None:
            worker.failed.connect(on_failure)
        if on_progress is not None:
            worker.progress.connect(on_progress)
        # 结束后从全局集合移除，允许 GC 回收（finished 在主线程触发，集合访问安全）
        worker.finished.connect(lambda w=worker: cls._release(w))
        cls._alive.add(worker)
        worker.start()
        return worker

    @classmethod
    def _release(cls, worker: "AsyncWorker") -> None:
        """从全局存活集合移除 worker（run 结束后由 finished 信号触发）。"""
        cls._alive.discard(worker)
