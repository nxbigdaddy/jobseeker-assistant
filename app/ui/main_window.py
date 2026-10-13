# -*- coding: utf-8 -*-
"""主窗口模块：个人求职助手主窗口。

布局：左侧固定宽度导航（QListWidget，7 项）+ 右侧 QStackedWidget 页签。
视觉规范统一写在 GLOBAL_QSS（窗口背景 #f5f6fa、卡片白底圆角、主色 #3b82f6），
通过 setStyleSheet 应用到整个主窗口。
"""

import logging
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.ui.pages.ai_settings_page import AISettingsPage
from app.ui.pages.apply_page import ApplyPage
from app.ui.pages.job_list_page import JobListPage
from app.ui.pages.log_page import LogPage
from app.ui.pages.login_center import LoginCenterPage
from app.ui.pages.resume_page import ResumePage
from app.ui.pages.search_page import SearchPage

logger = logging.getLogger(__name__)

#: 全局样式表：统一视觉规范（背景 #f5f6fa / 卡片白底圆角 / 主色 #3b82f6）
GLOBAL_QSS = """
* {
    font-family: "Microsoft YaHei", "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 13px;
    color: #1f2937;
}

QMainWindow, QWidget#pageStack {
    background-color: #f5f6fa;
}

/* ============ 左侧导航 ============ */
QWidget#navContainer {
    background-color: #ffffff;
    border-right: 1px solid #e5e7eb;
}
QLabel#navTitle {
    font-size: 15px;
    font-weight: bold;
    color: #3b82f6;
    padding: 14px 12px 8px 18px;
}
QListWidget#navList {
    background-color: transparent;
    border: none;
    padding-top: 4px;
}
QListWidget#navList::item {
    height: 42px;
    padding-left: 18px;
    color: #374151;
    border: none;
    border-radius: 6px;
    margin: 2px 8px;
}
QListWidget#navList::item:selected {
    background-color: #3b82f6;
    color: #ffffff;
}
QListWidget#navList::item:hover:!selected {
    background-color: #eff6ff;
}

/* ============ 卡片 ============ */
QFrame#card {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
}

/* ============ 标题 / 提示 ============ */
QLabel#pageTitle {
    font-size: 17px;
    font-weight: bold;
    color: #111827;
}
QLabel#hint {
    color: #6b7280;
    font-size: 12px;
}
QLabel#siteName {
    font-size: 14px;
    font-weight: bold;
    color: #111827;
}
QLabel#siteLight {
    font-size: 13px;
    color: #374151;
}

/* ============ 分组卡片 ============ */
QGroupBox {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 10px;
    color: #111827;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    font-weight: bold;
}

/* ============ 按钮 ============ */
QPushButton {
    background-color: #3b82f6;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 18px;
}
QPushButton:hover { background-color: #2563eb; }
QPushButton:pressed { background-color: #1d4ed8; }
QPushButton:disabled { background-color: #cbd5e1; color: #f8fafc; }
QPushButton#secondary { background-color: #e5e7eb; color: #374151; }
QPushButton#secondary:hover { background-color: #d1d5db; }
QPushButton#secondary:pressed { background-color: #9ca3af; }
QPushButton#danger { background-color: #ef4444; }
QPushButton#danger:hover { background-color: #dc2626; }

/* ============ 表格 ============ */
QTableWidget, QTableView {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    gridline-color: #eef2f7;
    alternate-background-color: #f8fafc;
    selection-background-color: #dbeafe;
    selection-color: #1e293b;
}
QTableWidget::item, QTableView::item {
    padding: 3px 6px;
}
QHeaderView::section {
    background-color: #f1f5f9;
    border: none;
    border-bottom: 1px solid #e5e7eb;
    padding: 6px 8px;
    color: #475569;
    font-weight: bold;
}
QTableCornerButton::section {
    background-color: #f1f5f9;
    border: none;
}

/* ============ 输入控件 ============ */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit {
    background-color: #ffffff;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: #bfdbfe;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus {
    border-color: #3b82f6;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background-color: #ffffff;
    border: 1px solid #e5e7eb;
    selection-background-color: #dbeafe;
    selection-color: #1e293b;
}

/* ============ 勾选框 ============ */
QCheckBox {
    spacing: 6px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #d1d5db;
    border-radius: 4px;
    background-color: #ffffff;
}
QCheckBox::indicator:checked {
    background-color: #3b82f6;
    border-color: #3b82f6;
}
QCheckBox::indicator:hover {
    border-color: #3b82f6;
}

/* ============ 滚动区域 / 滚动条 ============ */
QScrollArea {
    border: none;
    background: transparent;
}
QScrollArea > QWidget {
    background-color: transparent;
}
QScrollArea > QWidget > QWidget {
    background-color: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #cbd5e1;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #94a3b8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background: #cbd5e1;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover { background: #94a3b8; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

/* ============ 状态栏 ============ */
QStatusBar {
    background-color: #ffffff;
    border-top: 1px solid #e5e7eb;
}
QStatusBar QLabel {
    color: #374151;
}

/* ============ 对话框 / 提示 ============ */
QMessageBox {
    background-color: #ffffff;
}
QToolTip {
    background-color: #111827;
    color: #ffffff;
    border: none;
    padding: 4px 8px;
}
"""

