# -*- coding: utf-8 -*-
"""投递中心页：待投递队列 + 投递操作区 + 投递历史。

接线说明（服务层：app.services.apply_service.ApplyService / app.state.AppState /
app.db.repository）：
- showEvent：_refresh_history 从 applications 表填历史；_refresh_queue 从
  AppState.get_selected() 填待投递队列（首列勾选）；
- 半自动投递（§9.1）：确认回调由 worker 线程触发（apply_job 内部确认点），
  经 ``confirm_requested`` 信号转发到主线程弹模态确认框，主线程把用户选择
  写回 threading.Event，worker 线程同步等待 —— 跨线程"同步确认"的标准做法；
- 逐条串行：一次只跑一条（_apply_next），成功/失败后取下一条；
- 「取消当前」：ApplyService.cancel_current() 为 async，放入 worker 执行
  （内部仅置取消标志，下一条投递会在确认点/检查点中断）；
- 批次开始前先 reset_cancel() 清除上次的取消标志（§9.2 安全方向）；
- 全自动模式勾选仅弹风险提示并记录状态，不改变 ApplyService 行为
  （半自动人工确认始终生效；全自动模式在后续版本实现，见 _on_auto_toggled 注释）。
"""

import logging
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.db import repository
from app.db.database import get_connection
from app.services.apply_service import ApplyService
from app.state import AppState
from app.ui.worker import AsyncWorker

logger = logging.getLogger(__name__)

#: 待投递队列列
QUEUE_COLUMNS = ["勾选", "岗位", "公司", "来源"]
#: 投递历史列
HISTORY_COLUMNS = ["时间", "岗位", "站点", "简历版本", "状态", "备注"]

#: 确认框等待超时（秒）：用户长时间不操作时 worker 按"拒绝投递"处理
_CONFIRM_TIMEOUT_SECONDS = 120


def _app_to_dict(conn, app) -> dict:
    """Application 模型 → 历史表字典（岗位标题经 job_ref_id 关联 jobs 表）。"""
    title = ""
    if app.job_ref_id is not None:
        try:
            job = repository.get_job(conn, app.job_ref_id)
            if job is not None:
                title = job.title
        except Exception:  # noqa: BLE001
            pass
    return {
        "time": app.applied_at,
        "title": title,
        "source": app.source,
        "resume_version": app.resume_version,
        "status": app.status,
        "note": app.note,
    }


