"""Playwright 浏览器驱动：管理每站点独立的持久化浏览器上下文。

对应设计文档 §4.1（app/browser 目录规划）、§6（基础设施层 PlaywrightDriver：
持久化浏览器上下文，每站点独立 user_data_dir）与 §7（登录模块方案 A：登录态
自动落盘到 user_data_dir，无需手动导出 Cookie）。

playwright 是异步库，本模块全部接口均为 async；GUI 侧会放到独立线程的
asyncio 事件循环里调用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.async_api import async_playwright

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

# settings 提供 BROWSER_DIR（data/browser）；导入失败时降级到项目根目录 data/browser
try:
    from app.config import settings as _settings

    BROWSER_DIR: Path = Path(_settings.BROWSER_DIR)
except Exception:  # noqa: BLE001 - app.config.settings 模块尚未就绪时降级
    BROWSER_DIR = Path(__file__).resolve().parents[2] / "data" / "browser"


# ---------------------------------------------------------------------------
# 真实浏览器（Edge）探测与反自动化配置
# ---------------------------------------------------------------------------
# 强风控站点（Boss 直聘）会识别 Playwright 自带的 chromium（带 CDP 调试通道、
# automation 痕迹），触发"页面反复刷新/无法扫码"。改用系统真实 Edge 浏览器
# （channel="msedge"）可大幅降低风控识别率。

_EDGE_CANDIDATES = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft/Edge/Application/msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Microsoft/Edge/Application/msedge.exe",
]
EDGE_EXE: str | None = next((str(p) for p in _EDGE_CANDIDATES if p.is_file()), None)

# 默认走真实 Edge 的站点（强风控站点）
SITE_USE_REAL_BROWSER: frozenset = frozenset({"zhipin"})

# 反自动化检测注入脚本：删除 webdriver 标志、伪造 chrome/languages/plugins，
# 尽量让浏览器看起来像用户手动打开的普通浏览器。
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


class BrowserManager:
    """管理单个站点的 Playwright 持久化浏览器上下文。

    每个站点一个独立的 user_data_dir（data/browser/<site>/），Cookie 与
    localStorage 由 Playwright 自动落盘，登录一次后后续启动自动恢复会话。
    """

    _instances: dict[str, "BrowserManager"] = {}

    def __init__(self, site: str, headless: bool = False, channel: str | None = None) -> None:
        """初始化浏览器管理器。

        :param site: 站点标识（如 "zhipin"），对应 data/browser/<site> 目录
        :param headless: 是否无头模式，默认 False（桌面应用需可见窗口扫码/人工操作）
        :param channel: 浏览器通道；None=Playwright chromium，"msedge"=系统真实 Edge
            （强风控站点用真实浏览器，风控识别率更低）
        """
        self.site = site
        self.headless = headless
        self.channel = channel
        self.user_data_dir = BROWSER_DIR / site
        self.context: BrowserContext | None = None
        self._playwright: Playwright | None = None
        self._pages: list[Page] = []
        self._stopped = False

    # ------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        """启动浏览器并创建持久化上下文。

        登录态（Cookie/localStorage）由 Playwright 自动落盘到 user_data_dir，
        无需手动导出 Cookie。

        反自动化检测：移除 ``--enable-automation``、注入 stealth 脚本；
        强风控站点用系统真实 Edge（channel="msedge"）而非 Playwright chromium。
        """
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        kwargs = dict(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            viewport={"width": 1360, "height": 900},
            locale="zh-CN",
            ignore_default_args=["--enable-automation"],
            args=["--disable-blink-features=AutomationControlled"],
        )
        if self.channel:
            kwargs["channel"] = self.channel
        self.context = await self._playwright.chromium.launch_persistent_context(**kwargs)
        # 注入反检测脚本（删除 webdriver、伪造 chrome/languages/plugins）
        try:
            await self.context.add_init_script(STEALTH_INIT_SCRIPT)
        except Exception as exc:  # noqa: BLE001
            logger.debug("注入反检测脚本失败（忽略）: %s", exc)
        logger.info(
            "站点 %s 浏览器上下文已启动（user_data_dir=%s, channel=%s）",
            self.site, self.user_data_dir, self.channel or "chromium",
        )

    async def stop(self) -> None:
        """停止浏览器：关闭所有页面、上下文与 Playwright 实例。

        对已被外部关闭（如手动登录时用户关闭窗口）的 context 做 is_closed 检查，
        避免对已关闭对象再次 close() 造成 Playwright 挂起。
        """
        await self.close_all_pages()
        if self.context is not None:
            try:
                # 已关闭的 context 不再重复 close（Playwright 会挂起）
                is_closed = getattr(self.context, "is_closed", None)
                if not (callable(is_closed) and is_closed()):
                    await self.context.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("关闭站点 %s 的上下文失败：%s", self.site, exc)
            self.context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("停止站点 %s 的 Playwright 失败：%s", self.site, exc)
            self._playwright = None
        self._stopped = True
        # 已停止的实例不再复用，从类级缓存移除（键 = site:headless:channel）
        _key = f"{self.site}:{self.headless}:{self.channel or 'chromium'}"
        if self._instances.get(_key) is self:
            self._instances.pop(_key, None)
        logger.info("站点 %s 浏览器已停止", self.site)

    # ------------------------------------------------------------- 页面管理

    async def new_page(self) -> Page:
        """新建一个页面并记录，便于统一关闭。"""
        if self.context is None:
            raise RuntimeError("浏览器上下文未启动，请先调用 start()")
        page = await self.context.new_page()
        self._pages.append(page)
        return page

    async def close_all_pages(self) -> None:
        """关闭所有已记录的页面（保留浏览器上下文，登录态不受影响）。"""
        for page in self._pages:
            try:
                await page.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭页面失败：%s", exc)
        self._pages.clear()

    # ------------------------------------------------------------- 便捷方法

    async def wait_for(self, page: Page, selector: str, timeout_ms: int = 30000) -> None:
        """等待页面上出现指定选择器（可见），超时抛 playwright TimeoutError。

        :param page: 目标页面
        :param selector: CSS/XPath 选择器
        :param timeout_ms: 超时毫秒数，默认 30000
        """
        await page.wait_for_selector(selector, state="visible", timeout=timeout_ms)

    async def wait_for_url_contains(self, page: Page, part: str, timeout_ms: int = 30000) -> None:
        """等待页面 URL 包含指定片段（用于登录成功/跳转判定）。

        :param page: 目标页面
        :param part: URL 期望包含的片段，如 "login"、"index"
        :param timeout_ms: 超时毫秒数，默认 30000
        """
        await page.wait_for_url(f"**{part}**", timeout=timeout_ms)

    async def sleep(self, ms: int) -> None:
        """异步等待指定毫秒数（拟人化随机等待用，不阻塞事件循环）。"""
        await asyncio.sleep(ms / 1000.0)

    async def screenshot_element(self, page: Page, selector: str, path: str | Path) -> None:
        """等待元素可见后截图保存到指定路径（供登录二维码展示）。

        :param page: 目标页面
        :param selector: 二维码图片元素选择器
        :param path: 截图保存路径（PNG）
        """
        locator = page.locator(selector)
        await locator.wait_for(state="visible", timeout=30000)
        await locator.screenshot(path=str(path))
        logger.info("元素截图已保存：%s", path)

    # ------------------------------------------------------------- 会话备份

    async def save_cookies_backup(self, path: str | Path) -> None:
        """把当前上下文全部 Cookie 导出为 JSON 备份（对应文档 §7.1 cookies.json 备份）。

        :param path: 备份文件路径，如 data/browser/cookies.json
        """
        if self.context is None:
            raise RuntimeError("浏览器上下文未启动，请先调用 start()")
        cookies = await self.context.cookies()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Cookie 备份已保存：%s（%d 条）", target, len(cookies))

    # ------------------------------------------------------------- 类级缓存

    @classmethod
    def get(cls, site: str, headless: bool = False, channel: str | None = None) -> "BrowserManager":
        """按站点 + 模式获取（并复用）BrowserManager 实例。

        同一 (site, headless, channel) 组合复用同一实例与同一持久化上下文；
        已 stop 的实例会重建。headless 用于登录态检查等无需可见窗口的场景，
        与有头模式（扫码/人工操作）使用不同实例，避免互相干扰。

        :param site: 站点标识（如 "zhipin"）
        :param headless: 是否无头模式（默认 False：桌面应用需可见窗口扫码）
        :param channel: 浏览器通道；None 时对强风控站点（SITE_USE_REAL_BROWSER）
            且系统存在 Edge 时自动使用 "msedge"
        """
        if channel is None and site in SITE_USE_REAL_BROWSER and EDGE_EXE:
            channel = "msedge"
        key = f"{site}:{headless}:{channel or 'chromium'}"
        instance = cls._instances.get(key)
        if instance is None or instance._stopped:
            instance = cls(site, headless=headless, channel=channel)
            cls._instances[key] = instance
        return instance
