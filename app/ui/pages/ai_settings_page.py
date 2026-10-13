# -*- coding: utf-8 -*-
"""AI 设置页：API 配置表单 + 提示词管理。

接线说明（app.db.repository / app.services.ai_service.AiService）：
- showEvent：load_settings 从 ai_settings 表填表单；load_prompts 从 prompts 表
  填下拉与内容；
- 保存配置：repository.save_ai_settings（models.AISettings，temperature 用
  QDoubleSpinBox 值）；
- 保存提示词：repository.save_prompt（当前 feature）；恢复默认：从
  repository.DEFAULT_PROMPTS 取回该 feature 的 content 重填；
- 测试连接：AiService.chat 在 AsyncWorker 后台执行，成功弹"连接成功：{前 50 字}"，
  失败（RuntimeError 等）弹错误信息。
"""

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.db import repository
from app.db.database import get_connection
from app.db.models import AISettings, PromptTemplate
from app.db.repository import DEFAULT_PROMPTS
from app.services.ai_service import AiService
from app.ui.worker import AsyncWorker

logger = logging.getLogger(__name__)

#: 提示词功能列表：(键, 显示名)，对应设计文档 §11.4 的 A1~A9
PROMPT_FUNCTIONS = [
    ("resume_optimize", "A1 简历优化"),
    ("match_score", "A2 匹配评分"),
    ("apply_strategy", "A3 投递策略"),
    ("greeting", "A4 招呼语"),
    ("job_recommend", "A5 岗位推荐"),
    ("follow_up", "A6 跟进话术"),
    ("interview_predict", "A7 面试预测"),
    ("salary_analysis", "A8 薪资分析"),
    ("review_report", "A9 复盘报告"),
]


