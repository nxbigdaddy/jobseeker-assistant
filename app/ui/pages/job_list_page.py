# -*- coding: utf-8 -*-
"""岗位列表页：筛选栏 + 岗位表格（QTableView/QStandardItemModel）+ 详情区。

接线说明（服务层：app.db.repository / app.services.ai_service.AiService /
app.services.resume_service.ResumeService / app.state.AppState）：
- 筛选/刷新：repository.list_jobs(conn, source=来源, city=城市, keyword=关键词)
  取数据填表（self._jobs 与行号一一对应，供详情/队列/AI 使用）；
- 选中行 → show_detail(description)；
- 「添加到投递队列」：把选中岗位 dict 存到 AppState.add_selected（去重），
  作为投递中心的待投递队列来源；
- 「AI 匹配评分」：AiService.match_score(jd, resume_text) 在 AsyncWorker 后台执行，
  结果弹窗展示并把分数写回 jobs.match_score 列（本页直接 SQL 更新，见注释）。
"""

import logging

from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.db import repository
from app.db.database import get_connection
from app.services.ai_service import AiService
from app.services.resume_service import ResumeService
from app.state import AppState
from app.ui.worker import AsyncWorker

logger = logging.getLogger(__name__)

#: 岗位表列（顺序即列顺序）
JOB_COLUMNS = ["来源", "岗位", "公司", "城市", "薪资", "学历", "经验", "发布时间", "匹配分", "收藏"]

