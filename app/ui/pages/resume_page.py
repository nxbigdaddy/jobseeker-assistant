# -*- coding: utf-8 -*-
"""简历库页：简历版本列表 + 上传/删除/AI 优化 + 简历文本预览。

接线说明（服务层：app.services.resume_service.ResumeService /
app.services.ai_service.AiService / app.state.AppState）：
- showEvent：_refresh 从 ResumeService.list_resumes() 填表；选中行 →
  show_resume_text(content)，并把该版本设为 AppState 当前投递简历
  （AppState.set_resume，供投递中心/岗位列表 AI 使用）；
- 上传：QFileDialog 选文件 + QInputDialog 输入目标岗位 → ResumeService.add_resume
  （内部复制副本到 data/resumes 并解析文本）→ 刷新；
- 删除：确认后 ResumeService.delete_resume（连同文件副本）；
- AI 优化：选中简历内容 + 输入 JD → AiService.optimize_resume 在 AsyncWorker
  后台执行 → 结果对话框（可编辑）→「另存为新版本」（add_resume 把 AI 结果
  写入 txt 保存为新版本，不覆盖原版）。
"""

import logging
import os
import tempfile
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services.ai_service import AiService
from app.services.resume_service import ResumeService
from app.state import AppState
from app.ui.worker import AsyncWorker

logger = logging.getLogger(__name__)

#: 简历列表列
RESUME_COLUMNS = ["版本名", "目标岗位", "更新时间"]


def _resume_to_dict(resume) -> dict:
    """Resume 模型 → set_resumes 展示字典。"""
    return {
        "name": resume.name,
        "target": resume.target_title,
        "updated_at": resume.updated_at,
    }


