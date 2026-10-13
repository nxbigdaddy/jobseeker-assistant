# -*- coding: utf-8 -*-
"""搜索采集页：左侧任务配置 + 右侧运行记录。

接线说明（服务层：app.services.crawl_service.CrawlService /
app.scheduler.scheduler.SchedulerService / app.db.repository）：
- showEvent 时从 DB 读 search_tasks / crawl_runs 填表（_refresh_tasks / _refresh_runs）；
- 新增/编辑/删除任务：QDialog 表单（名称 / 站点多选 / 关键词 / 城市 / 薪资区间 / 启用），
  通过 repository.insert_search_task / update_search_task / delete_search_task 持久化
  （sites / keywords / cities 由 models.SearchTask.as_dict 存 JSON 字符串）；
- 立即采集：AsyncWorker 后台执行 run_all_enabled，完成后刷新运行记录并弹摘要；
- 每日增量开关 + 时间：开启→启动 SchedulerService，关闭→停止；时间变化→写入
  settings 表（简化：不重启调度器，下次启动生效，见 _on_daily_time_changed 注释）。
"""

import logging
import re

from PySide6.QtCore import QTime, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from app.config import settings as app_settings
from app.db import repository
from app.db.database import get_connection
from app.db.models import SearchTask
from app.services.crawl_service import CrawlService
from app.scheduler.scheduler import SchedulerService
from app.ui.worker import AsyncWorker

logger = logging.getLogger(__name__)

#: 搜索任务表列：名称/站点/关键词/城市/启用
TASK_COLUMNS = ["名称", "站点", "关键词", "城市", "启用"]
#: 运行记录表列：时间/抓到/新增/状态
RUN_COLUMNS = ["时间", "抓到", "新增", "状态"]

#: 任务表单站点多选（key → 中文名，与登录中心一致）
SITE_CHOICES = [
    ("51job", "51job（前程无忧）"),
    ("zhaopin", "智联招聘"),
    ("lagou", "拉勾网"),
    ("liepin", "猎聘"),
    ("zhipin", "Boss 直聘"),
]


def _split_csv(text: str) -> list:
    """按中英文逗号/分号拆分输入为关键词/城市列表。"""
    return [p.strip() for p in re.split(r"[,，;；]", text or "") if p.strip()]


def _task_to_dict(task) -> dict:
    """SearchTask 模型 → load_tasks 展示字典。"""
    return {
        "name": task.name,
        "site": "、".join(task.sites or []),
        "keyword": "、".join(task.keywords or []),
        "city": "、".join(task.cities or []),
        "enabled": bool(task.enabled),
    }


def _run_to_dict(run) -> dict:
    """CrawlRun 模型 → load_runs 展示字典。"""
    return {
        "time": run.started_at,
        "fetched": run.fetched,
        "added": run.inserted,
        "status": run.status,
    }


