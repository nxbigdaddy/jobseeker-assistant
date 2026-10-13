# -*- coding: utf-8 -*-
"""日志页：滚动日志 + 级别过滤（真实可用的 logging Handler 实现）。

线程安全方案：logging 可能来自任意线程，_QueueHandler 只把格式化后的文本写入
queue.Queue（不触碰任何控件）；GUI 线程通过 QTimer 定时轮询队列，按当前级别过滤
后追加到 QPlainTextEdit。因此跨线程不会访问控件。
"""

import logging
import queue

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

#: 界面级别选项 -> logging 级别
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

#: 队列轮询间隔（毫秒）
_POLL_INTERVAL_MS = 200


class _QueueHandler(logging.Handler):
    """把 (级别号, 格式化文本) 写入线程安全队列的日志处理器。"""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.queue = queue.Queue()
        self.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:  # noqa: BLE001 - 格式化失败不能影响主程序
            message = f"[{record.levelname}] {record.name}: {record.getMessage()!r}"
        try:
            self.queue.put_nowait((record.levelno, message))
        except Exception:  # noqa: BLE001 - 队列写失败仅丢弃该条日志
            pass


class LogPage(QWidget):
    """日志页：显示 Python logging 记录，支持级别过滤。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._handler = None
        self._poll_timer = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("日志", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(QLabel("级别过滤:", self))
        self.level_combo = QComboBox(self)
        self.level_combo.addItems(list(_LEVELS.keys()))
        self.level_combo.setCurrentText("INFO")
        self.level_combo.currentTextChanged.connect(self._on_level_changed)
        bar.addWidget(self.level_combo)
        bar.addStretch(1)
        layout.addLayout(bar)

        self.log_edit = QPlainTextEdit(self)
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(5000)
        self.log_edit.setPlaceholderText("日志输出…")
        layout.addWidget(self.log_edit, 1)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def install(self) -> None:
        """向根 logger 安装处理器并启动队列轮询（幂等，可安全重复调用）。

        调用方：主窗口创建日志页后调用一次。处理器只持有队列引用，
        不访问控件，日志页销毁后也不会产生悬挂引用问题。
        """
        if self._handler is not None:
            return  # 本实例已安装过
        self._handler = _QueueHandler()
        logging.getLogger().addHandler(self._handler)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._drain_queue)
        self._poll_timer.start(_POLL_INTERVAL_MS)

        # 写入一条启动日志，用于验证链路
        logging.getLogger(__name__).info(
            "日志页已安装（级别过滤：%s）", self.level_combo.currentText()
        )

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _drain_queue(self) -> None:
        """把队列中的日志按当前级别过滤后追加到文本框（仅 GUI 线程调用）。"""
        if self._handler is None:
            return
        min_level = _LEVELS.get(self.level_combo.currentText(), logging.INFO)
        handler_queue = self._handler.queue
        while True:
            try:
                levelno, message = handler_queue.get_nowait()
            except queue.Empty:
                break
            if levelno >= min_level:
                self.log_edit.appendPlainText(message)

    def _on_level_changed(self, text: str) -> None:
        """级别过滤切换：仅影响后续追加的日志，不重放历史。"""
        logger.info("日志页：级别过滤切换为 %s", text)
