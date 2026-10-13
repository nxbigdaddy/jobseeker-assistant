# -*- coding: utf-8 -*-
"""
前程无忧（51job）站点采集器。

选择器基于 51job we.51job.com 公开页面结构（2023+ SPA 版本），
若站点改版，优先在本文件顶部"选择器常量区"统一调整，避免散落修改。

依赖约定：
- 运行期仅使用标准库（re / logging / datetime / json / time / pathlib / urllib）；
- playwright 仅用于类型注解（TYPE_CHECKING 下导入），运行期不导入；
- app.collectors.base 采用延迟导入：基类尚未就绪时本模块仍可独立加载，
  依赖基类的方法返回安全默认值并记录 error 日志。
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
# 选择器常量区 —— 站点改版时优先在此调整
# =====================================================================

# ---- 登录页 ----
SEL_LOGIN_QR_SWITCH = [  # 切换"扫码登录"入口
    ".qrcode-login",
    "text=扫码登录",
    "text=二维码登录",
    "text=微信扫码",
]
SEL_QR_IMAGE = [  # 二维码图片 / 画布
    ".qrcode",
    "img[src*='qrcode']",
    "img[src*='qr']",
    "canvas",
    ".qr-code img",
]
SEL_SMS_TAB = [  # 切换"短信登录"标签
    "text=短信登录",
    "text=验证码登录",
    ".sms-login",
]
SEL_PHONE_INPUT = [
    "input[name='phone']",
    "input[placeholder*='手机']",
    "input[placeholder*='手机号']",
]
SEL_GET_CODE_BTN = [
    "text=获取验证码",
    "button:has-text('获取验证码')",
    ".get-code",
]
SEL_CODE_INPUT = [
    "input[name='code']",
    "input[placeholder*='验证码']",
    "input[placeholder*='短信']",
]
SEL_LOGIN_BTN = [
    "button[type='submit']",
    ".login-btn",
    "text=登录",
    "button:has-text('登 录')",
]
SEL_USER_MENU = [  # 已登录用户菜单（按 2026-09-01 真实已登录首页实测校准）
    # 可靠 href 特征：退出登录 / 我的51job 链接（未登录时不存在）
    "a[href*='common/logout']",
    "a[href*='/pc/my/myjob']",
    "a[href*='myjob']",
    # class 兜底
    ".username",
    ".userinfo",
    ".user-name",
    ".header-user",
    ".jobseeker-name",
    ".header-userinfo",
    ".user-menu",
]
# 51job 登录特征 Cookie（2026-09-01 实测校准）：
# - 匿名访问种下的 cookie：uid / guid / acw_tc / Hm_lvt_* / sensorsdata2015jssdkcross 等
#   —— 其中 uid 是匿名统计标识，不能用作登录判定；
# - 登录后额外出现：名为 "51job" 的大 cookie（~536B，含登录态）—— 真正的登录标志。
# 故登录判定只看 "51job" cookie。
SEL_LOGIN_COOKIES = {"51job"}
SEL_LOGIN_FORM_HINT = [  # 页面出现登录表单 = 未登录
    "#loginForm",
    ".login-form",
    "input[name='loginname']",
    "input[type='password']",
    "input[placeholder*='账号']",
    "input[placeholder*='密码']",
]

# ---- 搜索列表页 ----
SEL_LIST_CONTAINER = [".joblist-box", ".j_joblist"]
SEL_LIST_ITEM = [".joblist-box .e", ".j_joblist .e", ".job-primary"]
SEL_JOB_TITLE = [".jname", ".info-primary .jname"]
SEL_JOB_TITLE_ALT = ["a[title]", ".job-title", "h3 a", ".t1 a"]
SEL_JOB_LINK = [
    "a.jname",
    "a[href*='job_']",
    "a[href*='job_detail']",
    "a[href*='jobId']",
]
SEL_COMPANY = [".info-primary .name", ".cname", ".company-name", ".com_name"]
SEL_SALARY = [".sal", ".info-primary .sal", ".salary", ".t2 .sal"]
SEL_AREA = [".info-primary .area", ".t3", ".area", ".jarea"]
SEL_TAGS_SPANS = [
    ".info-primary .tags span",
    ".tags span",
    ".jtags span",
    ".tag-item",
    "span.tag",
]
SEL_TAGS_CONTAINER = [".info-primary .tags", ".tags", ".jtags"]
SEL_TIME = [".time", ".t4", ".pub-time", ".date", ".jtime"]
SEL_NEXT_PAGE = [
    ".el-pagination .btn-next",
    ".pagination .next",
    ".el-pager .btn-next",
    "text=下一页",
]

# ---- 岗位详情页 ----
SEL_JOB_DETAIL_CONTAINER = [".tCompany_main", ".job_msg", "#jobDetail"]
SEL_JOB_DESC = [
    ".job_msg",
    ".bmsg",
    "#jobDetail",
    ".job-desc",
    ".tBorderTop_box .bmsg",
]
SEL_DETAIL_TITLE = [".tHeader h1", ".t1 h1", ".j_job_t h1", "h1", "title"]
SEL_DETAIL_COMPANY = [
    ".tHeader .cname",
    ".com_name",
    ".company-name",
    ".tHeader .t2 a",
    "a[href*='company']",
]
SEL_DETAIL_SALARY = [".tHeader .sal", ".job-salary", ".salary", ".t2 .sal"]

# ---- 投递 ----
SEL_APPLY_BTN = [
    "#applyJob",
    ".btn_apply",
    "button:has-text('立即申请')",
    "text=立即申请",
]
SEL_APPLY_DIALOG = ["#applyDialog", ".resume-select", ".apply-dialog", ".el-dialog"]
SEL_RESUME_OPTION = [
    ".resume-select .item",
    ".resume-select li",
    ".resume-list li",
    ".resume-item",
    "input[type='radio']",
]
SEL_SUBMIT_BTN = [
    ".btn-primary",
    "#btnSendResume",
    "button:has-text('确认投递')",
    "text=确认投递",
    "button:has-text('投递')",
    "text=投递",
]
SEL_APPLY_SUCCESS = [
    "text=投递成功",
    "text=申请成功",
    ".apply-success",
    ".success-box",
    "text=已投递",
]

# ---- 其他参数 ----
QR_TIMEOUT_SECONDS = 180
LOGIN_POLL_INTERVAL_MS = 2000

# =====================================================================
# 通用辅助
# =====================================================================


async def _match(root, *selectors):
    """依次尝试选择器，返回第一个能匹配元素的完整 Locator（可能匹配多个）；无匹配返回 None。

    root 可以是 Page 或 Locator；``text=xxx`` 前缀走 get_by_text(exact=False) 兜底。
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
    """把一组选择器拼成 CSS 逗号列表（过滤 text= 引擎项），供 wait_for_selector 使用。"""
    return ", ".join(s for s in selectors if not s.startswith("text="))


