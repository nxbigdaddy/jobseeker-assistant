# -*- coding: utf-8 -*-
"""登录服务模块 AuthService。

负责多站点登录会话的统一管理：登录状态校验、扫码/短信登录、
退出登录、站点显示名查询，并通过状态回调驱动 GUI 状态灯。

依赖说明：
- 仅依赖标准库（logging / typing），不依赖 asyncio 之外的第三方库；
- app.collectors.base / app.browser.driver 采用方法内延迟导入并容错，
  相关模块尚未落盘时记录日志并返回合理默认值，不影响本模块加载。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: 状态回调合法的状态集合（对应 GUI 登录中心状态灯）
VALID_STATES = ("ok", "expired", "unknown", "logging_in", "logged_in", "failed")


class AuthService:
    """登录服务：统一管理各招聘站点（51job / zhipin / lagou / liepin / zhaopin 等）的登录会话。

    - 站点适配器（Collector 子类）通过 :meth:`register` 注册到 :attr:`COLLECTORS`；
    - 通过 :meth:`get` 获取全局单例；
    - 所有方法均为 async，内部异常一律捕获并转为友好中文日志，不向上抛原始异常。
    """

    #: 站点注册表：site 标识 -> Collector 类（站点适配器完成后由外部 register 注册）
    COLLECTORS: dict[str, type] = {}

    #: 单例缓存：类名 -> AuthService 实例
    _instances: dict[str, "AuthService"] = {}

    def __init__(self) -> None:
        #: GUI 状态回调：cb(site: str, state: str)，可为 None
        self._status_cb: Optional[Callable[[str, str], None]] = None
        #: Collector 实例缓存：site -> Collector 实例
        self._collector_cache: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # 注册与单例
    # ------------------------------------------------------------------ #

    @classmethod
    def register(cls, site: str, collector_cls: type) -> None:
        """注册站点适配器：site 标识 -> Collector 类。

        参数:
            site: 站点标识，如 "zhipin"、"51job"。
            collector_cls: 实现 Collector 抽象接口的适配器类。
        """
        cls.COLLECTORS[site] = collector_cls
        logger.info("已注册站点适配器: %s -> %s", site, getattr(collector_cls, "__name__", str(collector_cls)))

    @classmethod
    def get(cls) -> "AuthService":
        """获取全局单例（按类名缓存，子类各自独立）。"""
        key = cls.__name__
        if key not in cls._instances:
            cls._instances[key] = cls()
        return cls._instances[key]

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    def _get_collector(self, site: str) -> Any:
        """从注册表取 Collector 类并实例化缓存（每个站点只实例化一次）。

        未注册站点抛出 RuntimeError(f"站点 {site} 适配器未实现")。
        """
        cached = self._collector_cache.get(site)
        if cached is not None:
            return cached
        collector_cls = self.COLLECTORS.get(site)
        if collector_cls is None:
            raise RuntimeError(f"站点 {site} 适配器未实现")
        collector = collector_cls()
        self._collector_cache[site] = collector
        logger.debug("已实例化站点适配器: %s -> %s", site, getattr(collector_cls, "__name__", str(collector_cls)))
        return collector

    def _notify(self, site: str, state: str) -> None:
        """内部：通知 GUI 状态变化（若已设置状态回调），回调自身异常不外抛。"""
        if state not in VALID_STATES:
            logger.warning("忽略非法状态通知: site=%s state=%s", site, state)
            return
        cb = self._status_cb
        if cb is None:
            return
        try:
            cb(site, state)
        except Exception:
            logger.warning("状态回调执行失败: site=%s state=%s", site, state, exc_info=True)

    async def _safe_close_pages(self, site: str, manager: Any) -> None:
        """内部：关闭该站点所有页面（容错，manager 可为 None 或未启动）。"""
        if manager is None:
            return
        try:
            await manager.close_all_pages()
        except Exception as exc:
            logger.warning("关闭站点 %s 页面失败: %s", site, exc)

    # ------------------------------------------------------------------ #
    # 对外接口
    # ------------------------------------------------------------------ #

    def set_status_callback(self, cb: Optional[Callable[[str, str], None]]) -> None:
        """设置状态回调，GUI 用它更新状态灯。

        回调签名: cb(site: str, state: str)
        state 取值: 'ok' | 'expired' | 'unknown' | 'logging_in' | 'logged_in' | 'failed'
        传 None 可取消回调。
        """
        self._status_cb = cb
        if cb is None:
            logger.debug("已取消状态回调")
        else:
            logger.debug("已设置状态回调")

    async def check_status(self, site: str) -> str:
        """校验站点登录状态。

        流程: 启动该站浏览器 → new_page → goto_login 进入登录页/首页
              → is_logged_in 判断是否被重定向 → 返回状态；结束后关闭所有页面。

        返回:
            'ok'       已登录
            'expired'  登录已失效（被重定向到登录页）
            'unknown'  无法确认（依赖模块未就绪 / 适配器未注册 / 校验异常）
        """
        self._notify(site, "logging_in")
        try:
            from app.browser.driver import BrowserManager
        except ImportError as exc:
            logger.warning("依赖模块 app.browser.driver 未就绪，站点 %s 状态返回 unknown: %s", site, exc)
            self._notify(site, "unknown")
            return "unknown"

        try:
            collector = self._get_collector(site)
        except RuntimeError as exc:
            logger.warning("站点 %s 适配器未实现，无法校验登录状态: %s", site, exc)
            self._notify(site, "unknown")
            return "unknown"

        manager: Any = None
        try:
            # 状态检查无需可见窗口：用无头模式，避免启动时逐个弹出浏览器打扰用户
            manager = BrowserManager.get(site, headless=True)
            await manager.start()
            page = await manager.new_page()
            await collector.goto_login(page)
            # 等待页面跳转/重定向稳定后再判断登录态（等待失败不影响后续判断）
            try:
                await manager.sleep(1500)
            except Exception:
                pass
            if await collector.is_logged_in(page):
                logger.info("站点 %s 登录状态: 已登录", site)
                self._notify(site, "ok")
                return "ok"
            logger.info("站点 %s 登录状态: 已失效（被重定向到登录页）", site)
            self._notify(site, "expired")
            return "expired"
        except Exception as exc:
            logger.warning("校验站点 %s 登录状态失败，返回 unknown: %s", site, exc)
            self._notify(site, "unknown")
            return "unknown"
        finally:
            # 每次调用后关闭所有页面并停止浏览器进程，释放资源与 profile 锁
            # （否则每次状态检查都会残留一个 chrome 进程）
            await self._safe_close_pages(site, manager)
            if manager is not None:
                try:
                    await manager.stop()
                except Exception as exc:
                    logger.warning("停止站点 %s 浏览器失败: %s", site, exc)

    async def login(
        self,
        site: str,
        mode: str = "qr",
        phone: str = "",
        code: str = "",
        qr_callback: Optional[Callable] = None,
    ) -> bool:
        """执行站点登录。

        参数:
            site: 站点标识。
            mode: 'qr' 扫码登录（默认）；'sms' 短信验证码登录。
            phone: 短信登录时的手机号。
            code: 短信验证码。
            qr_callback: 扫码回调（由 GUI 提供，用于截取并展示二维码）。

        返回:
            True 登录成功；False 登录失败或发生异常（任何异常都回调 'failed' 并返回 False）。
        """
        self._notify(site, "logging_in")
        try:
            from app.browser.driver import BrowserManager
        except ImportError as exc:
            logger.warning("依赖模块 app.browser.driver 未就绪，站点 %s 无法登录: %s", site, exc)
            self._notify(site, "failed")
            return False

        try:
            collector = self._get_collector(site)
        except RuntimeError as exc:
            logger.warning("站点 %s 适配器未实现，无法登录: %s", site, exc)
            self._notify(site, "failed")
            return False

        manager: Any = None
        try:
            manager = BrowserManager.get(site)
            await manager.start()
            page = await manager.new_page()
            await collector.goto_login(page)

            if mode == "qr":
                logger.info("站点 %s 开始扫码登录", site)
                ok = await collector.login_qr(page, qr_callback)
            elif mode == "sms":
                if not phone or not code:
                    logger.warning("短信登录缺少手机号或验证码: site=%s phone=%s", site, phone)
                    self._notify(site, "failed")
                    return False
                logger.info("站点 %s 开始短信验证码登录", site)
                ok = await collector.login_sms(page, phone, code)
            else:
                logger.warning("不支持的登录方式 %r: site=%s", mode, site)
                self._notify(site, "failed")
                return False

            if ok:
                logger.info("站点 %s 登录成功（方式: %s）", site, mode)
                self._notify(site, "logged_in")
                return True
            logger.warning("站点 %s 登录未成功（方式: %s）", site, mode)
            self._notify(site, "failed")
            return False
        except Exception as exc:
            logger.warning("站点 %s 登录过程异常（方式: %s）: %s", site, mode, exc)
            self._notify(site, "failed")
            return False
        finally:
            await self._safe_close_pages(site, manager)

    async def ensure_login(self, site: str, qr_callback: Optional[Callable] = None) -> bool:
        """确保站点已登录：已登录直接返回 True，否则尝试扫码登录。

        参数:
            site: 站点标识。
            qr_callback: 扫码回调（仅在需要重新登录时使用）。

        返回:
            True 站点已登录或登录成功；False 登录失败。
        """
        status = await self.check_status(site)
        if status == "ok":
            logger.info("站点 %s 已登录，无需重新登录", site)
            return True
        logger.info("站点 %s 未登录（状态: %s），尝试扫码登录", site, status)
        return await self.login(site, mode="qr", qr_callback=qr_callback)

    async def manual_login(
        self, site: str, on_browser_open: Optional[Callable] = None, timeout_seconds: int = 900
    ) -> bool:
        """手动登录模式：打开浏览器让用户自己完成登录并关闭窗口，随后验证登录态。

        适用场景：站点自动扫码不稳定（如 Boss 直聘风控强），或用户更愿意手动操作。
        程序只负责：打开有头浏览器到登录页 → 等待用户手动登录并关闭窗口 →
        用独立会话验证登录态并回写状态灯。

        参数:
            site: 站点标识。
            on_browser_open: 浏览器打开成功后可回调（GUI 用它提示用户）。
            timeout_seconds: 等待用户完成登录的最长时间（默认 15 分钟）。

        返回:
            True 检测到已登录；False 失败/超时/未登录。
        """
        self._notify(site, "logging_in")
        try:
            from app.browser.driver import BrowserManager

            collector = self._get_collector(site)
        except Exception as exc:  # noqa: BLE001
            logger.warning("站点 %s 手动登录准备失败: %s", site, exc)
            self._notify(site, "failed")
            return False

        manager: Any = None
        try:
            # 有头模式：用户可见、可手动操作
            manager = BrowserManager.get(site)
            await manager.start()
            page = await manager.new_page()
            try:
                await collector.goto_login(page)
            except Exception as exc:  # noqa: BLE001
                logger.warning("站点 %s 打开登录页失败，用户仍可手动导航: %s", site, exc)

            # 等待用户关闭浏览器（context close 事件）
            closed = asyncio.Event()
            if manager.context is not None:
                try:
                    manager.context.on("close", lambda: closed.set())
                except Exception as exc:  # noqa: BLE001
                    logger.debug("注册浏览器关闭事件失败（改用轮询）: %s", exc)
            if callable(on_browser_open):
                try:
                    on_browser_open()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("on_browser_open 回调异常: %s", exc)

            try:
                await asyncio.wait_for(closed.wait(), timeout=timeout_seconds)
                logger.info("站点 %s 浏览器已被用户关闭，开始验证登录态", site)
            except asyncio.TimeoutError:
                logger.warning("站点 %s 手动登录等待超时（%s 秒）", site, timeout_seconds)
                self._notify(site, "failed")
                return False

            # 浏览器已被用户关闭：不要对已关闭的 context/page 再操作（Playwright 会挂起）。
            # 先让出事件循环等关闭主流程完成，只停止 driver，再标记旧实例不可复用。
            await asyncio.sleep(0.5)
            try:
                if manager._playwright is not None:
                    await asyncio.wait_for(manager._playwright.stop(), timeout=5)
            except Exception:  # noqa: BLE001
                logger.debug("停止旧浏览器 driver 失败（忽略）: %s", manager.site)
            manager.context = None
            manager._pages = []
            manager._playwright = None
            manager._stopped = True

            # 用无头独立会话验证登录态
            status = await self.check_status(site)
            ok = status == "ok"
            logger.info("站点 %s 手动登录验证结果: %s", site, status)
            self._notify(site, "logged_in" if ok else "expired")
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning("站点 %s 手动登录异常: %s", site, exc)
            self._notify(site, "failed")
            return False
        finally:
            if manager is not None and manager.context is not None:
                try:
                    await asyncio.wait_for(manager.stop(), timeout=5)
                except Exception:  # noqa: BLE001
                    logger.debug("手动登录收尾清理失败（忽略）: %s", manager.site)

    async def logout(self, site: str) -> None:
        """退出登录：清空该站浏览器的 Cookie，登录态随即失效。

        优先使用 BrowserManager 提供的 context；若驱动未提供该方法，
        则通过 new_page 后取 page.context.clear_cookies() 实现。
        """
        try:
            from app.browser.driver import BrowserManager
        except ImportError as exc:
            logger.warning("依赖模块 app.browser.driver 未就绪，站点 %s 无法退出登录: %s", site, exc)
            return

        manager: Any = None
        try:
            manager = BrowserManager.get(site)
            await manager.start()
            page = await manager.new_page()
            # 优先取 BrowserManager 的 context，取不到则退回 page.context
            context = None
            try:
                context = getattr(manager, "context", None)
            except Exception:
                context = None
            if context is None:
                context = page.context
            await context.clear_cookies()
            logger.info("站点 %s 已退出登录（Cookie 已清除）", site)
            self._notify(site, "expired")
        except Exception as exc:
            logger.warning("站点 %s 退出登录失败: %s", site, exc)
            self._notify(site, "failed")
        finally:
            await self._safe_close_pages(site, manager)

    async def site_display_name(self, site: str) -> str:
        """返回站点中文名（来自 app.collectors.base.SITES 映射），缺省返回 site 标识。"""
        try:
            from app.collectors.base import SITES
            return SITES.get(site, site)
        except ImportError as exc:
            logger.warning("模块 app.collectors.base 未就绪，站点 %s 显示名使用原始标识: %s", site, exc)
            return site