class ResumePage(QWidget):
    """简历库页。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._resumes = []  # 表行 -> Resume 模型（删除/AI 优化用）
        self._build_ui()
        self.set_resumes([])
        self.show_resume_text("")

    def showEvent(self, event) -> None:
        """页面每次显示时刷新简历列表。"""
        super().showEvent(event)
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("简历库", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._build_list_group(), 2)
        body.addWidget(self._build_preview_group(), 3)
        layout.addLayout(body, 1)

    def _build_list_group(self) -> QGroupBox:
        """左侧：简历版本列表 + 操作按钮。"""
        group = QGroupBox("简历版本", self)
        v = QVBoxLayout(group)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        self.resume_table = QTableWidget(0, len(RESUME_COLUMNS), group)
        self.resume_table.setHorizontalHeaderLabels(RESUME_COLUMNS)
        self.resume_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.resume_table.setSelectionMode(QTableWidget.SingleSelection)
        self.resume_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.resume_table.setAlternatingRowColors(True)
        self.resume_table.horizontalHeader().setStretchLastSection(True)
        self.resume_table.itemSelectionChanged.connect(self._on_selection_changed)
        v.addWidget(self.resume_table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        upload_btn = QPushButton("上传简历", group)
        upload_btn.clicked.connect(self._on_upload)
        buttons.addWidget(upload_btn)

        delete_btn = QPushButton("删除", group)
        delete_btn.setObjectName("danger")
        delete_btn.clicked.connect(self._on_delete)
        buttons.addWidget(delete_btn)

        optimize_btn = QPushButton("AI 优化", group)
        optimize_btn.clicked.connect(self._on_optimize)
        buttons.addWidget(optimize_btn)
        buttons.addStretch(1)

        v.addLayout(buttons)
        return group

    def _build_preview_group(self) -> QGroupBox:
        """右侧：简历文本预览。"""
        group = QGroupBox("简历文本预览", self)
        v = QVBoxLayout(group)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        self.preview_edit = QPlainTextEdit(group)
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setPlaceholderText("选择简历版本后在此显示解析出的简历文本…")
        v.addWidget(self.preview_edit, 1)
        return group

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def set_resumes(self, resumes: list) -> None:
        """填充简历版本列表。

        :param resumes: [{name, target, updated_at}, ...]
        """
        self.resume_table.setRowCount(0)
        for resume in resumes:
            row = self.resume_table.rowCount()
            self.resume_table.insertRow(row)
            values = (
                resume.get("name", ""),
                resume.get("target", ""),
                resume.get("updated_at", ""),
            )
            for col, value in enumerate(values):
                self.resume_table.setItem(row, col, QTableWidgetItem(str(value)))

    def show_resume_text(self, text: str) -> None:
        """在右侧预览区显示简历文本。"""
        self.preview_edit.setPlainText(text)

    # ------------------------------------------------------------------ #
    # 数据刷新
    # ------------------------------------------------------------------ #
    def _refresh(self) -> None:
        """从 ResumeService 刷新简历列表。"""
        try:
            resumes = ResumeService.get().list_resumes()
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取简历列表失败：%s", exc)
            resumes = []
        self._resumes = list(resumes)
        self.set_resumes([_resume_to_dict(r) for r in resumes])

    def _selected_resume(self):
        """返回当前选中行的 Resume 模型；无选中返回 None。"""
        rows = self.resume_table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        if row < len(self._resumes):
            return self._resumes[row]
        return None

    # ------------------------------------------------------------------ #
    # 内部回调
    # ------------------------------------------------------------------ #
    def _on_selection_changed(self) -> None:
        """选中简历：预览其解析文本，并设为 AppState 当前投递简历。"""
        resume = self._selected_resume()
        if resume is None:
            return
        self.show_resume_text(resume.content or "")
        # 设为当前投递用简历版本（投递中心 / 岗位列表 AI 读取）
        AppState.get().set_resume(resume.name)
        self._set_status(f"当前投递简历：{resume.name}")

    def _on_upload(self) -> None:
        """选择 PDF/Word/TXT 简历文件：目标岗位后入库并刷新。"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择简历文件",
            "",
            "简历文件 (*.pdf *.doc *.docx *.txt *.md);;所有文件 (*)",
        )
        if not path:
            return
        name = Path(path).stem
        target, ok = QInputDialog.getText(self, "上传简历", "目标岗位（如：Java 开发）：")
        if not ok:
            return
        self._set_status(f"正在上传简历：{name}…")
        result = ResumeService.get().add_resume(
            name=name, target_title=target.strip(), file_path=path
        )
        if result:
            self._refresh()
            self._set_status(f"简历已上传：{name}")
        else:
            QMessageBox.warning(
                self, "简历库", "上传失败，请确认文件格式（PDF/Word/TXT）后重试"
            )

    def _on_delete(self) -> None:
        """删除选中的简历版本（含数据/文件副本）。"""
        resume = self._selected_resume()
        if resume is None:
            QMessageBox.information(self, "简历库", "请先选中要删除的简历版本")
            return
        ret = QMessageBox.question(
            self, "简历库", f"确定删除简历版本「{resume.name}」吗？\n（文件副本将一并删除）"
        )
        if ret != QMessageBox.Yes:
            return
        if ResumeService.get().delete_resume(resume.id):
            self._refresh()
            # 若删除的是当前投递简历，清空选择
            if AppState.get().get_resume() == resume.name:
                AppState.get().set_resume("")
            self._set_status(f"简历已删除：{resume.name}")
        else:
            QMessageBox.warning(self, "简历库", "删除失败，请查看日志")

    def _on_optimize(self) -> None:
        """AI 优化选中简历：输入 JD 后后台调用，结果可另存为新版本。"""
        resume = self._selected_resume()
        if resume is None:
            QMessageBox.information(self, "简历库", "请先选中要优化的简历版本")
            return
        try:
            ai = AiService.get()
            if not ai.is_configured():
                QMessageBox.warning(
                    self, "简历库", "尚未配置大模型 API，请先到「AI 设置」页完成配置"
                )
                return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "简历库", f"AI 服务不可用：{exc}")
            return
        jd, ok = QInputDialog.getMultiLineText(
            self, "AI 优化简历", "请输入目标岗位 JD（职位描述）："
        )
        if not ok or not jd.strip():
            return
        content = resume.content or ""
        if not content:
            QMessageBox.warning(self, "简历库", "该简历没有解析文本，无法优化")
            return
        self._set_status("正在调用 AI 优化简历…")
        AsyncWorker.run_async(
            lambda: ai.optimize_resume(jd, content),
            on_success=lambda text: self._on_optimize_done(resume, str(text or "")),
            on_failure=self._on_optimize_failed,
        )

    def _on_optimize_done(self, resume, ai_text: str) -> None:
        """AI 优化完成（主线程）：展示结果并支持「另存为新版本」。"""
        if not ai_text.strip():
            QMessageBox.warning(self, "简历库", "AI 未返回优化结果")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("AI 优化结果")
        dialog.resize(680, 520)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(8)
        layout.addWidget(QLabel("优化结果（可编辑后另存为新版本，不覆盖原版）：", dialog))
        edit = QPlainTextEdit(dialog)
        edit.setPlainText(ai_text)
        layout.addWidget(edit, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Close, dialog
        )
        buttons.button(QDialogButtonBox.Save).setText("另存为新版本")
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        self._save_optimized_version(resume, edit.toPlainText())

    def _save_optimized_version(self, resume, text: str) -> None:
        """把 AI 优化结果保存为新简历版本（add_resume 存 txt，不覆盖原版）。"""
        text = (text or "").strip()
        if not text:
            return
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="resume_optimized_")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            name = f"优化版-{resume.name}" if resume.name else "优化版简历"
            result = ResumeService.get().add_resume(
                name=name, target_title=resume.target_title or "", file_path=tmp_path
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存 AI 优化版本失败")
            QMessageBox.warning(self, "简历库", f"保存优化结果失败：{exc}")
            return
        finally:
            # add_resume 已复制副本到 data/resumes，临时文件可清理
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        if result:
            self._refresh()
            self._set_status(f"已保存新简历版本：{name}")
            QMessageBox.information(self, "简历库", f"已保存新简历版本：{name}")
        else:
            QMessageBox.warning(self, "简历库", "保存新简历版本失败，请查看日志")

    def _on_optimize_failed(self, msg: str) -> None:
        self._set_status("AI 优化失败")
        QMessageBox.warning(self, "简历库", f"AI 优化失败：{msg}")

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _set_status(self, text: str) -> None:
        """更新主窗口状态栏；不在主窗口内时仅记日志。"""
        set_status = getattr(self.window(), "set_status", None)
        if callable(set_status):
            set_status(text)
        else:
            logger.info("简历库状态：%s", text)