class SearchPage(QWidget):
    """搜索采集页。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks = []  # 任务表行对应的 SearchTask 模型（编辑/删除用）
        self._build_ui()
        # 初始为空表
        self.load_tasks([])
        self.load_runs([])
        # 每日增量开关/时间变化接线
        self.daily_check.toggled.connect(self._on_daily_toggled)
        self.daily_time.timeChanged.connect(self._on_daily_time_changed)

    def showEvent(self, event) -> None:
        """页面每次显示时刷新任务表、运行记录与每日采集设置。"""
        super().showEvent(event)
        self._refresh_tasks()
        self._refresh_runs()
        self._load_daily_setting()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("搜索采集", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._build_task_group(), 3)
        body.addWidget(self._build_run_group(), 2)
        layout.addLayout(body, 1)

    def _build_task_group(self) -> QGroupBox:
        """左侧：任务配置区（搜索任务表 + 操作按钮）。"""
        group = QGroupBox("任务配置", self)
        v = QVBoxLayout(group)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        self.task_table = QTableWidget(0, len(TASK_COLUMNS), group)
        self.task_table.setHorizontalHeaderLabels(TASK_COLUMNS)
        self._setup_table(self.task_table)
        v.addWidget(self.task_table, 1)

        # 按钮行
        controls = QHBoxLayout()
        controls.setSpacing(8)

        add_btn = QPushButton("新增任务", group)
        add_btn.clicked.connect(self._on_add_task)
        controls.addWidget(add_btn)

        edit_btn = QPushButton("编辑任务", group)
        edit_btn.clicked.connect(self._on_edit_task)
        controls.addWidget(edit_btn)

        delete_btn = QPushButton("删除任务", group)
        delete_btn.setObjectName("danger")
        delete_btn.clicked.connect(self._on_delete_task)
        controls.addWidget(delete_btn)

        run_btn = QPushButton("立即采集", group)
        run_btn.clicked.connect(self._on_run_now)
        controls.addWidget(run_btn)

        controls.addStretch(1)

        # 每日增量开关 + 时间选择
        self.daily_check = QCheckBox("每日增量采集", group)
        self.daily_check.setToolTip("开启后每天定时执行增量采集（默认 08:00，见设计文档 §14）")
        controls.addWidget(self.daily_check)

        self.daily_time = QTimeEdit(QTime(8, 0), group)
        self.daily_time.setDisplayFormat("HH:mm")
        self.daily_time.setFixedWidth(90)
        self.daily_time.setToolTip("每日采集时间；修改后写入设置，重启应用后生效")
        controls.addWidget(self.daily_time)

        v.addLayout(controls)
        return group

    def _build_run_group(self) -> QGroupBox:
        """右侧：运行记录区。"""
        group = QGroupBox("运行记录", self)
        v = QVBoxLayout(group)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        self.run_table = QTableWidget(0, len(RUN_COLUMNS), group)
        self.run_table.setHorizontalHeaderLabels(RUN_COLUMNS)
        self._setup_table(self.run_table)
        v.addWidget(self.run_table, 1)
        return group

    @staticmethod
    def _setup_table(table: QTableWidget) -> None:
        """统一的表格基础设置：整行选中、只读、交替行色。"""
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)

    # ------------------------------------------------------------------ #
    # 对外接口（与既有调用方保持兼容）
    # ------------------------------------------------------------------ #
    def load_tasks(self, tasks: list) -> None:
        """填充搜索任务表。

        :param tasks: [{name, site, keyword, city, enabled}, ...]
        """
        self.task_table.setRowCount(0)
        for task in tasks:
            row = self.task_table.rowCount()
            self.task_table.insertRow(row)
            values = (
                task.get("name", ""),
                task.get("site", ""),
                task.get("keyword", ""),
                task.get("city", ""),
                "是" if task.get("enabled") else "否",
            )
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col == 4:
                    item.setTextAlignment(Qt.AlignCenter)
                self.task_table.setItem(row, col, item)

    def load_runs(self, runs: list) -> None:
        """填充运行记录表。

        :param runs: [{time, fetched, added, status}, ...]
        """
        self.run_table.setRowCount(0)
        for run in runs:
            row = self.run_table.rowCount()
            self.run_table.insertRow(row)
            values = (
                run.get("time", ""),
                run.get("fetched", ""),
                run.get("added", ""),
                run.get("status", ""),
            )
            for col, value in enumerate(values):
                self.run_table.setItem(row, col, QTableWidgetItem(str(value)))

    # ------------------------------------------------------------------ #
    # 数据刷新（DB）
    # ------------------------------------------------------------------ #
    def _refresh_tasks(self) -> None:
        """从 DB 读 search_tasks 填任务表（enabled_only=False）。"""
        try:
            with get_connection() as conn:
                tasks = repository.list_search_tasks(conn, enabled_only=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取搜索任务失败：%s", exc)
            tasks = []
        self._tasks = list(tasks)
        self.load_tasks([_task_to_dict(t) for t in tasks])

    def _refresh_runs(self) -> None:
        """从 DB 读 crawl_runs 填运行记录表。"""
        try:
            with get_connection() as conn:
                runs = repository.list_crawl_runs(conn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取运行记录失败：%s", exc)
            runs = []
        self.load_runs([_run_to_dict(r) for r in runs])

    # ------------------------------------------------------------------ #
    # 每日增量采集：开关 + 时间
    # ------------------------------------------------------------------ #
    def _load_daily_setting(self) -> None:
        """从 settings 表读 daily_crawl_time 同步到 QTimeEdit；勾选框反映调度器状态。"""
        value = app_settings.DAILY_CRAWL_TIME
        try:
            with get_connection() as conn:
                value = repository.get_setting(
                    conn, "daily_crawl_time", app_settings.DAILY_CRAWL_TIME
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取每日采集时间失败：%s", exc)
        try:
            t = QTime.fromString(str(value), "HH:mm")
            if t.isValid():
                # 初始同步不触发 timeChanged 槽（避免每次切页都重复保存+提示）
                self.daily_time.blockSignals(True)
                self.daily_time.setTime(t)
                self.daily_time.blockSignals(False)
        except Exception:  # noqa: BLE001
            pass
        # 勾选框初始状态 = 调度器是否运行（主窗口启动时默认启动）
        running = SchedulerService.get().is_running()
        self.daily_check.blockSignals(True)
        self.daily_check.setChecked(running)
        self.daily_check.blockSignals(False)

    def _on_daily_toggled(self, checked: bool) -> None:
        """每日增量开关：勾上→启动调度器，取消→停止。"""
        scheduler = SchedulerService.get()
        if checked:
            # 调度状态回调接到主窗口状态栏
            scheduler.set_status_callback(self._set_status)
            ok = scheduler.start()
            self._set_status(
                f"每日增量采集已开启：{self.daily_time.time().toString('HH:mm')}"
                if ok else "调度器启动失败，详见日志"
            )
        else:
            scheduler.stop()
            self._set_status("每日增量采集已关闭")

    def _on_daily_time_changed(self, qtime: QTime) -> None:
        """时间变化：写入 settings 表；简化实现不重启调度器（注释说明）。"""
        value = qtime.toString("HH:mm")
        try:
            with get_connection() as conn:
                repository.set_setting(conn, "daily_crawl_time", value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("保存每日采集时间失败：%s", exc)
        # 简化：scheduler 模块在导入时读取 DAILY_CRAWL_TIME 常量，此处不重启；
        # 新时间在下一次启动应用时生效。如需立即生效可在此调用 scheduler.stop()+start()。
        logger.info("每日采集时间已保存 %s（重启后生效）", value)
        self._set_status(f"每日采集时间已保存 {value}（重启后生效）")

    # ------------------------------------------------------------------ #
    # 任务表单（新增 / 编辑）
    # ------------------------------------------------------------------ #
    def _open_task_dialog(self, task=None) -> None:
        """新增（task=None）或编辑（task 为 SearchTask）任务的模态表单。"""
        dialog = QDialog(self)
        dialog.setWindowTitle("编辑搜索任务" if task is not None else "新增搜索任务")
        dialog.setMinimumWidth(480)
        form = QFormLayout(dialog)
        form.setSpacing(10)

        name_edit = QLineEdit(dialog)
        name_edit.setPlaceholderText("任务名称，如：Java 后端")
        form.addRow("名称", name_edit)

        sites_list = QListWidget(dialog)
        sites_list.setSelectionMode(QListWidget.MultiSelection)
        for key, name in SITE_CHOICES:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, key)
            sites_list.addItem(item)
        sites_list.setFixedHeight(110)
        form.addRow("站点", sites_list)

        keyword_edit = QLineEdit(dialog)
        keyword_edit.setPlaceholderText("多个关键词用逗号分隔，如：Java开发,后端开发")
        form.addRow("关键词", keyword_edit)

        city_edit = QLineEdit(dialog)
        city_edit.setPlaceholderText("多个城市用逗号分隔，如：北京,上海")
        form.addRow("城市", city_edit)

        salary_min = QSpinBox(dialog)
        salary_min.setRange(0, 100)
        salary_min.setSuffix("k")
        salary_max = QSpinBox(dialog)
        salary_max.setRange(0, 100)
        salary_max.setSuffix("k")
        salary_max.setValue(50)
        salary_row = QHBoxLayout()
        salary_row.addWidget(salary_min)
        salary_row.addWidget(QLabel("~", dialog))
        salary_row.addWidget(salary_max)
        salary_row.addStretch(1)
        form.addRow("薪资区间", salary_row)

        enabled_check = QCheckBox("启用该任务", dialog)
        enabled_check.setChecked(True)
        form.addRow("启用", enabled_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog
        )
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        # 编辑时预填
        if task is not None:
            name_edit.setText(task.name)
            task_sites = set(task.sites or [])
            for i in range(sites_list.count()):
                item = sites_list.item(i)
                if item.data(Qt.UserRole) in task_sites:
                    item.setSelected(True)
            keyword_edit.setText(",".join(task.keywords or []))
            city_edit.setText(",".join(task.cities or []))
            if task.salary_min:
                salary_min.setValue(task.salary_min)
            if task.salary_max:
                salary_max.setValue(task.salary_max)
            enabled_check.setChecked(bool(task.enabled))

        if dialog.exec() != QDialog.Accepted:
            return

        # ---- 收集表单数据并校验 ----
        name = name_edit.text().strip()
        sites = [
            sites_list.item(i).data(Qt.UserRole)
            for i in range(sites_list.count())
            if sites_list.item(i).isSelected()
        ]
        keywords = _split_csv(keyword_edit.text())
        cities = _split_csv(city_edit.text())
        if not name or not sites:
            QMessageBox.warning(self, "搜索采集", "任务名称与站点不能为空")
            return

        model = SearchTask(
            id=task.id if task is not None else None,
            name=name,
            sites=sites,
            keywords=keywords,
            cities=cities,
            salary_min=salary_min.value() or None,
            salary_max=salary_max.value() or None,
            enabled=1 if enabled_check.isChecked() else 0,
            last_run_at=task.last_run_at if task is not None else None,
        )
        with get_connection() as conn:
            if task is None:
                repository.insert_search_task(conn, model)
            else:
                repository.update_search_task(conn, model)
        self._refresh_tasks()
        self._set_status(f"任务已保存：{name}")

    def _on_add_task(self) -> None:
        self._open_task_dialog()

    def _on_edit_task(self) -> None:
        row = self.task_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "搜索采集", "请先选中要编辑的任务")
            return
        task = self._tasks[row] if row < len(self._tasks) else None
        if task is None:
            QMessageBox.warning(self, "搜索采集", "无法获取选中的任务数据")
            return
        self._open_task_dialog(task)

    def _on_delete_task(self) -> None:
        row = self.task_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "搜索采集", "请先选中要删除的任务")
            return
        task = self._tasks[row] if row < len(self._tasks) else None
        name = task.name if task is not None else f"任务#{row + 1}"
        ret = QMessageBox.question(
            self, "搜索采集", f"确定删除任务「{name}」吗？"
        )
        if ret != QMessageBox.Yes:
            return
        if task is not None:
            with get_connection() as conn:
                repository.delete_search_task(conn, task.id)
        self._refresh_tasks()
        self._set_status(f"任务已删除：{name}")

    # ------------------------------------------------------------------ #
    # 立即采集
    # ------------------------------------------------------------------ #
    def _on_run_now(self) -> None:
        """后台运行所有启用任务（全量采集），完成后刷新记录并弹摘要。

        注意：run_all_enabled 内部对未登录站点会跳过 —— 提示用户先去登录中心登录。
        """
        self._set_status("正在执行采集任务…")
        AsyncWorker.run_async(
            # on_progress 使用空实现：详细进度见日志页（CrawlService 内部 logger）
            lambda: CrawlService.get().run_all_enabled(
                fetch_detail=True, on_progress=lambda t: None
            ),
            on_success=self._on_crawl_done,
            on_failure=self._on_crawl_failed,
        )

    def _on_crawl_done(self, results) -> None:
        """采集完成（主线程）：刷新运行记录并弹摘要。"""
        self._refresh_runs()
        results = results or []
        total_fetched = sum(r.get("fetched", 0) for r in results)
        total_inserted = sum(r.get("inserted", 0) for r in results)
        self._set_status(f"采集完成：抓取 {total_fetched} 条 / 入库 {total_inserted} 条")
        QMessageBox.information(
            self,
            "搜索采集",
            f"本次采集完成：\n抓取 {total_fetched} 条\n入库 {total_inserted} 条\n\n"
            "提示：未登录的站点会被跳过，请先在「登录中心」登录后再采集。",
        )

    def _on_crawl_failed(self, msg: str) -> None:
        self._set_status("采集失败")
        QMessageBox.warning(self, "搜索采集", f"采集失败：{msg}")

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _set_status(self, text: str) -> None:
        """更新主窗口状态栏；不在主窗口内时仅记日志。"""
        set_status = getattr(self.window(), "set_status", None)
        if callable(set_status):
            set_status(text)
        else:
            logger.info("搜索采集状态：%s", text)
