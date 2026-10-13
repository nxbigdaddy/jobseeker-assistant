# -*- coding: utf-8 -*-
"""登录中心页：站点卡片列表（状态灯 + 登录/退出按钮）。

接线说明（服务层：app.services.auth_service.AuthService）：
- 页面创建时后台逐个 check_status 校验登录态并回填状态灯（结果映射
  ok→已登录，expired/unknown→未登录）；
- 注册 AuthService 状态回调，回调可能来自 worker 线程，统一经 ``status_signal``
  转发到主线程再更新状态灯；
- 登录：先检查该站点适配器是否已注册（AuthService.COLLECTORS），再弹出登录
  方式选择（扫码 / 短信验证码），扫码回调在 worker 线程触发，经 ``qr_signal``
  转发到主线程显示二维码窗口；
- 所有异步调用（check_status / login / logout）都放到 AsyncWorker 的独立线程
  事件循环里执行，绝不阻塞 GUI 主线程。
"""

import logging
import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.services.auth_service import AuthService
from app.ui.worker import AsyncWorker

logger = logging.getLogger(__name__)

#: 站点列表：key 为站点标识，name 为显示名
SITES = [
    {"key": "51job", "name": "51job（前程无忧）"},
    {"key": "zhaopin", "name": "智联招聘"},
    {"key": "lagou", "name": "拉勾网"},
    {"key": "liepin", "name": "猎聘"},
    {"key": "zhipin", "name": "Boss 直聘"},
]

#: 状态关键字 -> 状态灯文本（含 AuthService 状态别名：ok/expired/unknown/failed）
_STATE_LIGHT_TEXT = {
    "logged_in": "🟢 已登录",
    "login": "🟢 已登录",
    "ok": "🟢 已登录",
    "已登录": "🟢 已登录",
    "logging_in": "🟡 登录中",
    "pending": "🟡 登录中",
    "登录中": "🟡 登录中",
    "logged_out": "🔴 未登录",
    "offline": "🔴 未登录",
    "expired": "🔴 未登录",
    "unknown": "🔴 未登录",
    "failed": "🔴 未登录",
    "未登录": "🔴 未登录",
}

#: 二维码图片等文件就绪的最长轮询时间（秒）
_QR_WAIT_SECONDS = 5


def _map_status(status: str) -> str:
    """AuthService 状态 → 状态灯状态：ok→已登录；expired/unknown→未登录（红色）。"""
    return {
        "ok": "logged_in",
        "expired": "logged_out",
        "unknown": "logged_out",
    }.get(status, status)


async def _check_all_sites() -> list:
    """并行校验所有站点登录状态（并发上限 3，避免同时开太多浏览器），
    返回 [(site, 映射后状态)]。单站失败/超时按未登录处理，绝不阻塞整批。
    """
    import asyncio

    sem = asyncio.Semaphore(3)
    svc = AuthService.get()

    async def check(site_key: str) -> tuple:
        async with sem:
            try:
                # 单站最长 30 秒（含浏览器启动与站点访问），超时按未登录处理
                status = await asyncio.wait_for(svc.check_status(site_key), timeout=30)
                return (site_key, _map_status(status))
            except Exception as exc:  # noqa: BLE001
                logger.warning("站点 %s 登录状态检查失败: %s", site_key, exc)
                return (site_key, "logged_out")

    results = await asyncio.gather(*[check(s["key"]) for s in SITES])
    return results


async def _check_site(site_key: str) -> str:
    """校验单个站点登录状态，返回映射后的状态灯状态。"""
    status = await AuthService.get().check_status(site_key)
    return _map_status(status)


