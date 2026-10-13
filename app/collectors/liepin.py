# -*- coding: utf-8 -*-
"""
猎聘（liepin）站点采集器。

选择器基于 liepin 公开页面结构，改版时优先调整此处 —— 所有候选选择器
集中在 :class:`LiepinCollector` 的类属性选择器列表里统一修改，避免散落在各方法中。

依赖约定：
- 运行期仅使用标准库（re / logging / time / datetime / pathlib / urllib）；
- playwright 仅用于类型注解（TYPE_CHECKING 下导入），运行期不导入；
- app.collectors.base 采用延迟导入：基类尚未就绪时本模块仍可独立加载，
  依赖基类的方法返回安全默认值并记录 error 日志。

异步约定（重要）：
- Playwright 的 locator 方法（count / inner_text / text_content / get_attribute /
  is_visible 等）全部是异步的，因此凡是操作 locator 的辅助函数（_match / _first /
  _label_segments 等）都必须定义为 async def，并在内部 await；
- 所有调用点必须加 await，严禁在同步函数中调用 playwright 异步方法。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin

if TYPE_CHECKING:  # pragma: no cover
    from app.collectors.base import Collector, RawJob
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

# 运行时产物（如二维码截图）保存目录：项目根目录下的 data/
_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# 基类可能在并行开发中尚未创建：此处容错导入仅用于类继承；
# 方法内部通过 _load_base() 再次延迟导入以获取 RawJob / normalize_salary。
try:  # pragma: no cover
    from app.collectors.base import Collector as _BaseCollector
except Exception:  # noqa: BLE001 - base 未就绪时退化为 object，保证模块可独立加载
    _BaseCollector = object


# =====================================================================
# 通用辅助
# =====================================================================


async def _match(root, *selectors):
    """依次尝试选择器，返回第一个能匹配元素的完整 Locator（可能匹配多个）；无匹配返回 None。

    root 可以是 Page 或 Locator；``text=xxx`` 前缀走 get_by_text(exact=False) 兜底。
    注意：locator.count() 是异步方法，本函数必须 async 并 await，否则 coroutine
    永不被执行、选择器永远匹配不到（已复现的坑）。
    """
    for sel in selectors:
        try:
            if sel.startswith("text="):
                loc = root.get_by_text(sel[len("text="):], exact=False)
            else:
                loc = root.locator(sel)
            if await loc.count() > 0:
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


async def _first(root, *selectors):
    """依次尝试选择器，返回第一个非空 Locator 的首个元素；全部失败返回 None。"""
    loc = await _match(root, *selectors)
    return loc.first if loc is not None else None


def _css_join(selectors) -> str:
    """把一组选择器拼成 CSS 逗号列表（过滤 text= 引擎项），供 wait_for_selector 使用。

    纯字符串操作，不接触 locator，保持同步即可。
    """
    return ", ".join(s for s in selectors if not s.startswith("text="))


def _fallback_salary(text: str) -> tuple[int | None, int | None]:
    """本地兜底薪资解析（base.normalize_salary 不可用时），单位为千元/月。

    纯字符串/正则操作，不接触 locator，保持同步即可。
    """
    if not text:
        return None, None
    compact = text.replace(" ", "").replace("k", "千").replace("K", "千")
    m = re.search(r"([\d.]+)\s*[-~至到]\s*([\d.]+)\s*(千|万)?", compact)
    if not m:
        return None, None
    lo, hi = float(m.group(1)), float(m.group(2))
    if m.group(3) == "万":
        lo, hi = lo * 10, hi * 10
    return int(round(lo)), int(round(hi))


# 常用城市 -> 猎聘城市编码（猎聘 PC 搜索 URL 用数字编码；未知城市返回 ''，即不传 dqs 参数）
CITY_CODES: dict[str, str] = {
    "北京": "010",
    "上海": "020",
    "广州": "030",
    "深圳": "040",
    "杭州": "070",
    "成都": "080",
    "南京": "060",
    "武汉": "090",
    "西安": "110",
    "苏州": "150",
    "天津": "050",
    "重庆": "100",
}


def _city_code(city: str | None) -> str:
    """返回城市对应的猎聘编码；未知城市返回 ''（调用方不传 dqs 参数，表示不限城市）。

    纯字典操作，不接触 locator，保持同步即可。
    """
    if not city:
        return ""
    city = city.strip()
    code = CITY_CODES.get(city)
    if code:
        return code
    for suffix in ("市", "省"):
        if city.endswith(suffix) and len(city) > len(suffix):
            code = CITY_CODES.get(city[: -len(suffix)])
            if code:
                return code
    return ""


# =====================================================================
# 采集器主体
# =====================================================================
class LiepinCollector(_BaseCollector):
    """猎聘（liepin）站点采集器，实现 Collector 抽象接口。"""

    source = "liepin"

    # =================================================================
    # 选择器候选列表（类属性）—— 站点改版时优先在此调整
    # 基于 www.liepin.com 公开页面结构（SPA 版本）
    # =================================================================

    # ---- 登录页（www.liepin.com/login）----
    SEL_LOGIN_QR_SWITCH = [  # 切换"扫码登录"入口（文本 / .qrcode 类）
        "text=扫码登录",
        ".qrcode",
        "text=二维码登录",
        ".qrcode-login",
    ]
    SEL_QR_IMAGE = [  # 二维码图片 / 画布
        ".qrcode",
        ".qrcode img",
        ".qr-code img",
        "img[src*='qrcode']",
        "img[src*='qr']",
        "canvas",
    ]
    SEL_SMS_TAB = [  # 切换"验证码登录 / 手机号登录"标签
        "text=验证码登录",
        "text=手机号登录",
        ".sms-login",
        ".code-login",
    ]
    SEL_PHONE_INPUT = [
        "input[placeholder*='手机']",
        "input[placeholder*='手机号']",
        "input[name='phone']",
        "input[name='mobile']",
    ]
    SEL_GET_CODE_BTN = [
        "text=获取验证码",
        "button:has-text('获取验证码')",
        ".get-code",
        "#sendCode",
    ]
    SEL_CODE_INPUT = [
        "input[placeholder*='验证码']",
        "input[name='code']",
        "input[name='verifyCode']",
        "input[placeholder*='短信']",
    ]
    SEL_LOGIN_BTN = [
        "button[type='submit']",
        ".login-btn",
        ".btn-login",
        "text=登录",
        "button:has-text('登 录')",
    ]
    SEL_USER_MENU = [  # 已登录用户菜单
        # 注意：只用结构化 class 选择器，勿加 text= 文本项——
        # 未登录页面也常有"个人中心/退出"等文字链接，会造成登录态误判
        ".user-info",
        ".user-account",
        ".avatar",
        ".username",
        ".user-name",
        ".header-user",
        ".user-menu",
    ]
    SEL_LOGIN_FORM_HINT = [  # 页面出现登录表单 = 未登录
        ".login-form",
        ".login-panel",
        "input[type='password']",
        "input[placeholder*='密码']",
        "input[placeholder*='账号']",
    ]

    # ---- 搜索列表页（www.liepin.com/zhaopin，SPA）----
    SEL_LIST_ITEM = [
        ".job-card",
        ".job-list-item",
        ".job-detail-box",
        ".job-card-pc-container",
    ]
    SEL_JOB_TITLE = [
        ".job-card-pc-container__job-title",
        ".job-title",
        "h3 a",
        "h3",
        "a[href*='job']",
    ]
    SEL_JOB_LINK = [
        "a[href*='/job/']",
        "a[href*='.shtml']",
        "a[href*='/a/']",
        "a[href*='job']",
    ]
    SEL_COMPANY = [
        ".company-name",
        ".job-card-pc-container__company-name",
        ".company",
        ".com_name",
    ]
    SEL_SALARY = [
        ".job-salary",
        ".job-card-pc-container__salary",
        ".salary",
    ]
    SEL_LABELS = [  # 地区/条件标签容器（"北京-海淀区 5-10年 大专 节日福利"）
        ".job-labels",
        ".job-card-pc-container__labels",
        ".labels",
    ]
    SEL_LABEL_SPANS = [
        ".job-labels span",
        ".job-card-pc-container__labels span",
        ".labels span",
        ".tag-item",
        "span.tag",
    ]
    SEL_TIME = [
        ".job-deadline",
        ".time",
        ".job-card-pc-container__time",
        ".deadline",
    ]
    SEL_NEXT_PAGE = [  # 翻页：.pager 内"下一页"按钮
        ".pager .next",
        ".pager .pager-next",
        ".pager a:has-text('下一页')",
        "text=下一页",
        "a:has-text('下一页')",
    ]

    # ---- 岗位详情页（www.liepin.com/job/{id}.shtml）----
    SEL_DETAIL_CONTAINER = [
        ".job-intro-container",
        ".job-description",
        "#job-detail",
        ".job-detail-box",
    ]
    SEL_DETAIL_DESC = [
        ".job-intro-container",
        ".job-description",
        "#job-detail",
        ".job-detail-content",
        ".job-desc",
    ]
    SEL_DETAIL_TITLE = [
        "h1",
        ".job-title",
        ".job-name",
        ".name",
    ]
    SEL_DETAIL_COMPANY = [
        ".company-name",
        ".job-card-pc-container__company-name",
        ".company",
        ".com_name",
        "a[href*='company']",
    ]
    SEL_DETAIL_SALARY = [
        ".job-salary",
        ".job-card-pc-container__salary",
        ".salary-item",
        ".salary",
    ]

    # ---- 投递 ----
    SEL_APPLY_BTN = [  # 详情页"投递 / 立即投递"按钮（.btn 含文本"投递"）
        "button.btn:has-text('投递')",
        ".btn:has-text('投递')",
        "button:has-text('立即投递')",
        "text=立即投递",
        "button:has-text('投递')",
        "text=投递",
    ]
    SEL_APPLY_DIALOG = [
        ".resume-select",
        ".apply-dialog",
        ".resume-dialog",
        ".el-dialog",
        ".dialog-popup",
    ]
    SEL_RESUME_LIST = [
        ".resume-list",
        ".resume-select",
        ".apply-dialog .resume-list",
    ]
    SEL_RESUME_OPTION = [
        ".resume-list li",
        ".resume-list .item",
        ".resume-list .resume-name",
        ".resume-item",
        "input[type='radio']",
    ]
    SEL_SUBMIT_BTN = [
        "button:has-text('确认投递')",
        "text=确认投递",
        ".btn-primary",
        "button:has-text('投递')",
        "text=投递",
    ]
    SEL_APPLY_SUCCESS = [
        "text=投递成功",
        "text=申请成功",
        "text=已投递",
        ".apply-success",
        ".success-box",
    ]

    # ---- 其他参数 ----
    QR_TIMEOUT_SECONDS = 180
    LOGIN_POLL_INTERVAL_MS = 2000
    PAGE_WAIT_MS = 1500
    LIST_WAIT_TIMEOUT_MS = 30000  # SPA 列表渲染最长等待
    DETAIL_WAIT_TIMEOUT_MS = 15000  # 详情页容器最长等待

    # ------------------------------------------------------------------
    # base 延迟加载与 RawJob 构造（纯同步，不接触 locator）
    # ------------------------------------------------------------------
    def _load_base(self):
        """延迟导入 app.collectors.base；尚未就绪时返回 None。"""
        try:
            import app.collectors.base as _base

            return _base
        except Exception as exc:  # noqa: BLE001
            logger.error("app.collectors.base 尚未就绪，无法按契约构造 RawJob: %s", exc)
            return None

    def _make_raw_job(self, **fields):
        """按契约构造 RawJob：缺失字段给安全默认值；base 不可用返回 None。"""
        base = self._load_base()
        if base is None or not hasattr(base, "RawJob"):
            return None
        data = {
            "source": self.source,
            "job_id": "",
            "title": "",
            "company": "",
            "company_size": "",
            "industry": "",
            "salary_min": None,
            "salary_max": None,
            "salary_raw": "",
            "city": "",
            "district": "",
            "experience": "",
            "education": "",
            "tags": [],
            "description": "",
            "publish_date": "",
            "job_url": "",
            "crawl_time": datetime.now().isoformat(timespec="seconds"),
            "status": "active",
            "is_favorite": 0,
            "match_score": None,
        }
        for key, val in fields.items():
            if key in data and val is not None:
                data[key] = val
        if isinstance(data["tags"], str):  # 容错：允许传入 JSON 字符串形式的标签
            try:
                parsed = json.loads(data["tags"])
                data["tags"] = parsed if isinstance(parsed, list) else []
            except Exception:  # noqa: BLE001
                data["tags"] = []
        try:
            return base.RawJob(**data)
        except Exception as exc:  # noqa: BLE001
            logger.error("RawJob 构造失败: %s", exc)
            return None

    def _extract_job_id(self, url: str) -> str:
        """从岗位链接中提取 job_id（/job/{数字}.shtml 或 /a/{id}）；取不到时退回末段数字。

        纯正则操作，不接触 locator，保持同步即可。
        """
        if not url:
            return ""
        m = re.search(r"/job/(\d+)", url)
        if m:
            return m.group(1)
        m = re.search(r"/a/(\d+)", url)
        if m:
            return m.group(1)
        tail = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0].split("#")[0]
        digits = re.findall(r"\d+", tail)
        if digits:
            return digits[-1]
        return ""

    @staticmethod
    def _split_area(area_text: str) -> tuple[str, str]:
        """拆分形如 '北京-海淀区' 或 '北京' 的地区文本 -> (city, district)。

        仅当首段命中已知城市（或带'市'后缀、或带区级后缀）时才认定为地区，
        避免把 '5-10年' 之类的条件标签误拆为城市。纯正则操作，保持同步即可。
        """
        if not area_text:
            return "", ""
        text = area_text.strip()
        m = re.match(
            r"^([\u4e00-\u9fff]{2,4}?)(?:[-–—]([\u4e00-\u9fff]{2,8}?(?:区|县|市)))?$",
            text,
        )
        if not m:
            return "", ""
        city = m.group(1).rstrip("市")
        district = m.group(2) or ""
        if city in CITY_CODES or city + "市" in CITY_CODES or district:
            return city, district
        return "", ""

    @staticmethod
    def _classify_tag(text: str) -> str:
        """把标签文本归类：experience / education / tag。纯字符串操作，保持同步即可。"""
        if "经验" in text or re.search(r"\d+\s*年", text) or re.search(
            r"\d+\s*[-~—至]\s*\d+\s*年", text
        ):
            return "experience"
        if any(k in text for k in ("学历", "大专", "本科", "硕士", "博士", "中专", "高中")):
            return "education"
        return "tag"

    async def _settle(self, page: Page) -> None:
        """等待页面加载平稳（networkidle 尽力而为 + 固定停顿）。"""
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(800)

    async def _label_segments(self, item) -> list[str]:
        """从标签容器提取分段文本（"北京-海淀区 5-10年 大专" → 分段列表）。

        优先读容器整体文本按空白切分；容器为空时退回逐 span 收集。
        操作 locator，必须 async。
        """
        segments: list[str] = []
        labels_el = await _first(item, *self.SEL_LABELS)
        if labels_el is not None:
            try:
                raw = (await labels_el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                raw = ""
            if raw:
                segments = [
                    s.strip() for s in re.split(r"[\s|｜/]+", raw) if s.strip()
                ]
        if not segments:
            spans_loc = await _match(item, *self.SEL_LABEL_SPANS)
            spans = await spans_loc.all() if spans_loc is not None else []
            for sp in spans:
                try:
                    t = (await sp.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if t:
                    segments.append(t)
        return segments

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    async def goto_login(self, page: Page) -> None:
        """打开猎聘登录页。"""
        try:
            await page.goto(
                "https://www.liepin.com/login",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self._settle(page)
        except Exception as exc:  # noqa: BLE001
            logger.error("goto_login 失败: %s", exc)

    async def is_logged_in(self, page: Page) -> bool:
        """判断登录态：访问首页，被重定向到登录页或出现登录表单则未登录；异常返回 False。"""
        try:
            # 已在非登录业务页时，直接检查 DOM，避免反复跳转
            current = (page.url or "").lower()
            if current.startswith("http") and "login" not in current:
                if await _first(page, *self.SEL_USER_MENU) is not None:
                    return True
                if await _first(page, *self.SEL_LOGIN_FORM_HINT) is not None:
                    return False
                if await self._cookie_logged_in(page):
                    return True
                # 保守判定：无已登录证据即视为未登录（避免误报扫码成功）
                return False
            # 需要导航校验：访问首页，看是否被重定向回登录页
            await page.goto(
                "https://www.liepin.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self._settle(page)
            url = (page.url or "").lower()
            if "login" in url:
                return False
            if await _first(page, *self.SEL_USER_MENU) is not None:
                return True
            if await _first(page, *self.SEL_LOGIN_FORM_HINT) is not None:
                return False
            if await self._cookie_logged_in(page):
                return True
            # 保守判定：无已登录证据即视为未登录
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("is_logged_in 异常: %s", exc)
            return False

    async def login_qr(self, page: Page, qr_callback) -> bool:
        """切换到扫码登录，截图二维码并回调 qr_callback(qr_path)，随后轮询登录结果。"""
        try:
            current = (page.url or "").lower()
            if "login" not in current:
                await self.goto_login(page)
            # 尝试切换到"扫码登录"入口（文本 / .qrcode 类）
            switch = await _first(page, *self.SEL_LOGIN_QR_SWITCH)
            if switch is not None:
                try:
                    await switch.click(timeout=5000)
                    await page.wait_for_timeout(800)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("点击扫码入口失败，忽略: %s", exc)
            # 等待二维码元素出现
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_QR_IMAGE), timeout=5000
                )
            except Exception:  # noqa: BLE001
                pass
            qr = await _first(page, *self.SEL_QR_IMAGE)
            qr_missing = qr is None
            if qr is not None:
                try:
                    _DATA_DIR.mkdir(parents=True, exist_ok=True)
                    qr_path = str(
                        _DATA_DIR
                        / f"qr_liepin_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    )
                    await qr.screenshot(path=qr_path)
                    if callable(qr_callback):
                        qr_callback(qr_path)
                    logger.info("二维码已截图: %s", qr_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("二维码截图/回调失败: %s", exc)
            else:
                # 二维码元素未找到：等待 3 秒后再找一次，仍没有则缩短轮询上限
                logger.warning("未找到二维码元素，等待 3 秒后重试查找")
                await page.wait_for_timeout(3000)
                qr = await _first(page, *self.SEL_QR_IMAGE)
                if qr is None:
                    qr_missing = True
                    logger.error("仍未找到二维码元素（站点可能改版），将缩短登录等待时间")
                else:
                    qr_missing = False
                    try:
                        _DATA_DIR.mkdir(parents=True, exist_ok=True)
                        qr_path = str(
                            _DATA_DIR
                            / f"qr_liepin_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                        )
                        await qr.screenshot(path=qr_path)
                        if callable(qr_callback):
                            qr_callback(qr_path)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("二维码截图/回调失败: %s", exc)
            # 轮询登录状态（二维码缺失时缩短等待，避免用户空等）
            deadline = time.monotonic() + (
                self.QR_TIMEOUT_SECONDS if not qr_missing else 30
            )
            while time.monotonic() < deadline:
                await page.wait_for_timeout(self.LOGIN_POLL_INTERVAL_MS)
                try:
                    if await self.is_logged_in(page):
                        return True
                except Exception:  # noqa: BLE001
                    continue
            logger.warning("扫码登录超时（%s 秒）", self.QR_TIMEOUT_SECONDS)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("login_qr 异常: %s", exc)
            return False

    async def login_sms(self, page: Page, phone: str, code: str) -> bool:
        """短信登录：切"验证码登录 / 手机号登录"标签 -> 填手机号 -> 获取验证码 -> 填验证码 -> 登录 -> 轮询结果。"""
        try:
            current = (page.url or "").lower()
            if "login" not in current:
                await self.goto_login(page)
            tab = await _first(page, *self.SEL_SMS_TAB)
            if tab is not None:
                try:
                    await tab.click(timeout=5000)
                    await page.wait_for_timeout(600)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("切换验证码登录标签失败: %s", exc)
            phone_input = await _first(page, *self.SEL_PHONE_INPUT)
            if phone_input is None:
                logger.error("未找到手机号输入框，短信登录失败")
                return False
            await phone_input.fill(str(phone))
            get_code = await _first(page, *self.SEL_GET_CODE_BTN)
            if get_code is not None:
                try:
                    await get_code.click(timeout=5000)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("点击'获取验证码'失败，继续尝试: %s", exc)
            code_input = await _first(page, *self.SEL_CODE_INPUT)
            if code_input is None:
                logger.error("未找到验证码输入框，短信登录失败")
                return False
            await code_input.fill(str(code))
            login_btn = await _first(page, *self.SEL_LOGIN_BTN)
            if login_btn is None:
                logger.error("未找到登录按钮，短信登录失败")
                return False
            await login_btn.click(timeout=5000)
            for _ in range(30):
                await page.wait_for_timeout(self.LOGIN_POLL_INTERVAL_MS)
                try:
                    if await self.is_logged_in(page):
                        return True
                except Exception:  # noqa: BLE001
                    continue
            logger.warning("短信登录提交后未检测到登录成功")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("login_sms 异常: %s", exc)
            return False

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------
    async def search_jobs(
        self,
        page: Page,
        keyword: str,
        city: str | None = None,
        max_pages: int = 3,
    ) -> list[RawJob]:
        """按关键词+城市搜索岗位并翻页采集，返回 RawJob 列表；异常/未登录返回 []。"""
        try:
            base = self._load_base()
            if base is None:
                logger.error("app.collectors.base 未就绪，search_jobs 返回空列表")
                return []
            code = _city_code(city)
            if code:
                url = (
                    f"https://www.liepin.com/zhaopin/?key={quote(keyword)}"
                    f"&dqs={code}"
                )
            else:
                url = f"https://www.liepin.com/zhaopin/?key={quote(keyword)}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self._settle(page)
            if "login" in (page.url or "").lower():
                logger.error("搜索页被重定向到登录页，请先登录后再采集")
                return []
            results: list = []
            seen: set[str] = set()
            page_no = 0
            for page_no in range(1, max_pages + 1):
                try:
                    # SPA 列表渲染：等待候选选择器集合中任一出现，最多 30 秒
                    await page.wait_for_selector(
                        _css_join(self.SEL_LIST_ITEM),
                        timeout=self.LIST_WAIT_TIMEOUT_MS,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "第 %s 页未等到列表项（可能已改版或无结果），尝试直接解析",
                        page_no,
                    )
                items_loc = await _match(page, *self.SEL_LIST_ITEM)
                if items_loc is None:
                    logger.info("第 %s 页未找到岗位列表项，停止翻页", page_no)
                    break
                items = await items_loc.all()
                if not items:
                    break
                for item in items:
                    try:
                        job = await self._parse_list_item(page, item, base, city)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("解析列表项失败: %s", exc)
                        continue
                    if job is None or not job.job_id:
                        continue
                    if job.job_id in seen:
                        continue
                    seen.add(job.job_id)
                    results.append(job)
                if page_no >= max_pages:
                    break
                if not await self._goto_next_page(page):
                    break
            logger.info(
                "search_jobs 完成 keyword=%s city=%s pages=%s 条数=%s",
                keyword,
                city,
                page_no,
                len(results),
            )
            return results
        except Exception as exc:  # noqa: BLE001
            logger.error("search_jobs 异常: %s", exc)
            return []

    async def _parse_list_item(self, page: Page, item, base, city: str | None):
        """解析搜索列表中的单条岗位，返回 RawJob 或 None。"""
        # 岗位名
        title = ""
        title_el = await _first(item, *self.SEL_JOB_TITLE)
        if title_el is not None:
            try:
                title = (await title_el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                title = ""
            if not title:
                try:
                    title = (await title_el.get_attribute("title") or "").strip()
                except Exception:  # noqa: BLE001
                    pass
        # 岗位链接与 job_id
        job_url = ""
        if title_el is not None:
            try:
                job_url = (await title_el.get_attribute("href") or "").strip()
            except Exception:  # noqa: BLE001
                pass
        if not job_url:
            link_el = await _first(item, *self.SEL_JOB_LINK)
            if link_el is not None:
                try:
                    job_url = (await link_el.get_attribute("href") or "").strip()
                except Exception:  # noqa: BLE001
                    pass
        if job_url and not job_url.startswith("http"):
            job_url = urljoin("https://www.liepin.com/", job_url)
        job_id = self._extract_job_id(job_url)
        # 公司
        company = ""
        c_el = await _first(item, *self.SEL_COMPANY)
        if c_el is not None:
            try:
                company = (await c_el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                company = ""
        # 薪资
        salary_raw = ""
        s_el = await _first(item, *self.SEL_SALARY)
        if s_el is not None:
            try:
                salary_raw = (await s_el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                salary_raw = ""
        salary_min, salary_max = None, None
        if salary_raw:
            try:
                salary_min, salary_max = base.normalize_salary(salary_raw)
            except Exception as exc:  # noqa: BLE001
                salary_min, salary_max = _fallback_salary(salary_raw)
                logger.debug("列表薪资解析失败 %r，使用本地兜底: %s", salary_raw, exc)
        # 城市/区 + 经验/学历/标签：全部来自标签容器文本，按关键词归类
        parsed_city, district = "", ""
        experience, education = "", ""
        tags: list[str] = []
        segments = await self._label_segments(item)
        for seg in segments:
            c_part, d_part = self._split_area(seg)
            if c_part or d_part:
                if not parsed_city:
                    parsed_city, district = c_part, d_part
                continue
            kind = self._classify_tag(seg)
            if kind == "experience":
                if not experience:
                    experience = seg
            elif kind == "education":
                if not education:
                    education = seg
            else:
                tags.append(seg)
        if not parsed_city and city:
            parsed_city = city.strip()
        # 发布时间（相对时间原样保存）
        publish_date = ""
        t_el = await _first(item, *self.SEL_TIME)
        if t_el is not None:
            try:
                publish_date = (await t_el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                publish_date = ""
        return self._make_raw_job(
            job_id=job_id,
            title=title,
            company=company,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_raw=salary_raw,
            city=parsed_city,
            district=district,
            experience=experience,
            education=education,
            tags=tags,
            publish_date=publish_date,
            job_url=job_url,
        )

    async def _goto_next_page(self, page: Page) -> bool:
        """点击"下一页"并等待，成功返回 True。"""
        try:
            btn = await _first(page, *self.SEL_NEXT_PAGE)
            if btn is None:
                return False
            cls = await btn.get_attribute("class") or ""
            disabled = await btn.get_attribute("disabled")
            if disabled is not None or "disabled" in cls:
                return False
            await btn.click(timeout=5000)
            await page.wait_for_timeout(self.PAGE_WAIT_MS)  # 页间停顿防反爬
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("翻页失败: %s", exc)
            return False

    async def parse_job(self, page: Page, job_url: str):
        """打开岗位详情页解析完整信息并回填 title/company/salary/description；失败返回 None。"""
        try:
            base = self._load_base()
            if base is None:
                logger.error("app.collectors.base 未就绪，parse_job 返回 None")
                return None
            target = (job_url or "").strip()
            if not target:
                logger.warning("parse_job 缺少有效详情页链接: %r", job_url)
                return None
            if not target.startswith("http"):
                target = urljoin("https://www.liepin.com/", target)
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            await self._settle(page)
            # 登录墙：被重定向到登录页直接判失败
            current = (page.url or "").lower()
            if "login" in current:
                logger.warning("详情页被重定向到登录页，解析失败: %s", target)
                return None
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_DETAIL_CONTAINER),
                    timeout=self.DETAIL_WAIT_TIMEOUT_MS,
                )
            except Exception:  # noqa: BLE001
                logger.warning("详情页容器未等到: %s", target)
            # 岗位描述
            description = ""
            desc_el = await _first(page, *self.SEL_DETAIL_DESC)
            if desc_el is not None:
                try:
                    description = (await desc_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    description = ""
            # 标题（详情更全则覆盖列表值）
            title = ""
            t_el = await _first(page, *self.SEL_DETAIL_TITLE)
            if t_el is not None:
                try:
                    title = (await t_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    title = ""
            # 公司
            company = ""
            c_el = await _first(page, *self.SEL_DETAIL_COMPANY)
            if c_el is not None:
                try:
                    company = (await c_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    company = ""
            # 薪资
            salary_raw = ""
            s_el = await _first(page, *self.SEL_DETAIL_SALARY)
            if s_el is not None:
                try:
                    salary_raw = (await s_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    salary_raw = ""
            salary_min, salary_max = None, None
            if salary_raw:
                try:
                    salary_min, salary_max = base.normalize_salary(salary_raw)
                except Exception as exc:  # noqa: BLE001
                    salary_min, salary_max = _fallback_salary(salary_raw)
                    logger.debug(
                        "详情页薪资解析失败 %r，使用本地兜底: %s", salary_raw, exc
                    )
            # 容器未出现且无任何内容 → 判为解析失败（反爬/登录墙）
            if not description and not title:
                logger.warning("详情页未解析到有效内容，判定失败: %s", target)
                return None
            job_id = self._extract_job_id(target)
            return self._make_raw_job(
                job_id=job_id,
                title=title,
                company=company,
                salary_min=salary_min,
                salary_max=salary_max,
                salary_raw=salary_raw,
                description=description,
                job_url=target,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("parse_job 异常: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 投递
    # ------------------------------------------------------------------
    async def open_apply(self, page: Page, job) -> None:
        """打开岗位投递弹窗（只打开，不提交）。"""
        try:
            job_url = getattr(job, "job_url", "") or ""
            if not job_url:
                logger.warning("job 缺少 job_url，open_apply 跳过")
                return
            target = job_url
            if not target.startswith("http"):
                target = urljoin("https://www.liepin.com/", target)
            await page.goto(target, wait_until="domcontentloaded", timeout=30000)
            await self._settle(page)
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_APPLY_BTN),
                    timeout=self.DETAIL_WAIT_TIMEOUT_MS,
                )
            except Exception:  # noqa: BLE001
                pass
            btn = await _first(page, *self.SEL_APPLY_BTN)
            if btn is None:
                logger.warning("未找到'投递'按钮: %s", target)
                return
            await btn.click(timeout=5000)
            await page.wait_for_timeout(1000)
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_APPLY_DIALOG), timeout=5000
                )
            except Exception:  # noqa: BLE001
                logger.debug("未检测到投递弹窗（可能已直接进入选择简历界面）")
        except Exception as exc:  # noqa: BLE001
            logger.error("open_apply 异常: %s", exc)

    async def submit_apply(self, page: Page, resume_name: str) -> bool:
        """在投递弹窗中选择简历版本并点击最终投递，返回是否成功；异常返回 False。"""
        try:
            # 选择简历版本：出现简历列表时按 resume_name 匹配，匹配不到点默认项
            chosen = False
            list_loc = await _match(page, *self.SEL_RESUME_LIST)
            if list_loc is not None:
                opts_loc = await _match(page, *self.SEL_RESUME_OPTION)
                options = await opts_loc.all() if opts_loc is not None else []
                if options and resume_name:
                    for opt in options:
                        try:
                            text = (await opt.inner_text() or "").strip()
                        except Exception:  # noqa: BLE001
                            text = ""
                        if text and resume_name in text:
                            try:
                                await opt.click(timeout=3000)
                                chosen = True
                                break
                            except Exception as exc:  # noqa: BLE001
                                logger.warning("点击简历选项失败: %s", exc)
                if not chosen:
                    default = await _first(page, *self.SEL_RESUME_OPTION)
                    if default is not None:
                        try:
                            await default.click(timeout=3000)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("点击默认简历选项失败: %s", exc)
            # 点击最终"投递"确认按钮
            submit_btn = await _first(page, *self.SEL_SUBMIT_BTN)
            if submit_btn is None:
                logger.error("未找到最终'投递'按钮")
                return False
            await submit_btn.click(timeout=5000)
            # 等待"投递成功"类提示
            for sel in self.SEL_APPLY_SUCCESS:
                try:
                    if sel.startswith("text="):
                        await page.get_by_text(
                            sel[len("text="):], exact=False
                        ).first.wait_for(state="visible", timeout=3000)
                    else:
                        await page.wait_for_selector(sel, timeout=3000)
                    logger.info("submit_apply 成功（检测到成功提示 %s）", sel)
                    return True
                except Exception:  # noqa: BLE001
                    continue
            # 成功提示未出现：投递弹窗消失也视为成功
            try:
                dialog = await _first(page, *self.SEL_APPLY_DIALOG)
                if dialog is not None:
                    await dialog.wait_for(state="hidden", timeout=5000)
                    logger.info("submit_apply 成功（投递弹窗已关闭）")
                    return True
            except Exception:  # noqa: BLE001
                pass
            logger.warning("submit_apply 未能确认投递结果")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("submit_apply 异常: %s", exc)
            return False

    async def send_greeting(self, page: Page, job, text: str) -> bool:
        """猎聘无打招呼流程，直接跳过。"""
        logger.info("猎聘不支持打招呼，已跳过")
        return False


__all__ = ["CITY_CODES", "LiepinCollector"]
