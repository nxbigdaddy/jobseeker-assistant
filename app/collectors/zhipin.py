# -*- coding: utf-8 -*-
"""
Boss直聘（zhipin）站点采集器。

选择器基于 zhipin 公开页面结构，反爬较强，改版时优先调整此处
（类属性"选择器常量区"统一维护，避免散落修改）。

站点特殊性（设计文档 §2.1）：Boss直聘网页端**仅 App 扫码登录**，
无简历投递弹窗——"投递"即与 HR 打开沟通窗口发送打招呼语（send_greeting）。

依赖约定：
- 运行期仅使用标准库（re / logging / json / time / datetime / pathlib / urllib）；
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
# 通用辅助
# =====================================================================


async def _match(root, *selectors):
    """依次尝试选择器，返回第一个能匹配元素的完整 Locator（可能匹配多个）；无匹配返回 None。

    root 可以是 Page 或 Locator；``text=xxx`` 前缀走 get_by_text(exact=False) 兜底。

    注意：playwright async API 中 Locator.count() 是协程，必须 await，
    否则比较会抛 TypeError 并被吞掉导致永远匹配不到（故本函数为 async）。
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


# 常用城市 -> Boss直聘城市码（未知城市由 _city_code 兜底返回 ''，即不传 city 参数）
CITY_CODES: dict[str, str] = {
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "武汉": "101200100",
    "南京": "101190100",
    "西安": "101110100",
    "苏州": "101190400",
    "天津": "101030100",
    "重庆": "101040100",
}