def _city_code(city: str | None) -> str:
    """返回城市对应的 51job 城市编码；未知城市返回 '000000'（全国）。"""
    if not city:
        return "000000"
    city = city.strip()
    code = CITY_CODES.get(city)
    if code:
        return code
    for suffix in ("市", "省"):
        if city.endswith(suffix) and len(city) > len(suffix):
            code = CITY_CODES.get(city[: -len(suffix)])
            if code:
                return code
    return "000000"


def _fallback_salary(text: str) -> tuple[int | None, int | None]:
    """本地兜底薪资解析（base.normalize_salary 不可用时），单位为千元/月。"""
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


# 常用城市 -> 51job 城市编码（未知城市由 _city_code 兜底为 000000 全国）
CITY_CODES: dict[str, str] = {
    "北京": "010000",
    "上海": "020000",
    "广州": "030200",
    "深圳": "040000",
    "杭州": "080200",
    "南京": "070200",
    "成都": "090200",
    "武汉": "170200",
    "西安": "200200",
    "苏州": "060300",
    "天津": "050000",
    "重庆": "060000",
}


# =====================================================================
# 采集器主体
# =====================================================================
class Wuyou51Collector(_BaseCollector):
    """前程无忧（51job）站点采集器，实现 Collector 抽象接口。"""

    source = "51job"

    # ------------------------------------------------------------------
    # base 延迟加载与 RawJob 构造
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
            "crawl_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
        """从岗位链接中提取 job_id；取不到时退回 URL 哈希 / 末段。"""
        if not url:
            return ""
        m = re.search(r"job_(\d+)", url)
        if m:
            return m.group(1)
        m = re.search(r"/job_detail/(\d+)", url)
        if m:
            return m.group(1)
        if "#" in url:
            frag = url.rsplit("#", 1)[-1].strip()
            if frag:
                return frag
        tail = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        if re.search(r"\d", tail):
            return tail
        return ""

    @staticmethod
    def _split_area(area_text: str) -> tuple[str, str]:
        """拆分形如 '北京-海淀区' 的地区文本 -> (city, district)。"""
        if not area_text:
            return "", ""
        parts = [p.strip() for p in re.split(r"[-–—，,]", area_text) if p.strip()]
        if not parts:
            return "", ""
        city = parts[0].rstrip("市") if len(parts[0]) > 2 else parts[0]
        district = parts[1] if len(parts) >= 2 else ""
        return city, district

    @staticmethod
    def _classify_tag(text: str) -> str:
        """把标签文本归类：experience / education / tag。"""
        if "经验" in text:
            return "experience"
        if any(k in text for k in ("大专", "本科", "硕士", "博士", "学历")):
            return "education"
        return "tag"

    async def _settle(self, page: Page) -> None:
        """等待页面加载平稳（networkidle 尽力而为 + 固定停顿）。"""
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(800)

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    async def goto_login(self, page: Page) -> None:
        """打开 51job 登录页。"""
        try:
            await page.goto(
                "https://login.51job.com/login.php",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self._settle(page)
        except Exception as exc:  # noqa: BLE001
            logger.error("goto_login 失败: %s", exc)

    async def _has_login_cookie(self, page: Page) -> bool:
        """Cookie 辅助信号：存在 51job 登录特征 Cookie（uid / 51job）即视为已登录。

        DOM 选择器可能因站点改版失效，Cookie 是独立且可靠的登录判定通道。
        """
        try:
            cookies = await page.context.cookies()
            names = {c.get("name", "") for c in cookies}
            return bool(SEL_LOGIN_COOKIES & names)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取 51job cookie 失败: %s", exc)
            return False

    async def is_logged_in(self, page: Page) -> bool:
        """判断登录态：访问首页，若被重定向到登录页或出现登录表单则未登录；异常返回 False。

        判定信号（任一命中即已登录）：
        1) DOM 用户菜单（href 特征/class 选择器）；
        2) 登录特征 Cookie（uid / 51job）。
        两个信号都没有才判定未登录（保守，不误报）。
        """
        try:
            # 已在非登录业务页时，直接检查 DOM，避免反复跳转
            current = (page.url or "").lower()
            if (
                current.startswith("http")
                and "login" not in current
                and "passport" not in current
                and "sso" not in current
            ):
                if await _first(page, *SEL_USER_MENU) is not None:
                    return True
                if await _first(page, *SEL_LOGIN_FORM_HINT) is not None:
                    return False
                if await self._has_login_cookie(page):
                    return True
                # 无任何已登录证据（用户菜单/登录表单/cookie 均未命中）：
                # 保守判定未登录，绝不默认"已登录"（否则会误报扫码成功）
                return False
            # 需要导航校验：访问首页，看是否被重定向回登录页
            await page.goto(
                "https://www.51job.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self._settle(page)
            url = (page.url or "").lower()
            if "login" in url or "passport" in url or "sso" in url:
                return False
            if await _first(page, *SEL_USER_MENU) is not None:
                return True
            if await _first(page, *SEL_LOGIN_FORM_HINT) is not None:
                return False
            if await self._has_login_cookie(page):
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
            if "login" not in current and "passport" not in current:
                await self.goto_login(page)
            # 尝试切换到扫码登录
            switch = await _first(page, *SEL_LOGIN_QR_SWITCH)
            if switch is not None:
                try:
                    await switch.click(timeout=5000)
                    await page.wait_for_timeout(800)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("点击扫码入口失败，忽略: %s", exc)
            # 等待二维码元素出现
            try:
                await page.wait_for_selector(_css_join(SEL_QR_IMAGE), timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            qr = await _first(page, *SEL_QR_IMAGE)
            qr_missing = qr is None
            if qr is not None:
                try:
                    _DATA_DIR.mkdir(parents=True, exist_ok=True)
                    qr_path = str(
                        _DATA_DIR
                        / f"qr_51job_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    )
                    await qr.screenshot(path=qr_path)
                    if callable(qr_callback):
                        qr_callback(qr_path)
                    logger.info("二维码已截图: %s", qr_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("二维码截图/回调失败: %s", exc)
            else:
                # 二维码元素未找到：可能改版/登录页默认即扫码。
                # 等待 3 秒后再找一次，仍没有则缩短轮询上限并提示（避免空等 180 秒）。
                logger.warning("未找到二维码元素，等待 3 秒后重试查找")
                await page.wait_for_timeout(3000)
                qr = await _first(page, *SEL_QR_IMAGE)
                if qr is None:
                    qr_missing = True
                    logger.error("仍未找到二维码元素（站点可能改版），将缩短登录等待时间")
                else:
                    qr_missing = False
                    try:
                        _DATA_DIR.mkdir(parents=True, exist_ok=True)
                        qr_path = str(
                            _DATA_DIR
                            / f"qr_51job_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                        )
                        await qr.screenshot(path=qr_path)
                        if callable(qr_callback):
                            qr_callback(qr_path)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("二维码截图/回调失败: %s", exc)
            # 轮询登录状态（二维码缺失时缩短等待，避免用户空等）
            deadline = time.monotonic() + (
                QR_TIMEOUT_SECONDS if not qr_missing else 30
            )
            while time.monotonic() < deadline:
                await page.wait_for_timeout(LOGIN_POLL_INTERVAL_MS)
                try:
                    if await self.is_logged_in(page):
                        return True
                except Exception:  # noqa: BLE001
                    continue
            logger.warning("扫码登录超时（%s 秒）", QR_TIMEOUT_SECONDS)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("login_qr 异常: %s", exc)
            return False

    async def login_sms(self, page: Page, phone: str, code: str) -> bool:
        """短信登录：切标签 -> 填手机号 -> 获取验证码 -> 填验证码 -> 登录 -> 轮询结果。"""
        try:
            current = (page.url or "").lower()
            if "login" not in current and "passport" not in current:
                await self.goto_login(page)
            tab = await _first(page, *SEL_SMS_TAB)
            if tab is not None:
                try:
                    await tab.click(timeout=5000)
                    await page.wait_for_timeout(600)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("切换短信登录标签失败: %s", exc)
            phone_input = await _first(page, *SEL_PHONE_INPUT)
            if phone_input is None:
                logger.error("未找到手机号输入框，短信登录失败")
                return False
            await phone_input.fill(str(phone))
            get_code = await _first(page, *SEL_GET_CODE_BTN)
            if get_code is not None:
                try:
                    await get_code.click(timeout=5000)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("点击'获取验证码'失败，继续尝试: %s", exc)
            code_input = await _first(page, *SEL_CODE_INPUT)
            if code_input is None:
                logger.error("未找到验证码输入框，短信登录失败")
                return False
            await code_input.fill(str(code))
            login_btn = await _first(page, *SEL_LOGIN_BTN)
            if login_btn is None:
                logger.error("未找到登录按钮，短信登录失败")
                return False
            await login_btn.click(timeout=5000)
            for _ in range(30):
                await page.wait_for_timeout(LOGIN_POLL_INTERVAL_MS)
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
            if code != "000000":
                url = (
                    f"https://we.51job.com/pc/search?jobArea={code}"
                    f"&keyword={quote(keyword)}"
                )
            else:
                url = f"https://we.51job.com/pc/search?keyword={quote(keyword)}"
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
                    await page.wait_for_selector(
                        _css_join(SEL_LIST_CONTAINER), timeout=15000
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "第 %s 页未等到列表容器（可能已改版或无结果），尝试直接解析",
                        page_no,
                    )
                items_loc = await _match(page, *SEL_LIST_ITEM)
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
        title_el = await _first(item, *SEL_JOB_TITLE)
        if title_el is None:
            title_el = await _first(item, *SEL_JOB_TITLE_ALT)
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
            link_el = await _first(item, *SEL_JOB_LINK)
            if link_el is not None:
                try:
                    job_url = (await link_el.get_attribute("href") or "").strip()
                except Exception:  # noqa: BLE001
                    pass
        if job_url and not job_url.startswith("http"):
            job_url = urljoin("https://we.51job.com/", job_url)
        job_id = self._extract_job_id(job_url)
        # 公司
        company = ""
        c_el = await _first(item, *SEL_COMPANY)
        if c_el is not None:
            try:
                company = (await c_el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                company = ""
        # 薪资
        salary_raw = ""
        s_el = await _first(item, *SEL_SALARY)
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
        # 城市 / 区
        area_text = ""
        a_el = await _first(item, *SEL_AREA)
        if a_el is not None:
            try:
                area_text = (await a_el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                area_text = ""
        parsed_city, district = self._split_area(area_text)
        if not parsed_city and city:
            parsed_city = city.strip()
        # 经验 / 学历 / 标签
        experience, education = "", ""
        tags: list[str] = []
        spans_loc = await _match(item, *SEL_TAGS_SPANS)
        spans = await spans_loc.all() if spans_loc is not None else []
        if not spans:
            cont = await _match(item, *SEL_TAGS_CONTAINER)
            if cont is not None:
                try:
                    spans = await cont.locator("span").all()
                except Exception:  # noqa: BLE001
                    spans = []
        for sp in spans:
            try:
                t = (await sp.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not t:
                continue
            kind = self._classify_tag(t)
            if kind == "experience":
                if not experience:
                    experience = t
            elif kind == "education":
                if not education:
                    education = t
            else:
                tags.append(t)
        # 发布时间（原样保存）
        publish_date = ""
        t_el = await _first(item, *SEL_TIME)
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
            btn = await _first(page, *SEL_NEXT_PAGE)
            if btn is None:
                return False
            cls = await btn.get_attribute("class") or ""
            disabled = await btn.get_attribute("disabled")
            if disabled is not None or "disabled" in cls:
                return False
            await btn.click(timeout=5000)
            await page.wait_for_timeout(1500)  # 页间停顿防反爬
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
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await self._settle(page)
            try:
                await page.wait_for_selector(
                    _css_join(SEL_JOB_DETAIL_CONTAINER), timeout=15000
                )
            except Exception:  # noqa: BLE001
                logger.warning("详情页容器未等到: %s", job_url)
            # 岗位描述
            description = ""
            desc_el = await _first(page, *SEL_JOB_DESC)
            if desc_el is not None:
                try:
                    description = (await desc_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    description = ""
            # 标题
            title = ""
            t_el = await _first(page, *SEL_DETAIL_TITLE)
            if t_el is not None:
                try:
                    title = (await t_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    title = ""
            # 公司
            company = ""
            c_el = await _first(page, *SEL_DETAIL_COMPANY)
            if c_el is not None:
                try:
                    company = (await c_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    company = ""
            # 薪资
            salary_raw = ""
            s_el = await _first(page, *SEL_DETAIL_SALARY)
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
                    logger.debug("详情页薪资解析失败 %r，使用本地兜底: %s", salary_raw, exc)
            job_id = self._extract_job_id(job_url)
            return self._make_raw_job(
                job_id=job_id,
                title=title,
                company=company,
                salary_min=salary_min,
                salary_max=salary_max,
                salary_raw=salary_raw,
                description=description,
                job_url=job_url,
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
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await self._settle(page)
            try:
                await page.wait_for_selector(_css_join(SEL_APPLY_BTN), timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            btn = await _first(page, *SEL_APPLY_BTN)
            if btn is None:
                logger.warning("未找到'立即申请'按钮: %s", job_url)
                return
            await btn.click(timeout=5000)
            await page.wait_for_timeout(1000)
            try:
                await page.wait_for_selector(
                    _css_join(SEL_APPLY_DIALOG), timeout=5000
                )
            except Exception:  # noqa: BLE001
                logger.debug("未检测到投递弹窗（可能已直接进入选择简历界面）")
        except Exception as exc:  # noqa: BLE001
            logger.error("open_apply 异常: %s", exc)

    async def submit_apply(self, page: Page, resume_name: str) -> bool:
        """在投递弹窗中选择简历版本并点击最终投递，返回是否成功；异常返回 False。"""
        try:
            # 选择简历版本：优先按 resume_name 文本匹配，匹配不到点默认项
            chosen = False
            opts_loc = await _match(page, *SEL_RESUME_OPTION)
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
                default = await _first(page, *SEL_RESUME_OPTION)
                if default is not None:
                    try:
                        await default.click(timeout=3000)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("点击默认简历选项失败: %s", exc)
            # 点击最终"投递"按钮
            submit_btn = await _first(page, *SEL_SUBMIT_BTN)
            if submit_btn is None:
                logger.error("未找到最终'投递'按钮")
                return False
            await submit_btn.click(timeout=5000)
            # 等待"投递成功"类提示
            for sel in SEL_APPLY_SUCCESS:
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
            # 成功提示未出现：投递按钮消失也视为成功
            try:
                await submit_btn.wait_for(state="hidden", timeout=5000)
                logger.info("submit_apply 成功（投递按钮已消失）")
                return True
            except Exception:  # noqa: BLE001
                pass
            logger.warning("submit_apply 未能确认投递结果")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("submit_apply 异常: %s", exc)
            return False

    async def send_greeting(self, page: Page, job, text: str) -> bool:
        """51job 无打招呼流程，直接跳过。"""
        logger.info("51job 不支持打招呼，已跳过")
        return False


__all__ = ["CITY_CODES", "Wuyou51Collector"]