class ApplyPage(QWidget):
    """投递中心页。"""

    #: 确认请求转发信号（worker 线程 → 主线程）：(request_id, job, resume_name, platform)
    confirm_requested = Signal(object, object, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._queue_items = []          # 队列行 -> 队列项（JobDict）
        self._apply_pending = None      # 待投递序列（串行队列）
        self._apply_svc = None          # 投递服务引用
        self._apply_stats = {"ok": 0, "fail": 0}
        self._confirm_requests = {}     # request_id -> {"event", "result"}
        self._confirm_seq = 0
        self._build_ui()
        self.set_queue([])
        self.set_history([])
        self.confirm_requested.connect(self._on_confirm_requested)

    def showEvent(self, event) -> None:
        """页面每次显示时刷新投递历史与待投递队列。"""
        super().showEvent(event)
        self._refresh_history()
        self._refresh_queue()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("投递中心", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._build_queue_group(), 3)
        body.addWidget(self._build_action_card(), 0)
        body.addWidget(self._build_history_group(), 3)
        layout.addLayout(body, 1)

    def _build_queue_group(self) -> QGroupBox:
        """左侧：待投递队列。"""
        group = QGroupBox("待投递队列", self)
        v = QVBoxLayout(group)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        self.queue_table = QTableWidget(0, len(QUEUE_COLUMNS), group)
        self.queue_table.setHorizontalHeaderLabels(QUEUE_COLUMNS)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.queue_table.setSelectionMode(QTableWidget.ExtendedSelection)
        self.queue_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.queue_table, 1)
        return group

    def _build_action_card(self) -> QFrame:
        """中部：投递操作按钮。"""
        card = QFrame(self)
        card.setObjectName("card")
        card.setFixedWidth(170)
        col = QVBoxLayout(card)
        col.setContentsMargins(14, 16, 14, 16)
        col.setSpacing(12)
        col.addStretch(1)

        self.start_btn = QPushButton("开始投递\n（半自动）", card)
        self.start_btn.clicked.connect(self._on_start_apply)
        col.addWidget(self.start_btn)

        self.auto_check = QCheckBox("全自动模式", card)
        self.auto_check.setChecked(False)
        self.auto_check.setToolTip(
            "默认关闭。开启后跳过人工确认，风险自负（内置风控仍生效，见设计文档 §9.2）"
        )
        self.auto_check.toggled.connect(self._on_auto_toggled)
        col.addWidget(self.auto_check)

        self.cancel_btn = QPushButton("取消当前", card)
        self.cancel_btn.setObjectName("secondary")
        self.cancel_btn.clicked.connect(self._on_cancel)
        col.addWidget(self.cancel_btn)

        col.addStretch(1)
        return card

    def _build_history_group(self) -> QGroupBox:
        """右侧：投递历史。"""
        group = QGroupBox("投递历史", self)
        v = QVBoxLayout(group)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        self.history_table = QTableWidget(0, len(HISTORY_COLUMNS), group)
        self.history_table.setHorizontalHeaderLabels(HISTORY_COLUMNS)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SingleSelection)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.horizontalHeader().setStretchLastSection(True)
        v.addWidget(self.history_table, 1)
        return group

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def set_queue(self, items: list) -> None:
        """填充待投递队列；首列为勾选框。

        :param items: [{title, company, source}, ...]
        """
        self._queue_items = list(items or [])
        self.queue_table.setRowCount(0)
        for item in self._queue_items:
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)

            check_item = QTableWidgetItem()
            check_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            check_item.setCheckState(Qt.Unchecked)
            self.queue_table.setItem(row, 0, check_item)

            self.queue_table.setItem(row, 1, QTableWidgetItem(item.get("title", "")))
            self.queue_table.setItem(row, 2, QTableWidgetItem(item.get("company", "")))
            self.queue_table.setItem(row, 3, QTableWidgetItem(item.get("source", "")))

    def set_history(self, items: list) -> None:
        """填充投递历史表。

        :param items: [{time, title, source, resume_version, status, note}, ...]
        """
        self.history_table.setRowCount(0)
        for item in items:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            values = (
                item.get("time", ""),
                item.get("title", ""),
                item.get("source", ""),
                item.get("resume_version", ""),
                item.get("status", ""),
                item.get("note", ""),
            )
            for col, value in enumerate(values):
                self.history_table.setItem(row, col, QTableWidgetItem(str(value)))

    def confirm_apply_dialog(self, job_title: str, resume_name: str, platform: str) -> bool:
        """模态确认框：显示岗位/简历/平台，询问用户是否确认投递。

        :return: True 表示用户点击了「确认投递」，False 表示取消。
        """
        box = QMessageBox(self)
        box.setWindowTitle("投递确认")
        box.setIcon(QMessageBox.Question)
        box.setText("即将向以下岗位提交投递申请：")
        box.setInformativeText(
            f"岗位：{job_title}\n简历版本：{resume_name}\n平台：{platform}\n\n"
            "请确认信息无误后再提交。"
        )
        confirm_btn = box.addButton("确认投递", QMessageBox.AcceptRole)
        cancel_btn = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        return box.clickedButton() is confirm_btn

    # ------------------------------------------------------------------ #
    # 数据刷新
    # ------------------------------------------------------------------ #
    def _refresh_queue(self) -> None:
        """从 AppState 待投递队列刷新队列表。"""
        self.set_queue(AppState.get().get_selected())

    def _refresh_history(self) -> None:
        """从 applications 表刷新投递历史。"""
        try:
            with get_connection() as conn:
                apps = repository.list_applications(conn)
                items = [_app_to_dict(conn, a) for a in apps]
            self.set_history(items)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取投递历史失败：%s", exc)
            self.set_history([])

    # ------------------------------------------------------------------ #
    # 跨线程确认回调（ApplyService 确认点在 worker 线程触发）
    # ------------------------------------------------------------------ #
    def _confirm_proxy(self, job, resume_name: str, platform: str) -> bool:
        """ApplyService 注入的确认回调（worker 线程调用）。

        把确认请求经信号转发到主线程弹模态框，本线程同步等待用户选择
        （阻塞 worker 线程不影响 GUI 响应；超时按"拒绝投递"处理）。
        """
        self._confirm_seq += 1
        request_id = self._confirm_seq
        holder = {"event": threading.Event(), "result": False}
        self._confirm_requests[request_id] = holder
        self.confirm_requested.emit(request_id, job, resume_name, platform)
        holder["event"].wait(timeout=_CONFIRM_TIMEOUT_SECONDS)
        self._confirm_requests.pop(request_id, None)
        return holder["result"]

    def _on_confirm_requested(self, request_id, job, resume_name: str, platform: str) -> None:
        """主线程槽：弹出模态确认框并把用户选择写回请求。"""
        title = ""
        try:
            title = getattr(job, "title", "") or (job.get("title") if isinstance(job, dict) else "")
        except Exception:  # noqa: BLE001
            title = ""
        confirmed = self.confirm_apply_dialog(str(title or ""), resume_name, platform)
        holder = self._confirm_requests.get(request_id)
        if holder is not None:
            holder["result"] = confirmed
            holder["event"].set()

    # ------------------------------------------------------------------ #
    # 内部回调
    # ------------------------------------------------------------------ #
    def _checked_rows(self) -> list:
        """返回队列中已勾选的行号列表。"""
        rows = []
        for row in range(self.queue_table.rowCount()):
            item = self.queue_table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                rows.append(row)
        return rows

    def _on_start_apply(self) -> None:
        """开始半自动投递：勾选岗位逐条串行投递（每步人工确认）。"""
        checked = self._checked_rows()
        if not checked:
            QMessageBox.information(self, "投递中心", "请先在待投递队列中勾选要投递的岗位。")
            return
        resume_name = AppState.get().get_resume()
        if not resume_name:
            QMessageBox.warning(
                self, "投递中心", "尚未选择投递用简历版本，请先到「简历库」页选择一份简历。"
            )
            return

        self._apply_svc = ApplyService.get()
        # 批次开始前清除取消标志（§9.2：宁可拒绝也不越限，取消标志保持置位）
        self._apply_svc.reset_cancel()
        # 注入确认回调：worker 线程触发 → 信号转发主线程弹确认框
        self._apply_svc.set_confirm_callback(self._confirm_proxy)

        jobs = [self._queue_items[row] for row in checked if row < len(self._queue_items)]
        if not jobs:
            return
        self._apply_pending = list(jobs)
        self._apply_stats = {"ok": 0, "fail": 0}
        self._set_status(f"开始投递：队列共 {len(jobs)} 条")
        self._apply_next()

    def _apply_next(self) -> None:
        """串行投递：一次只跑一条，成功/失败后取下一条。"""
        if not self._apply_pending:
            stats = self._apply_stats
            self._set_status(f"投递批次完成：成功 {stats['ok']} 条 / 未完成 {stats['fail']} 条")
            self._apply_pending = None
            QMessageBox.information(
                self, "投递中心",
                f"投递批次完成：\n成功 {stats['ok']} 条\n未完成 {stats['fail']} 条",
            )
            return
        item = self._apply_pending[0]
        site = str(item.get("source", ""))
        resume_name = AppState.get().get_resume() or ""
        self._set_status(f"正在投递：{item.get('title', '')}（{site}）")
        AsyncWorker.run_async(
            lambda: self._apply_svc.apply_job(site, item, resume_name),
            on_success=lambda ok: self._on_apply_one_done(bool(ok)),
            on_failure=self._on_apply_one_failed,
        )

    def _on_apply_one_done(self, ok: bool) -> None:
        """单条投递结束（主线程）：刷新历史并取下一条。"""
        item = self._apply_pending[0] if self._apply_pending else None
        title = item.get("title", "") if item else ""
        if ok:
            self._apply_stats["ok"] += 1
            self._set_status(f"投递成功：{title}")
        else:
            # apply_job 返回 False = 风控/未登录/用户取消/提交失败等（安全方向）
            self._apply_stats["fail"] += 1
            self._set_status(f"投递未完成：{title}")
        if self._apply_pending:
            self._apply_pending.pop(0)
        self._refresh_history()
        self._apply_next()

    def _on_apply_one_failed(self, msg: str) -> None:
        """单条投递抛异常（主线程）：记录并继续下一条。"""
        self._apply_stats["fail"] += 1
        logger.warning("单条投递异常：%s", msg)
        if self._apply_pending:
            self._apply_pending.pop(0)
        self._refresh_history()
        self._apply_next()

    def _on_cancel(self) -> None:
        """取消当前投递：置取消标志，当前进行中的投递在下一个检查点中断。"""
        try:
            service = ApplyService.get()
        except Exception as exc:  # noqa: BLE001
            logger.warning("取消投递失败：%s", exc)
            return
        # cancel_current 为 async（内部仅置标志），放 worker 执行避免"协程未被 await"
        AsyncWorker.run_async(
            lambda: service.cancel_current(),
            on_success=lambda _r: self._set_status("已请求取消当前投递"),
            on_failure=lambda msg: self._set_status(f"取消失败：{msg}"),
        )

    def _on_auto_toggled(self, checked: bool) -> None:
        """全自动模式：默认关闭；勾选时弹风险提示（设计文档 §9.2）。

        当前版本仅记录勾选状态，不改变 ApplyService 行为 —— 半自动人工确认
        始终生效。全自动模式（跳过确认、风险自担）在后续版本实现。
        """
        if checked:
            QMessageBox.warning(
                self,
                "全自动模式",
                "全自动模式将跳过人工确认，存在账号风控风险，请谨慎使用"
                "（设计文档 §9.2：默认人工确认模式）。\n\n"
                "当前版本仍按半自动模式执行（人工确认始终生效），"
                "全自动模式将在后续版本实现。",
            )
        # 仅记录状态（日志），不改变投递行为
        logger.info("全自动模式开关：%s（仅记录，当前版本不生效）", "开" if checked else "关")

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _set_status(self, text: str) -> None:
        """更新主窗口状态栏；不在主窗口内时仅记日志。"""
        set_status = getattr(self.window(), "set_status", None)
        if callable(set_status):
            set_status(text)
        else:
            logger.info("投递中心状态：%s", text)