CITIES = ["全部城市", "北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "南京", "苏州"]
SALARIES = ["全部薪资", "5k 以下", "5-10k", "10-15k", "15-20k", "20-30k", "30k 以上"]
SOURCES = ["全部来源", "51job", "智联招聘", "拉勾网", "猎聘", "Boss 直聘"]

#: 来源下拉文本 → 数据库站点标识
_SOURCE_KEYS = {
    "51job": "51job",
    "智联招聘": "zhaopin",
    "拉勾网": "lagou",
    "猎聘": "liepin",
    "Boss 直聘": "zhipin",
}


def _salary_match(job: dict, label: str) -> bool:
    """客户端薪资过滤：岗位区间 [lo, hi]（千元）与所选档位有交集才保留。

    无薪资信息的岗位保守保留（无法判断）。
    """
    if label == "全部薪资":
        return True
    lo = job.get("salary_min")
    hi = job.get("salary_max")
    if lo is None and hi is None:
        return True
    if label == "5k 以下":
        return (lo is not None and lo < 5) or (hi is not None and hi < 5)
    if label == "30k 以上":
        return (hi is not None and hi >= 30) or (lo is not None and lo >= 30)
    parts = label.rstrip("k").split("-")
    if len(parts) != 2:
        return True
    try:
        a, b = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        return True
    # 区间重叠判断：[lo, hi] 与 [a, b] 不相交才排除
    if hi is not None and hi < a:
        return False
    if lo is not None and lo > b:
        return False
    return True


class JobListPage(QWidget):
    """岗位列表页。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs = []  # 行号 -> job dict（与模型行一一对应）
        self._model = QStandardItemModel(0, len(JOB_COLUMNS), self)
        self._model.setHorizontalHeaderLabels(JOB_COLUMNS)
        self._build_ui()
        self.set_jobs([])

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("岗位列表", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        # ---- 筛选栏（卡片）----
        filter_card = QFrame(self)
        filter_card.setObjectName("card")
        bar = QHBoxLayout(filter_card)
        bar.setContentsMargins(12, 10, 12, 10)
        bar.setSpacing(8)

        bar.addWidget(QLabel("关键词:", filter_card))
        self.keyword_edit = QLineEdit(filter_card)
        self.keyword_edit.setPlaceholderText("如：Python 开发")
        self.keyword_edit.setFixedWidth(180)
        bar.addWidget(self.keyword_edit)

        bar.addWidget(QLabel("城市:", filter_card))
        self.city_combo = QComboBox(filter_card)
        self.city_combo.addItems(CITIES)
        bar.addWidget(self.city_combo)

        bar.addWidget(QLabel("薪资:", filter_card))
        self.salary_combo = QComboBox(filter_card)
        self.salary_combo.addItems(SALARIES)
        bar.addWidget(self.salary_combo)

        bar.addWidget(QLabel("来源:", filter_card))
        self.source_combo = QComboBox(filter_card)
        self.source_combo.addItems(SOURCES)
        bar.addWidget(self.source_combo)

        filter_btn = QPushButton("筛选", filter_card)
        filter_btn.clicked.connect(self._on_filter)
        bar.addWidget(filter_btn)

        refresh_btn = QPushButton("刷新", filter_card)
        refresh_btn.setObjectName("secondary")
        refresh_btn.clicked.connect(self._on_refresh)
        bar.addWidget(refresh_btn)

        queue_btn = QPushButton("添加到投递队列", filter_card)
        queue_btn.clicked.connect(self._on_add_to_queue)
        bar.addWidget(queue_btn)

        match_btn = QPushButton("AI 匹配评分", filter_card)
        match_btn.clicked.connect(self._on_ai_match)
        bar.addWidget(match_btn)
        bar.addStretch(1)

        layout.addWidget(filter_card)

        # ---- 岗位表格 ----
        self.view = QTableView(self)
        self.view.setModel(self._model)
        self.view.setSelectionBehavior(QTableView.SelectRows)
        self.view.setSelectionMode(QTableView.SingleSelection)
        self.view.setEditTriggers(QTableView.NoEditTriggers)
        self.view.setAlternatingRowColors(True)
        self.view.setSortingEnabled(True)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.view, 3)

        # ---- 详情区 ----
        self.detail_edit = QTextEdit(self)
        self.detail_edit.setReadOnly(True)
        self.detail_edit.setPlaceholderText("选中岗位后在此显示岗位描述…")
        layout.addWidget(self.detail_edit, 2)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def set_jobs(self, jobs: list) -> None:
        """填充岗位表并缓存行对应的完整岗位 dict（self._jobs 与行号一一对应）。

        :param jobs: Job 模型或 dict 列表
        """
        keys = [
            "source", "title", "company", "city", "salary",
            "education", "experience", "publish_time", "match_score", "favorite",
        ]
        self._jobs = []
        self._model.removeRows(0, self._model.rowCount())
        for job in jobs:
            data = job.as_dict() if hasattr(job, "as_dict") else dict(job or {})
            self._jobs.append(data)
            row_values = {
                "source": data.get("source", ""),
                "title": data.get("title", ""),
                "company": data.get("company", ""),
                "city": data.get("city", ""),
                "salary": data.get("salary_raw") or data.get("salary") or "",
                "education": data.get("education", ""),
                "experience": data.get("experience", ""),
                "publish_time": data.get("publish_date", ""),
                "match_score": data.get("match_score", ""),
                "favorite": "★" if data.get("is_favorite") else "",
            }
            items = [QStandardItem(str(row_values.get(k, ""))) for k in keys]
            self._model.appendRow(items)

    def show_detail(self, text: str) -> None:
        """在下方详情区显示岗位描述文本。"""
        self.detail_edit.setPlainText(text)

    # ------------------------------------------------------------------ #
    # 内部回调
    # ------------------------------------------------------------------ #
    def _current_row(self):
        """返回当前选中行的行号；无选中返回 None。"""
        indexes = self.view.selectionModel().selectedRows()
        if not indexes:
            return None
        return indexes[0].row()

    def _on_selection_changed(self, selected, _deselected) -> None:
        """选中岗位行时，在详情区显示该岗位描述。"""
        indexes = selected.indexes()
        if not indexes:
            return
        row = indexes[0].row()
        if row < len(self._jobs):
            data = self._jobs[row]
            desc = data.get("description") or ""
            if desc:
                self.show_detail(desc)
            else:
                self.show_detail(
                    f"【岗位】{data.get('title', '')}\n【公司】{data.get('company', '')}\n\n"
                    "（该岗位暂无描述文本）"
                )

    def _query_and_show(self) -> None:
        """按筛选条件查询岗位并填充表格（筛选/刷新共用）。"""
        try:
            with get_connection() as conn:
                jobs = repository.list_jobs(
                    conn,
                    source=self._selected_source_key(),
                    city=self._selected_city(),
                    keyword=self.keyword_edit.text().strip() or None,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("查询岗位列表失败：%s", exc)
            jobs = []
        # 薪资为客户端二次过滤（repository.list_jobs 不直接支持薪资条件）
        salary_label = self.salary_combo.currentText()
        if salary_label != "全部薪资":
            jobs = [j for j in jobs if _salary_match(j, salary_label)]
        self.set_jobs(jobs)
        self._set_status(f"岗位列表：共 {len(jobs)} 条")

    def _selected_source_key(self):
        """来源下拉 → 站点标识；"全部来源" 返回 None。"""
        text = self.source_combo.currentText()
        if text == "全部来源":
            return None
        return _SOURCE_KEYS.get(text, text)

    def _selected_city(self):
        """城市下拉 → 城市名；"全部城市" 返回 None。"""
        text = self.city_combo.currentText()
        return None if text == "全部城市" else text

    def _on_filter(self) -> None:
        self._query_and_show()

    def _on_refresh(self) -> None:
        self._query_and_show()

    # ------------------------------------------------------------------ #
    # 添加到投递队列
    # ------------------------------------------------------------------ #
    def _on_add_to_queue(self) -> None:
        """把选中岗位加入待投递队列（AppState，去重），状态栏提示。"""
        row = self._current_row()
        if row is None or row >= len(self._jobs):
            QMessageBox.information(self, "岗位列表", "请先选中要加入投递队列的岗位")
            return
        job = self._jobs[row]
        state = AppState.get()
        state.add_selected(job)
        count = len(state.get_selected())
        self._set_status(f"已加入投递队列：{job.get('title', '')}（当前队列 {count} 条）")

    # ------------------------------------------------------------------ #
    # AI 匹配评分
    # ------------------------------------------------------------------ #
    def _on_ai_match(self) -> None:
        """对选中岗位做 AI 匹配评分（需先配置大模型 API 并选择简历）。"""
        row = self._current_row()
        if row is None or row >= len(self._jobs):
            QMessageBox.information(self, "岗位列表", "请先选中要评分的岗位")
            return
        job = self._jobs[row]
        try:
            ai = AiService.get()
            if not ai.is_configured():
                QMessageBox.warning(
                    self, "岗位列表", "尚未配置大模型 API，请先到「AI 设置」页完成配置"
                )
                return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "岗位列表", f"AI 服务不可用：{exc}")
            return

        resume_text = self._current_resume_text()
        if not resume_text:
            QMessageBox.warning(
                self, "岗位列表", "未找到当前简历文本，请先在「简历库」页选择一份简历"
            )
            return
        jd = job.get("description") or ""
        if not jd:
            QMessageBox.warning(self, "岗位列表", "该岗位没有描述文本，无法评分")
            return

        self._set_status("正在调用 AI 评分…")
        AsyncWorker.run_async(
            lambda: ai.match_score(jd, resume_text),
            on_success=lambda result: self._on_match_done(row, result),
            on_failure=self._on_match_failed,
        )

    def _current_resume_text(self) -> str:
        """返回 AppState 当前简历版本名的解析文本（供 AI 使用），找不到返回 ''。"""
        name = AppState.get().get_resume()
        if not name:
            return ""
        try:
            for resume in ResumeService.get().list_resumes():
                if resume.name == name:
                    return resume.content or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取当前简历文本失败：%s", exc)
        return ""

    def _on_match_done(self, row: int, result) -> None:
        """评分完成（主线程）：展示分数/理由/短板，并写回 jobs.match_score 列。"""
        result = result or {}
        score = result.get("score", 0)
        reasons = result.get("reasons") or []
        gaps = result.get("gaps") or []
        error = result.get("error")

        lines = [f"匹配评分：{score} 分", ""]
        if reasons:
            lines.append("【匹配理由】")
            lines.extend(f"• {r}" for r in reasons)
        if gaps:
            lines.append("【短板】")
            lines.extend(f"• {g}" for g in gaps)
        if error:
            lines.append(f"（说明：{error}）")
        QMessageBox.information(self, "AI 匹配评分", "\n".join(lines) if len(lines) > 2 else f"匹配评分：{score} 分")

        # 写回表格（模型第 9 列 = 匹配分）
        if 0 <= row < len(self._jobs):
            self._jobs[row]["match_score"] = score
            item = self._model.item(row, 8)
            if item is not None:
                item.setText(str(score))
            job_id = self._jobs[row].get("id")
            if job_id is not None:
                # repository 无 match_score 更新函数，此处直接 SQL 更新 jobs 表
                try:
                    with get_connection() as conn:
                        conn.execute(
                            "UPDATE jobs SET match_score=? WHERE id=?",
                            (float(score), job_id),
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("写回 match_score 失败：%s", exc)
        self._set_status("AI 评分完成")

    def _on_match_failed(self, msg: str) -> None:
        self._set_status("AI 评分失败")
        QMessageBox.warning(self, "岗位列表", f"AI 评分失败：{msg}")

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _set_status(self, text: str) -> None:
        """更新主窗口状态栏；不在主窗口内时仅记日志。"""
        set_status = getattr(self.window(), "set_status", None)
        if callable(set_status):
            set_status(text)
        else:
            logger.info("岗位列表状态：%s", text)