def _city_code(city: str | None) -> str:
    """返回城市对应的 Boss 城市码；未知城市返回 ''（调用方不传 city 参数，表示不限城市）。"""
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
class ZhipinCollector(_BaseCollector):
    """Boss直聘（zhipin）站点采集器，实现 Collector 抽象接口。

    特殊性（设计文档 §2.1）：网页端仅 App 扫码登录；无简历投递弹窗，
    "投递" = 打开与 HR 的沟通窗口并发送打招呼语（send_greeting）。
    """

    source = "zhipin"

    # =================================================================
    # 选择器候选列表（类属性）—— 站点改版时优先在此调整
    # 基于 www.zhipin.com 公开页面结构（web/geek 系列 SPA 页面）
    # =================================================================

    # ---- 登录（web/user 登录页 / 首页登录弹窗，仅 App 扫码）----
    SEL_LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
    SEL_HEADER_LOGIN_BTN = [  # 首页头部"登录"入口（触发登录弹窗）
        "a[href*='web/user']",
        "button:has-text('登录')",
        "text=登录",
    ]
    SEL_QR_IMAGE = [  # 登录弹窗中的二维码元素
        ".login-qrcode",
        ".login-qrcode canvas",
        ".login-qrcode img",
        "img[src*='qr']",
        "img[src*='qrcode']",
        "canvas",
    ]
    SEL_LOGIN_DIALOG = [  # 登录弹窗/遮罩（未登录提示）
        ".login-dialog",
        ".login-modal",
        ".login-mask",
        ".login-qrcode",
        "text=扫码登录",
    ]
    SEL_LOGIN_MASK = [  # 未登录采集时弹出的登录遮罩/引导
        ".login-mask",
        ".login-dialog",
        ".login-modal",
        "text=请先登录",
        "text=登录后查看",
        "text=立即登录",
    ]
    SEL_USER_MENU = [  # 已登录用户信息（用户信息区 / "我的"菜单）
        ".user-info",
        ".user-nav",
        ".user-card",
        "text=我的投递",
        "text=退出登录",
        "text=个人中心",
    ]

    # ---- 搜索列表页（/web/geek/job，SPA）----
    SEL_LIST_CONTAINER = [".job-card-wrapper", ".job-list-box"]
    SEL_LIST_ITEM = [
        ".job-list-box .job-card-wrapper",
        ".job-card-wrapper",
        ".job-card-box",
        ".job-list-box li",
    ]
    SEL_JOB_TITLE = [".job-name", ".job-card-left .job-name"]
    SEL_JOB_LINK = [
        "a[href*='/job_detail/']",
        "a[href*='job_detail']",
        ".job-card-left .job-name",
    ]
    SEL_COMPANY = [".company-name", ".company-info .name"]
    SEL_SALARY = [".salary", ".job-card-left .salary"]
    SEL_AREA = [".job-area", ".job-card-right .job-area"]
    SEL_JOB_INFO = [".job-info", ".job-card-footer"]
    SEL_TAG_LIST = [
        ".job-card-footer .tag-list li",
        ".job-info .tag-list li",
        ".job-card-footer li",
    ]
    SEL_TIME = [".job-pub-time", ".job-card-right .job-pub-time"]
    SEL_NEXT_PAGE = [
        ".ui-pagination .btn-next",
        ".job-list-box .btn-next",
        ".btn-next",
        "text=下一页",
        "a:has-text('下一页')",
    ]

    # ---- 岗位详情页（/job_detail/{id}.html）----
    SEL_DETAIL_CONTAINER = [".job-sec-text", "#job-detail", ".job-detail"]
    SEL_DETAIL_DESC = [
        ".job-sec-text",
        "#job-detail .job-sec-text",
        ".job-detail .job-sec-text",
        ".job-detail",
    ]
    SEL_DETAIL_TITLE = ["h1", ".job-name", ".job-banner .name", ".job-title"]
    SEL_DETAIL_COMPANY = [
        ".company-name",
        ".company-info .name",
        "a[href*='/company/']",
    ]
    SEL_DETAIL_SALARY = [".salary", ".job-banner .salary", ".job-info .salary"]

    # ---- 沟通窗口（Boss 无投递弹窗，投递即打招呼）----
    SEL_START_CHAT_BTN = [
        ".btn-startchat",
        "button:has-text('立即沟通')",
        "button:has-text('沟通')",
        ".btn-primary:has-text('沟通')",
        "text=立即沟通",
    ]
    SEL_CHAT_INPUT = [".chat-input", "textarea", ".ql-editor"]
    SEL_SEND_BTN = [
        ".btn-send",
        "button:has-text('发送')",
        "text=发送",
    ]
    SEL_CHAT_BUBBLE = [  # 已发送消息气泡
        ".msg-item",
        ".chat-message .msg-item",
        ".message-item",
        "text=已发送",
    ]

    # ---- 其他参数 ----
    QR_TIMEOUT_SECONDS = 240  # Boss 扫码确认后跳转较慢，轮询上限放宽到 240 秒
    LOGIN_POLL_INTERVAL_MS = 2000
    PAGE_WAIT_MS = 1500

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

    # ------------------------------------------------------------------
    # 字段解析辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_job_id(url: str) -> str:
        """从岗位链接中提取 job_id（href 形如 /job_detail/{id}.html）；取不到时退回 URL 末段数字。"""
        if not url:
            return ""
        m = re.search(r"/job_detail/([A-Za-z0-9_]+)", url)
        if m:
            return m.group(1)
        m = re.search(r"job_detail[/=]([A-Za-z0-9_]+)", url)
        if m:
            return m.group(1)
        tail = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0].split("#")[0]
        digits = re.findall(r"\d+", tail)
        if digits:
            return digits[-1]
        return ""

    @staticmethod
    def _split_area(area_text: str) -> tuple[str, str]:
        """拆分形如 '北京·海淀区' / '北京-海淀区' 的地区文本 -> (city, district)。"""
        if not area_text:
            return "", ""
        parts = [p.strip() for p in re.split(r"[·\-]", area_text) if p.strip()]
        if not parts:
            return "", ""
        city = parts[0].rstrip("市") if len(parts[0]) > 2 else parts[0]
        district = parts[1] if len(parts) >= 2 else ""
        return city, district

    @staticmethod
    def _classify_tag(text: str) -> str:
        """把标签文本归类：experience / education / tag（按关键词，其余进 tags）。"""
        if "经验" in text or re.search(r"\d+\s*[-~—至]\s*\d+\s*年", text) or re.search(
            r"\d+\s*年", text
        ):
            return "experience"
        if any(k in text for k in ("大专", "本科", "硕士", "博士", "学历", "中专", "高中")):
            return "education"
        return "tag"

    async def _settle(self, page: Page) -> None:
        """等待页面加载平稳（networkidle 尽力而为 + 固定停顿）。"""
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(800)

    async def _has_login_wall(self, page: Page) -> bool:
        """检测页面是否出现 Boss 登录遮罩/弹窗（未登录采集时常见）。"""
        try:
            return await _first(page, *self.SEL_LOGIN_MASK) is not None
        except Exception:  # noqa: BLE001
            return False

    async def _ensure_login_page(self, page: Page) -> None:
        """确保当前处于可展示登录二维码的页面：优先登录页，失败则回退首页点击'登录'。"""
        current = (page.url or "").lower()
        if "web/user" not in current and "login" not in current:
            await self.goto_login(page)
        # 登录页未渲染二维码时回退：访问首页点击"登录"触发登录弹窗
        qr = await _first(page, *self.SEL_QR_IMAGE)
        if qr is not None:
            return
        try:
            await page.goto(
                "https://www.zhipin.com/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(1500)
            btn = await _first(page, *self.SEL_HEADER_LOGIN_BTN)
            if btn is not None:
                await btn.click(timeout=5000)
                await page.wait_for_timeout(1500)
        except Exception as exc:  # noqa: BLE001
            logger.debug("首页触发登录弹窗失败，忽略: %s", exc)

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    async def goto_login(self, page: Page) -> None:
        """打开 Boss直聘登录页（网页端仅 App 扫码）。"""
        try:
            await page.goto(
                self.SEL_LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self._settle(page)
        except Exception as exc:  # noqa: BLE001
            logger.error("goto_login 失败: %s", exc)

    async def is_logged_in(self, page: Page) -> bool:
        """判断登录态：访问个人推荐页，被重定向到登录页或出现登录弹窗即未登录；异常返回 False。"""
        try:
            await page.goto(
                "https://www.zhipin.com/web/geek/recommend",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await self._settle(page)
            url = (page.url or "").lower()
            if "web/user" in url or "login" in url:
                return False
            if await _first(page, *self.SEL_LOGIN_DIALOG) is not None:
                return False
            if await _first(page, *self.SEL_USER_MENU) is not None:
                return True
            if await self._cookie_logged_in(page):
                return True
            # 用户信息未出现且无登录弹窗：无法确认登录态，保守判定未登录
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("is_logged_in 异常: %s", exc)
            return False

    async def login_qr(self, page: Page, qr_callback) -> bool:
        """扫码登录（Boss 网页端唯一登录方式）。

        等待登录弹窗中的二维码元素出现，截图保存到 data/qr_zhipin_*.png 并回调
        qr_callback(qr_path)；随后每 2 秒轮询 is_logged_in，最长 240 秒
        （Boss 扫码确认后跳转较慢）。
        """
        try:
            await self._ensure_login_page(page)
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_QR_IMAGE), timeout=10000
                )
            except Exception:  # noqa: BLE001
                logger.warning("10 秒内未等到二维码元素（可能已改版），尝试直接轮询")
            qr = await _first(page, *self.SEL_QR_IMAGE)
            if qr is not None:
                try:
                    _DATA_DIR.mkdir(parents=True, exist_ok=True)
                    qr_path = str(
                        _DATA_DIR
                        / f"qr_zhipin_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                    )
                    await qr.screenshot(path=qr_path)
                    if callable(qr_callback):
                        qr_callback(qr_path)
                    logger.info("二维码已截图: %s", qr_path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("二维码截图/回调失败: %s", exc)
            else:
                # 部分版本登录页默认即扫码：等待 3 秒后直接轮询
                logger.info("未找到二维码元素，等待 3 秒后直接轮询登录状态")
                await page.wait_for_timeout(3000)
            # 轮询登录状态（每 2 秒，最长 240 秒）
            deadline = time.monotonic() + self.QR_TIMEOUT_SECONDS
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
        """Boss直聘网页端无短信登录：直接记录 warning 并返回 False。"""
        logger.warning("Boss直聘网页端仅支持 App 扫码登录，不支持短信登录")
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
        """按关键词+城市搜索岗位并翻页采集，返回 RawJob 列表；异常返回 []。"""
        try:
            base = self._load_base()
            if base is None:
                logger.error("app.collectors.base 未就绪，search_jobs 返回空列表")
                return []
            code = _city_code(city)
            url = f"https://www.zhipin.com/web/geek/job?query={quote(keyword)}"
            if code:
                url += f"&city={code}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await self._settle(page)
            current = (page.url or "").lower()
            if "web/user" in current or "login" in current:
                logger.error("搜索页被重定向到登录页，请先登录后再采集")
                return []
            results: list = []
            seen: set[str] = set()
            page_no = 0
            for page_no in range(1, max_pages + 1):
                # 未登录可能弹出登录遮罩：检测到则记 warning 并跳过本页（返回已解析部分）
                if await self._has_login_wall(page):
                    logger.warning(
                        "Boss 需要登录，跳过本页（keyword=%s city=%s page=%s）",
                        keyword,
                        city,
                        page_no,
                    )
                    break
                # SPA 列表渲染：等待列表容器任一出现，最多 30 秒
                try:
                    await page.wait_for_selector(
                        _css_join(self.SEL_LIST_CONTAINER), timeout=30000
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "第 %s 页未等到列表容器（可能已改版或无结果），尝试直接解析",
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
        # 岗位链接与 job_id（href 形如 /job_detail/{id}.html）
        job_url = ""
        link_el = await _first(item, *self.SEL_JOB_LINK)
        if link_el is not None:
            try:
                job_url = (await link_el.get_attribute("href") or "").strip()
            except Exception:  # noqa: BLE001
                job_url = ""
        if not job_url and title_el is not None:
            try:
                job_url = (await title_el.get_attribute("href") or "").strip()
            except Exception:  # noqa: BLE001
                pass
        if job_url and not job_url.startswith("http"):
            job_url = urljoin("https://www.zhipin.com/", job_url)
        job_id = self._extract_job_id(job_url)
        # 公司
        company = ""
        c_el = await _first(item, *self.SEL_COMPANY)
        if c_el is not None:
            try:
                company = (await c_el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                company = ""
        # 薪资（raw + normalize_salary）
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
        # 城市 / 区（"北京·海淀区"按 '·'/'-' 拆分）
        area_text = ""
        a_el = await _first(item, *self.SEL_AREA)
        if a_el is not None:
            try:
                area_text = (await a_el.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                area_text = ""
        parsed_city, district = self._split_area(area_text)
        if not parsed_city and city:
            parsed_city = city.strip()
        # 经验 / 学历 / 标签（.job-card-footer .tag-list li / .job-info 文本按关键词归类）
        experience, education = "", ""
        tags: list[str] = []
        li_loc = await _match(item, *self.SEL_TAG_LIST)
        lis = await li_loc.all() if li_loc is not None else []
        if not lis:
            info_el = await _first(item, *self.SEL_JOB_INFO)
            if info_el is not None:
                try:
                    lis = await info_el.locator("li, span").all()
                except Exception:  # noqa: BLE001
                    lis = []
        for li in lis:
            try:
                t = (await li.inner_text() or "").strip()
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
        # 发布时间（相对时间原样保存，如 "3天前"）
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
        """点击"下一页"并等待（每页停顿 1500ms 防反爬），成功返回 True。"""
        try:
            btn = await _first(page, *self.SEL_NEXT_PAGE)
            if btn is None:
                return False
            cls = await btn.get_attribute("class") or ""
            disabled = await btn.get_attribute("disabled")
            if disabled is not None or "disabled" in cls:
                return False
            await btn.click(timeout=5000)
            await page.wait_for_timeout(self.PAGE_WAIT_MS)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("翻页失败: %s", exc)
            return False

    async def parse_job(self, page: Page, job_url: str):
        """打开岗位详情页解析完整信息并回填 title/company/salary/description；登录墙/失败返回 None。"""
        try:
            base = self._load_base()
            if base is None:
                logger.error("app.collectors.base 未就绪，parse_job 返回 None")
                return None
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await self._settle(page)
            current = (page.url or "").lower()
            if "web/user" in current or "login" in current:
                logger.warning("详情页被重定向到登录页，解析失败: %s", job_url)
                return None
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_DETAIL_CONTAINER), timeout=15000
                )
            except Exception:  # noqa: BLE001
                logger.warning("详情页容器未等到: %s", job_url)
            # 登录墙检测
            if await self._has_login_wall(page):
                logger.warning("详情页出现登录弹窗，解析失败: %s", job_url)
                return None
            # 岗位描述
            description = ""
            desc_el = await _first(page, *self.SEL_DETAIL_DESC)
            if desc_el is not None:
                try:
                    description = (await desc_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    description = ""
            # 标题
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
            # 薪资（详情有则回填）
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
                    logger.debug("详情薪资解析失败 %r，使用本地兜底: %s", salary_raw, exc)
            # 未解析到任何有效内容 → 判为失败（反爬/登录墙/超时）
            if not description and not title:
                logger.warning("详情页未解析到有效内容，判定失败: %s", job_url)
                return None
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
    # 投递（Boss 特殊：无直接投递，走"沟通/打招呼"流程）
    # ------------------------------------------------------------------
    async def open_apply(self, page: Page, job) -> None:
        """打开与 HR 的沟通窗口（Boss 无简历投递弹窗，投递即打招呼）。

        进入岗位详情页，点击"立即沟通"按钮打开聊天窗口，不发送任何消息。
        """
        try:
            job_url = getattr(job, "job_url", "") or ""
            if not job_url:
                logger.warning("job 缺少 job_url，open_apply 跳过")
                return
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await self._settle(page)
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_START_CHAT_BTN), timeout=15000
                )
            except Exception:  # noqa: BLE001
                pass
            btn = await _first(page, *self.SEL_START_CHAT_BTN)
            if btn is None:
                logger.warning("未找到'立即沟通'按钮: %s", job_url)
                return
            await btn.click(timeout=5000)
            await page.wait_for_timeout(1000)
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_CHAT_INPUT), timeout=8000
                )
            except Exception:  # noqa: BLE001
                logger.debug("未检测到聊天窗口输入框（可能未打开沟通窗口）")
        except Exception as exc:  # noqa: BLE001
            logger.error("open_apply 异常: %s", exc)

    async def submit_apply(self, page: Page, resume_name: str) -> bool:
        """Boss直聘无简历投递弹窗：直接返回 False 并提示使用 send_greeting 打招呼流程。"""
        logger.info("Boss 直聘请使用 send_greeting 打招呼流程（无简历投递弹窗）")
        return False

    async def send_greeting(self, page: Page, job, text: str) -> bool:
        """在 Boss 聊天窗口输入打招呼语并发送；输入框清空或出现已发送气泡视为成功。"""
        try:
            if not text:
                logger.warning("打招呼文本为空，send_greeting 跳过")
                return False
            # 等待聊天输入框；未打开沟通窗口时尝试在详情页点"立即沟通"
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_CHAT_INPUT), timeout=8000
                )
            except Exception:  # noqa: BLE001
                btn = await _first(page, *self.SEL_START_CHAT_BTN)
                if btn is not None:
                    try:
                        await btn.click(timeout=5000)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("点击'立即沟通'失败: %s", exc)
                    try:
                        await page.wait_for_selector(
                            _css_join(self.SEL_CHAT_INPUT), timeout=8000
                        )
                    except Exception:  # noqa: BLE001
                        pass
            chat_input = await _first(page, *self.SEL_CHAT_INPUT)
            if chat_input is None:
                logger.warning("未找到聊天输入框，send_greeting 失败")
                return False
            await chat_input.fill(text)
            await page.wait_for_timeout(300)
            # 优先点击发送按钮；找不到则按 Enter 发送
            sent = False
            send_btn = await _first(page, *self.SEL_SEND_BTN)
            if send_btn is not None:
                try:
                    await send_btn.click(timeout=5000)
                    sent = True
                except Exception as exc:  # noqa: BLE001
                    logger.debug("点击发送按钮失败，尝试 Enter 发送: %s", exc)
            if not sent:
                try:
                    await chat_input.press("Enter")
                    sent = True
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Enter 发送失败: %s", exc)
            if not sent:
                logger.warning("打招呼发送动作未执行成功")
                return False
            # 成功判定：输入框清空 或 出现已发送气泡
            for _ in range(10):
                await page.wait_for_timeout(500)
                empty = False
                try:
                    value = await chat_input.input_value()
                    empty = not (value or "").strip()
                except Exception:  # noqa: BLE001 - contenteditable（.ql-editor）无 input_value
                    try:
                        empty = not (await chat_input.inner_text() or "").strip()
                    except Exception:  # noqa: BLE001
                        empty = False
                if empty or await _first(page, *self.SEL_CHAT_BUBBLE) is not None:
                    logger.info("send_greeting 成功（输入框已清空或出现已发送气泡）")
                    return True
            logger.warning("send_greeting 未能确认发送结果")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("send_greeting 异常: %s", exc)
            return False


__all__ = ["CITY_CODES", "ZhipinCollector"]