class AISettingsPage(QWidget):
    """AI 设置页。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._prompts = {}  # key -> 提示词文本（当前页内存缓存）
        self._build_ui()

    def showEvent(self, event) -> None:
        """页面每次显示时从 DB 载入配置与提示词。"""
        super().showEvent(event)
        self.load_settings_from_db()
        self.load_prompts_from_db()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("AI 设置", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        layout.addWidget(self._build_config_group())
        layout.addWidget(self._build_prompt_group(), 1)

    def _build_config_group(self) -> QGroupBox:
        """上方：API 配置表单。"""
        group = QGroupBox("API 配置", self)
        form = QFormLayout(group)
        form.setContentsMargins(10, 14, 10, 10)
        form.setSpacing(8)

        self.enabled_check = QCheckBox("启用 AI 功能", group)
        form.addRow("启用", self.enabled_check)

        self.base_url_edit = QLineEdit(group)
        self.base_url_edit.setPlaceholderText("如 https://api.deepseek.com/v1（兼容 OpenAI 协议）")
        form.addRow("Base URL", self.base_url_edit)

        self.api_key_edit = QLineEdit(group)
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("请输入 API Key")
        form.addRow("API Key", self.api_key_edit)

        self.model_edit = QLineEdit(group)
        self.model_edit.setPlaceholderText("如 deepseek-chat / gpt-4o-mini")
        form.addRow("模型名", self.model_edit)

        self.temperature_spin = QDoubleSpinBox(group)
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.temperature_spin.setDecimals(1)
        self.temperature_spin.setValue(0.7)
        form.addRow("温度", self.temperature_spin)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        save_btn = QPushButton("保存配置", group)
        save_btn.clicked.connect(self._on_save_config)
        buttons.addWidget(save_btn)

        test_btn = QPushButton("测试连接", group)
        test_btn.setObjectName("secondary")
        test_btn.clicked.connect(self._on_test_connection)
        buttons.addWidget(test_btn)
        buttons.addStretch(1)

        form.addRow("", buttons)
        return group

    def _build_prompt_group(self) -> QGroupBox:
        """下方：提示词管理。"""
        group = QGroupBox("提示词管理", self)
        v = QVBoxLayout(group)
        v.setContentsMargins(10, 14, 10, 10)
        v.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("功能:", group))
        self.func_combo = QComboBox(group)
        for key, name in PROMPT_FUNCTIONS:
            self.func_combo.addItem(name, key)
        self.func_combo.currentIndexChanged.connect(self._on_func_changed)
        top.addWidget(self.func_combo)
        top.addStretch(1)
        v.addLayout(top)

        self.prompt_edit = QPlainTextEdit(group)
        self.prompt_edit.setPlaceholderText(
            "当前功能的提示词内容…（系统提示词+用户模板，"
            "支持 {jd}、{resume}、{platform} 等占位符）"
        )
        v.addWidget(self.prompt_edit, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        save_prompt_btn = QPushButton("保存提示词", group)
        save_prompt_btn.clicked.connect(self._on_save_prompt)
        buttons.addWidget(save_prompt_btn)

        restore_btn = QPushButton("恢复默认", group)
        restore_btn.setObjectName("secondary")
        restore_btn.clicked.connect(self._on_restore_prompt)
        buttons.addWidget(restore_btn)
        buttons.addStretch(1)

        v.addLayout(buttons)
        return group

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def load_settings(self, enabled=True, base_url="", api_key="", model="", temperature=0.7, **_):
        """填充 API 配置表单。"""
        self.enabled_check.setChecked(bool(enabled))
        self.base_url_edit.setText(base_url or "")
        self.api_key_edit.setText(api_key or "")
        self.model_edit.setText(model or "")
        self.temperature_spin.setValue(float(temperature))

    def load_prompts(self, prompts: dict) -> None:
        """载入提示词内容（key -> 文本），并刷新当前功能显示。"""
        self._prompts = dict(prompts or {})
        self._on_func_changed()

    # ------------------------------------------------------------------ #
    # DB 载入
    # ------------------------------------------------------------------ #
    def load_settings_from_db(self) -> None:
        """从 ai_settings 表读取配置填表单。"""
        try:
            with get_connection() as conn:
                s = repository.get_ai_settings(conn)
            self.load_settings(
                enabled=s.enabled,
                base_url=s.base_url,
                api_key=YOUR_SECRET,
                model=s.model,
                temperature=s.temperature,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 AI 配置失败：%s", exc)

    def load_prompts_from_db(self) -> None:
        """从 prompts 表读取全部提示词填下拉与内容。"""
        try:
            with get_connection() as conn:
                prompts = repository.list_prompts(conn)
            self.load_prompts({p.feature: p.content for p in prompts})
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取提示词失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 内部回调
    # ------------------------------------------------------------------ #
    def _on_func_changed(self) -> None:
        """切换功能下拉框时，显示该功能的提示词。"""
        key = self.func_combo.currentData()
        self.prompt_edit.setPlainText(self._prompts.get(key, ""))

    def _on_save_config(self) -> None:
        """把表单内容持久化到 ai_settings 表（单行 id=1）。"""
        try:
            with get_connection() as conn:
                repository.save_ai_settings(
                    conn,
                    AISettings(
                        base_url=self.base_url_edit.text().strip(),
                        api_key=YOUR_SECRET).strip(),
                        model=self.model_edit.text().strip(),
                        temperature=self.temperature_spin.value(),
                        enabled=1 if self.enabled_check.isChecked() else 0,
                    ),
                )
            self._set_status("AI 配置已保存")
            QMessageBox.information(self, "AI 设置", "AI 配置已保存")
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存 AI 配置失败")
            QMessageBox.warning(self, "AI 设置", f"保存失败：{exc}")

    def _on_test_connection(self) -> None:
        """后台测试大模型连接（chat 方法未配置时抛 RuntimeError）。"""
        try:
            ai = AiService.get()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "AI 设置", f"AI 服务不可用：{exc}")
            return
        self._set_status("正在测试连接…")
        AsyncWorker.run_async(
            lambda: ai.chat("你是测试助手", "回复 OK 即可"),
            on_success=lambda reply: self._on_test_ok(str(reply or "")),
            on_failure=self._on_test_failed,
        )

    def _on_test_ok(self, reply: str) -> None:
        self._set_status("AI 连接正常")
        QMessageBox.information(self, "AI 设置", f"连接成功：{reply[:50]}")

    def _on_test_failed(self, msg: str) -> None:
        self._set_status("AI 连接失败")
        QMessageBox.warning(self, "AI 设置", f"连接失败：{msg}")

    def _on_save_prompt(self) -> None:
        """保存当前功能的提示词到 prompts 表（feature 幂等 upsert）。"""
        key = self.func_combo.currentData()
        content = self.prompt_edit.toPlainText()
        self._prompts[key] = content
        try:
            with get_connection() as conn:
                repository.save_prompt(
                    conn,
                    PromptTemplate(
                        feature=key,
                        name=self.func_combo.currentText(),
                        content=content,
                        enabled=1,
                    ),
                )
            self._set_status(f"提示词已保存：{key}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存提示词失败")
            QMessageBox.warning(self, "AI 设置", f"保存提示词失败：{exc}")

    def _on_restore_prompt(self) -> None:
        """从内置默认提示词恢复当前功能的内容（仅重填编辑器）。"""
        key = self.func_combo.currentData()
        content = ""
        for item in DEFAULT_PROMPTS:
            if item.get("feature") == key:
                content = item.get("content", "")
                break
        if not content:
            QMessageBox.information(self, "AI 设置", "该功能没有内置默认提示词")
            return
        self._prompts[key] = content
        self._on_func_changed()
        self._set_status(f"已恢复默认提示词：{key}")

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _set_status(self, text: str) -> None:
        """更新主窗口状态栏；不在主窗口内时仅记日志。"""
        set_status = getattr(self.window(), "set_status", None)
        if callable(set_status):
            set_status(text)
        else:
            logger.info("AI 设置状态：%s", text)