#: 左侧导航项（顺序与 QStackedWidget 页面一一对应）
NAV_ITEMS = ["登录中心", "搜索采集", "岗位列表", "投递中心", "简历库", "AI 设置", "日志"]


class MainWindow(QMainWindow):
    """个人求职助手主窗口：左侧固定宽度导航 + 右侧 QStackedWidget 页签。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("个人求职助手")
        self.resize(1280, 800)
        self.setStyleSheet(GLOBAL_QSS)

        # 先注册站点采集器（登录中心页创建时会启动登录态检查线程，
        # 若此时尚未注册，检查会误报"适配器未实现"——故注册必须在建页面前完成）
        self._register_collectors()

        # ---- 页面实例（在 __init__ 中创建并 addWidget）----
        self._pages = {}
        self._stack = QStackedWidget(self)
        self._stack.setObjectName("pageStack")

        page_classes = [
            LoginCenterPage, SearchPage, JobListPage, ApplyPage,
            ResumePage, AISettingsPage, LogPage,
        ]
        for name, page_cls in zip(NAV_ITEMS, page_classes):
            page = page_cls(self._stack)
            self._pages[name] = page
            self._stack.addWidget(page)

        # 日志页：安装日志 Handler（幂等），让日志从启动起即可见
        self._pages["日志"].install()

        # ---- 左侧导航（固定宽度 ~180px）----
        self.nav_list = QListWidget(self)
        self.nav_list.setObjectName("navList")
        for name in NAV_ITEMS:
            item = QListWidgetItem(name)
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self.nav_list.addItem(item)
        # 选中项切换页面
        self.nav_list.currentRowChanged.connect(self._stack.setCurrentIndex)

        nav_title = QLabel("个人求职助手", self)
        nav_title.setObjectName("navTitle")

        nav_container = QWidget(self)
        nav_container.setObjectName("navContainer")
        nav_container.setFixedWidth(180)
        nav_layout = QVBoxLayout(nav_container)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)
        nav_layout.addWidget(nav_title)
        nav_layout.addWidget(self.nav_list, 1)

        # ---- 状态栏：状态文本 + 当前时间（QTimer 每秒刷新）----
        self._status_label = QLabel("采集空闲 / 投递空闲", self)
        self.statusBar().addWidget(self._status_label)

        self._clock_label = QLabel("", self)
        self.statusBar().addPermanentWidget(self._clock_label)

        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)
        self._update_clock()

        # ---- 组装 ----
        central = QWidget(self)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        root_layout.addWidget(nav_container)
        root_layout.addWidget(self._stack, 1)
        self.setCentralWidget(central)

        self.nav_list.setCurrentRow(0)
        self._center_on_screen()

        # ---- 服务接线：注册站点采集器 + 启动每日调度（失败不阻断界面）----
        self._init_services()

    def _register_collectors(self) -> None:
        """注册站点采集器（registry.register_all）；失败仅记日志，不阻断界面。"""
        try:
            from app.services.registry import register_all

            result = register_all()
            logger.info("站点采集器注册结果：%s", result)
        except Exception as exc:  # noqa: BLE001
            logger.exception("注册站点采集器失败：%s", exc)

    def _init_services(self) -> None:
        """启动每日调度服务（SchedulerService）。

        - 状态回调接到状态栏 set_status；
        - 任何异常只记日志，不影响主窗口显示。
        """
        try:
            from app.scheduler.scheduler import SchedulerService

            scheduler = SchedulerService.get()
            scheduler.set_status_callback(self.set_status)
            scheduler.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("启动调度服务失败：%s", exc)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def set_status(self, text: str) -> None:
        """供外部更新状态栏左侧的状态文本（如"采集中 / 投递空闲"）。"""
        self._status_label.setText(text)

    def page(self, name: str) -> QWidget:
        """按导航名取页面实例（如 "日志"）。"""
        return self._pages.get(name)

    # ------------------------------------------------------------------ #
    # 内部实现
    # ------------------------------------------------------------------ #
    def _update_clock(self) -> None:
        """刷新状态栏右侧的当前时间。"""
        self._clock_label.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def _center_on_screen(self) -> None:
        """将窗口移动到当前屏幕中央。"""
        screen = self.screen()
        if screen is None:
            return
        frame = self.frameGeometry()
        frame.moveCenter(screen.availableGeometry().center())
        self.move(frame.topLeft())