class LoginCenterPage(QWidget):
    """登录中心页：站点卡片列表，支持 set_site_status 更新状态灯。"""

    #: AuthService 状态回调转发信号（worker 线程 → 主线程）：(site, state)
    status_signal = Signal(str, str)
    #: 二维码路径转发信号（worker 线程 → 主线程）：str（图片路径）
    qr_signal = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cards = {}  # site key -> QFrame 卡片
        self._qr_dialog = None  # 二维码显示对话框（主线程对象）
        self._qr_label = None   # 二维码 QLabel（主线程对象）
        self._build_ui()
        # 初始状态：全部未登录（红色）
        for site in SITES:
            self.set_site_status(site["key"], "logged_out")

        # ---- 服务接线 ----
        # 状态回调可能来自 worker 线程，经信号转发到主线程更新状态灯
        self.status_signal.connect(self._on_status_slot)
        self.qr_signal.connect(self._on_qr_ready)
        AuthService.get().set_status_callback(self._on_status)
        # 启动时后台逐个校验站点登录态（结果映射后回填状态灯）
        AsyncWorker.run_async(
            _check_all_sites,
            on_success=self._apply_check_results,
            on_failure=lambda msg: logger.warning("登录状态校验失败：%s", msg),
        )

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        title = QLabel("登录中心", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        hint = QLabel(
            "管理各招聘站点账号的登录状态；登录/退出依赖浏览器会话（auth_service），"
            "服务接入后会自动回填状态灯。",
            self,
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 站点卡片列表（可滚动）
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        container = QWidget(scroll)
        card_layout = QVBoxLayout(container)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(10)
        card_layout.addStretch(1)

        for site in SITES:
            card = self._create_site_card(site)
            self._cards[site["key"]] = card
            card_layout.addWidget(card)

        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

    def _create_site_card(self, site: dict) -> QFrame:
        """创建单张站点卡片。"""
        card = QFrame(self)
        card.setObjectName("card")
        card.setFixedHeight(64)

        row = QHBoxLayout(card)
        row.setContentsMargins(16, 10, 12, 10)
        row.setSpacing(12)

        name_label = QLabel(site["name"], card)
        name_label.setObjectName("siteName")
        row.addWidget(name_label)

        light_label = QLabel("🔴 未登录", card)
        light_label.setObjectName("siteLight")
        row.addWidget(light_label)
        row.addStretch(1)

        site_key = site["key"]
        login_btn = QPushButton("登录/重新登录", card)
        login_btn.clicked.connect(lambda checked=False, k=site_key: self._on_login(k))
        manual_btn = QPushButton("手动登录", card)
        manual_btn.setObjectName("secondary")
        manual_btn.clicked.connect(lambda checked=False, k=site_key: self._on_manual_login(k))
        logout_btn = QPushButton("退出登录", card)
        logout_btn.setObjectName("secondary")
        logout_btn.clicked.connect(lambda checked=False, k=site_key: self._on_logout(k))
        row.addWidget(login_btn)
        row.addWidget(manual_btn)
        row.addWidget(logout_btn)
        return card

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #
    def set_site_status(self, site: str, state: str) -> None:
        """更新指定站点卡片的登录状态灯。

        :param site:  站点标识（如 "51job"、"boss"），大小写不敏感
        :param state: 状态关键字，如 logged_in / logging_in / logged_out（也支持中文别名）

        兜底：状态进入「登录中」后 35 秒内未再更新（检查/登录流程卡住或超时），
        自动回退为「未登录」并提示，避免状态灯永远停在"登录中"。
        """
        if not hasattr(self, "_status_timers"):
            self._status_timers = {}

        card = self._cards.get(site)
        if card is None:
            # 找不到时按 key 做一次大小写不敏感的模糊匹配
            for key, candidate in self._cards.items():
                if key.lower() == str(site).lower():
                    card = candidate
                    break
        if card is None:
            logger.warning("登录中心：未知站点 %s，忽略状态更新", site)
            return

        norm = str(state).lower()
        text = _STATE_LIGHT_TEXT.get(norm, "🔴 未登录")
        light = card.findChild(QLabel, "siteLight")
        if light is not None:
            light.setText(text)

        # 「登录中」超时兜底定时器管理
        timer = self._status_timers.get(site)
        if norm in ("logging_in", "pending", "登录中"):
            if timer is None:
                from PySide6.QtCore import QTimer

                t = QTimer(self)
                t.setSingleShot(True)
                t.timeout.connect(lambda s=site: self._on_status_timeout(s))
                t.start(35_000)
                self._status_timers[site] = t
        else:
            if timer is not None:
                timer.stop()
                self._status_timers.pop(site, None)

    def _on_status_timeout(self, site: str) -> None:
        """「登录中」超时兜底：强制回退为未登录并提示。"""
        self._status_timers.pop(site, None)
        logger.warning("站点 %s 登录状态检查/登录流程超时（>35s），状态灯回退为未登录", site)
        self.set_site_status(site, "logged_out")
        self._set_status(f"{site} 登录状态检查超时，请重试")

    # ------------------------------------------------------------------ #
    # AuthService 状态回调（可能来自 worker 线程）→ 信号转发主线程
    # ------------------------------------------------------------------ #
    def _on_status(self, site: str, state: str) -> None:
        """AuthService 状态回调（worker 线程可能调用）：仅转发信号，不操作控件。"""
        self.status_signal.emit(site, state)

    def _on_status_slot(self, site: str, state: str) -> None:
        """主线程槽：按状态更新对应站点状态灯。"""
        self.set_site_status(site, state)

    def _apply_check_results(self, results) -> None:
        """启动校验完成后（主线程）批量回填状态灯。"""
        for site_key, state in (results or []):
            self.set_site_status(site_key, state)

    # ------------------------------------------------------------------ #
    # 登录 / 退出
    # ------------------------------------------------------------------ #
    def _on_login(self, site_key: str) -> None:
        """登录/重新登录：校验适配器已注册后，弹登录方式选择并执行。"""
        service = AuthService.get()
        if site_key not in service.COLLECTORS:
            QMessageBox.warning(self, "登录中心", f"该站点适配器尚未实现（{site_key}）")
            return

        mode = self._choose_login_mode(site_key)
        if mode == "qr":
            self._set_status(f"{site_key} 正在准备扫码登录…")
            AsyncWorker.run_async(
                lambda: service.login(site_key, mode="qr", qr_callback=self._show_qr),
                on_success=lambda ok: self._on_login_done(site_key, bool(ok), "扫码"),
                on_failure=lambda msg: self._on_login_done(site_key, False, "扫码", msg),
            )
        elif mode == "sms":
            phone, ok1 = QInputDialog.getText(self, "短信登录", f"{site_key}：请输入手机号：")
            if not ok1 or not phone.strip():
                return
            code, ok2 = QInputDialog.getText(self, "短信登录", "请输入短信验证码：")
            if not ok2 or not code.strip():
                return
            self._set_status(f"{site_key} 正在短信登录…")
            AsyncWorker.run_async(
                lambda: service.login(
                    site_key, mode="sms", phone=phone.strip(), code=code.strip()
                ),
                on_success=lambda ok: self._on_login_done(site_key, bool(ok), "短信"),
                on_failure=lambda msg: self._on_login_done(site_key, False, "短信", msg),
            )
        # mode 为空 = 用户取消

    def _choose_login_mode(self, site_key: str) -> str:
        """弹出登录方式选择对话框，返回 'qr' / 'sms'；取消返回 ''。"""
        site_name = next((s["name"] for s in SITES if s["key"] == site_key), site_key)
        dialog = QDialog(self)
        dialog.setWindowTitle("选择登录方式")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(12)
        hint = QLabel(f"请选择 {site_name} 的登录方式：", dialog)
        layout.addWidget(hint)

        qr_btn = QPushButton("扫码登录", dialog)
        qr_btn.clicked.connect(lambda: dialog.done(1))
        sms_btn = QPushButton("短信验证码", dialog)
        sms_btn.setObjectName("secondary")
        sms_btn.clicked.connect(lambda: dialog.done(2))
        cancel_btn = QPushButton("取消", dialog)
        cancel_btn.clicked.connect(lambda: dialog.done(0))
        layout.addWidget(qr_btn)
        layout.addWidget(sms_btn)
        layout.addWidget(cancel_btn)

        result = dialog.exec()
        return {1: "qr", 2: "sms"}.get(result, "")

    def _on_logout(self, site_key: str) -> None:
        """退出登录：确认后后台清空该站点 Cookie，完成后刷新状态灯。"""
        service = AuthService.get()
        if site_key not in service.COLLECTORS:
            QMessageBox.warning(self, "登录中心", f"该站点适配器尚未实现（{site_key}）")
            return
        site_name = next((s["name"] for s in SITES if s["key"] == site_key), site_key)
        ret = QMessageBox.question(
            self, "退出登录", f"确定退出 {site_name} 的登录吗？"
        )
        if ret != QMessageBox.Yes:
            return
        self._set_status(f"{site_key} 正在退出登录…")
        AsyncWorker.run_async(
            lambda: service.logout(site_key),
            on_success=lambda _r: self._refresh_site_status(site_key),
            on_failure=lambda msg: self._on_logout_failed(site_key, msg),
        )

    def _on_logout_failed(self, site_key: str, msg: str) -> None:
        self._set_status(f"{site_key} 退出登录失败")
        QMessageBox.warning(self, "退出登录", f"退出失败：{msg}")

    def _refresh_site_status(self, site_key: str) -> None:
        """后台重新校验单个站点登录状态并更新状态灯。"""
        AsyncWorker.run_async(
            lambda: _check_site(site_key),
            on_success=lambda state: self.set_site_status(site_key, state),
            on_failure=lambda msg: self.set_site_status(site_key, "logged_out"),
        )

    def _on_login_done(self, site_key: str, ok: bool, mode: str, err: str = "") -> None:
        """登录任务结束（主线程）：关闭二维码窗口并提示结果。"""
        self._close_qr_dialog()
        site_name = next((s["name"] for s in SITES if s["key"] == site_key), site_key)
        if ok:
            self._set_status(f"{site_name} {mode}登录成功")
            QMessageBox.information(self, "登录中心", f"{site_name} {mode}登录成功")
        else:
            self._set_status(f"{site_name} {mode}登录失败")
            QMessageBox.warning(
                self, "登录中心",
                f"{site_name} {mode}登录失败：{err or '请查看日志或检查网络'}",
            )

    # ------------------------------------------------------------------ #
    # 手动登录模式（打开浏览器由用户自己操作，程序只负责检测结果）
    # ------------------------------------------------------------------ #
    def _on_manual_login(self, site_key: str) -> None:
        """手动登录：打开浏览器让用户自己登录，用户关闭浏览器后程序自动检测。"""
        service = AuthService.get()
        if site_key not in service.COLLECTORS:
            QMessageBox.warning(self, "登录中心", f"该站点适配器尚未实现（{site_key}）")
            return
        site_name = next((s["name"] for s in SITES if s["key"] == site_key), site_key)
        ret = QMessageBox.question(
            self,
            "手动登录",
            f"将打开浏览器登录 {site_name}。\n\n"
            f"请在浏览器中完成登录（扫码/验证码），\n"
            f"登录成功后【关闭浏览器窗口】，程序会自动检测结果。\n\n"
            f"最长等待 15 分钟，继续吗？",
        )
        if ret != QMessageBox.Yes:
            return
        self._set_status(f"{site_name} 浏览器已打开，请手动登录后关闭窗口…")
        AsyncWorker.run_async(
            lambda: service.manual_login(site_key),
            on_success=lambda ok: self._on_manual_login_done(site_key, bool(ok)),
            on_failure=lambda msg: self._on_manual_login_done(site_key, False, msg),
        )

    def _on_manual_login_done(self, site_key: str, ok: bool, err: str = "") -> None:
        """手动登录结束（主线程）：更新状态灯并提示结果。"""
        site_name = next((s["name"] for s in SITES if s["key"] == site_key), site_key)
        if ok:
            self._set_status(f"{site_name} 手动登录成功")
            QMessageBox.information(self, "登录中心", f"{site_name} 登录成功")
        else:
            self._set_status(f"{site_name} 手动登录未检测到")
            QMessageBox.warning(
                self, "登录中心",
                f"{site_name} 手动登录失败：{err or '未检测到登录态，请确认已登录并关闭浏览器窗口'}",
            )

    # ------------------------------------------------------------------ #
    # 二维码展示（扫码回调 → 信号 → 主线程轮询加载图片）
    # ------------------------------------------------------------------ #
    def _show_qr(self, qr_path: str) -> None:
        """QR 回调（worker 线程调用）：仅转发路径到主线程信号，不直接操作控件。"""
        self.qr_signal.emit(str(qr_path))

    def _on_qr_ready(self, qr_path: str) -> None:
        """主线程槽：创建/复用二维码窗口并轮询等待图片文件就绪后显示。"""
        if self._qr_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("扫码登录")
            dialog.setModal(False)
            layout = QVBoxLayout(dialog)
            label = QLabel("二维码加载中…", dialog)
            label.setObjectName("qrLabel")
            label.setAlignment(Qt.AlignCenter)
            label.setMinimumSize(280, 280)
            layout.addWidget(label)
            self._qr_dialog = dialog
            self._qr_label = label
        self._qr_dialog.show()
        self._qr_dialog.raise_()
        self._load_qr_image(qr_path, attempts=_QR_WAIT_SECONDS * 2)

    def _load_qr_image(self, qr_path: str, attempts: int) -> None:
        """轮询等待二维码图片文件存在（最多 5 秒）后加载显示。"""
        if self._qr_label is None:
            return
        if attempts <= 0:
            self._qr_label.setText("二维码图片加载超时，请重新登录")
            return
        try:
            if os.path.exists(qr_path) and os.path.getsize(qr_path) > 0:
                pixmap = QPixmap(qr_path)
                if not pixmap.isNull():
                    scaled = pixmap.scaled(
                        260, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                    self._qr_label.setPixmap(scaled)
                    self._qr_dialog.adjustSize()
                    return
        except OSError as exc:
            logger.warning("二维码图片读取失败：%s", exc)
        # 文件尚未就绪（截图与回调存在时序差），500ms 后重试
        QTimer.singleShot(
            500, lambda: self._load_qr_image(qr_path, attempts - 1)
        )

    def _close_qr_dialog(self) -> None:
        """关闭二维码窗口（登录成功/失败后调用，仅主线程）。"""
        if self._qr_dialog is not None:
            self._qr_dialog.close()
            self._qr_dialog.deleteLater()
            self._qr_dialog = None
            self._qr_label = None

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _set_status(self, text: str) -> None:
        """更新主窗口状态栏；不在主窗口内时仅记日志。"""
        set_status = getattr(self.window(), "set_status", None)
        if callable(set_status):
            set_status(text)
        else:
            logger.info("登录中心状态：%s", text)
