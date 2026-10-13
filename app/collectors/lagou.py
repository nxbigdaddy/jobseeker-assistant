# -*- coding: utf-8 -*-
"""
拉勾网（lagou）站点采集器。

选择器基于拉勾公开页面结构（www.lagou.com/wn/jobs 搜索列表、
/wn/jobs/{id}.html 岗位详情、passport.lagou.com/login/login.html 登录页），
改版时优先调整此处（类属性"选择器常量区"），避免散落修改。

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

# 拉勾 URL 直接用中文城市名：已知城市原样映射，未知城市回退 "全国"（不传 city 参数）
CITY_ALIASES: dict[str, str] = {
    "北京": "北京",
    "上海": "上海",
    "广州": "广州",
    "深圳": "深圳",
    "杭州": "杭州",
    "成都": "成都",
    "武汉": "武汉",
    "南京": "南京",
    "西安": "西安",
    "苏州": "苏州",
    "天津": "天津",
    "重庆": "重庆",
}


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


# =====================================================================
# 采集器主体
# =====================================================================
class LagouCollector(_BaseCollector):
    """拉勾网（lagou）站点采集器，实现 Collector 抽象接口。"""

    source = "lagou"

    # =====================================================================
    # 选择器常量区（类属性）—— 站点改版时优先在此调整
    # =====================================================================

    # ---- 登录页（passport.lagou.com/login/login.html）----
    SEL_LOGIN_QR_TAB = [  # 扫码登录入口（不稳定，可能不存在）
        "text=扫码登录",
        ".qrcode-login",
        ".qrcode",
    ]
    SEL_QR_IMAGE = [  # 二维码图片 / 画布
        ".qrcode img",
        ".qrcode canvas",
        ".qrcode",
        "img[src*='qr']",
        "canvas",
    ]
    SEL_SMS_TAB = [  # 切"验证码登录/密码登录"标签
        "text=验证码登录",
        ".input_code",
        "text=密码登录",
    ]
    SEL_PHONE_INPUT = [  # 手机号输入框（name/placeholder 含"手机"）
        "input[name*='phone']",
        "input[placeholder*='手机']",
        "input[name*='mobile']",
    ]
    SEL_GET_CODE_BTN = [
        "text=获取验证码",
        "button:has-text('获取验证码')",
        ".get-code",
    ]
    SEL_CODE_INPUT = [
        "input[name*='code']",
        "input[placeholder*='验证码']",
        "input[placeholder*='短信']",
    ]
    SEL_LOGIN_BTN = [
        "button[type='submit']",
        ".login-btn",
        "button:has-text('登录')",
        "text=登录",
    ]
    SEL_LOGIN_FORM_HINT = [  # 页面出现登录表单 = 未登录
        "#loginForm",
        ".login-form",
        ".input_code",
        "input[type='password']",
        "input[placeholder*='密码']",
    ]
    SEL_LOGGED_OUT_HINT = [  # 出现"登录/注册"类入口 = 未登录
        "a[href*='passport.lagou.com']",
        "a[href*='/login']",
        "text=登录/注册",
        "text=立即登录",
    ]
    SEL_USER_MENU = [  # 已登录用户菜单（用户名/头像）
        # 注意：只用结构化 class 选择器，勿加 text= 文本项——
        # 未登录页面也常有"个人中心/退出"等文字链接，会造成登录态误判
        ".user-card",
        ".username",
        ".user-name",
        ".header-user",
        ".jobseeker-name",
        ".header-userinfo",
        ".user-menu",
    ]

    # ---- 搜索列表页（/wn/jobs，SPA 滚动加载）----
    SEL_LIST_CONTAINER = [".item__wrap", ".position-item__info", ".job-list-box"]
    SEL_LIST_ITEM = [
        ".item__wrap",
        ".position-item__info",
        ".job-list-box li",
        ".job-list-box .job-item",
        ".job-list li",
    ]
    SEL_JOB_TITLE = [".item__position-name", ".position-item__name", "h3"]
    SEL_JOB_LINK = [
        "a[href*='/wn/jobs/']",
        "a[href*='/jobs/']",
        "a[href*='positionId']",
        "a",
    ]
    SEL_COMPANY = [".item__company-name", ".company-name", ".company", ".item__company"]
    SEL_SALARY = [".item__salary", ".salary", ".position-salary"]
    SEL_LABELS = [".item__labels", ".labels", ".position-labels"]
    SEL_TIME = [".item__time", ".time", ".publish-time"]
    SEL_LOAD_MORE = [
        "text=加载更多",
        ".load-more",
        "button:has-text('加载更多')",
        ".btn-loadmore",
    ]

    # ---- 岗位详情页（/wn/jobs/{id}.html）----
    SEL_JOB_DETAIL = [".job-detail", ".job-description", "#job-detail"]
    SEL_JOB_DESC = [".job-description", ".job-detail", "#job-detail", ".job-require"]
    SEL_DETAIL_TITLE = ["h1", ".job-name", ".position-head h1", ".job-detail h1"]
    SEL_DETAIL_COMPANY = [
        ".company-name",
        ".job-company",
        ".company",
        "a[href*='/company/']",
    ]
    SEL_DETAIL_SALARY = [".job-salary", ".salary", ".item__salary", ".position-salary"]
    SEL_DETAIL_LABELS = [".job-require", ".job-labels", ".position-labels"]

    # ---- 投递 ----
    SEL_APPLY_BTN = [  # 详情页"投递"按钮
        ".btn-panel button:has-text('投递')",
        "button:has-text('投递')",
        "a:has-text('投递')",
        ".btn-deliver",
        "text=投递",
    ]
    SEL_APPLY_DIALOG = [
        ".resume-list",
        ".apply-dialog",
        ".deliver-dialog",
        ".el-dialog",
        ".modal",
    ]
    SEL_RESUME_OPTION = [
        ".resume-list .item",
        ".resume-list li",
        ".resume-list .resume-item",
        ".resume-item",
        "input[type='radio']",
    ]
    SEL_SUBMIT_BTN = [
        "button:has-text('确认投递')",
        ".btn:has-text('投递')",
        "text=确认投递",
        "button:has-text('投递')",
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
    LOAD_MORE_WAIT_MS = 1500
    MAX_SCROLL_TIMES = 2
    _SCROLL_JS = """() => {
        window.scrollTo(0, document.body.scrollHeight);
        const els = document.querySelectorAll('.job-list-box, .list-box, .content-box, main');
        for (const el of els) {
            if (el.scrollHeight > el.clientHeight + 1) el.scrollTop = el.scrollHeight;
        }
    }"""

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
    # 城市 / 字段解析辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _city_param(city: str | None) -> str:
        """把城市名映射为拉勾 URL 参数值；未知/空城市返回 "全国"（调用方不传 city 参数）。"""
        if not city:
            return "全国"
        city = city.strip()
        alias = CITY_ALIASES.get(city)
        if alias:
            return alias
        if city.endswith("市") and len(city) > 1:  # 容错："北京市" → "北京"
            alias = CITY_ALIASES.get(city[:-1])
            if alias:
                return alias
        return "全国"

    @staticmethod
    def _extract_job_id(url: str) -> str:
        """从岗位链接中提取 job_id；取不到时退回 URL 数字段。"""
        if not url:
            return ""
        m = re.search(r"/jobs/(\d+)", url)
        if m:
            return m.group(1)
        m = re.search(r"positionId[=/](\d+)", url, re.IGNORECASE)
        if m:
            return m.group(1)
        tail = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0].split("#")[0]
        m = re.search(r"(\d+)", tail)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _split_area(text: str) -> tuple[str, str]:
        """从标签整块文本中提取城市/区，如 "北京·海淀" -> ("北京", "海淀")；无则 ("", "")。"""
        if not text:
            return "", ""
        for seg in re.split(r"[\s\n]+", text.strip()):
            seg = seg.strip()
            if not seg:
                continue
            parts = [p.strip() for p in re.split(r"[·\-]", seg) if p.strip()]
            if not parts:
                continue
            head = parts[0]
            if head in CITY_ALIASES:
                city = head
                district = ""
                if len(parts) >= 2 and re.fullmatch(r"[\u4e00-\u9fa5]{2,6}", parts[1]):
                    district = parts[1]
                return city, district
            if re.fullmatch(r"[\u4e00-\u9fa5]{2,6}", seg) and seg.endswith(("区", "县")):
                return seg, ""
        return "", ""

    @staticmethod
    def _is_city_segment(text: str) -> bool:
        """判断标签 span 是否为城市/区段（如 "北京·海淀"、"北京"、"海淀区"），避免混入 tags。"""
        if not text:
            return False
        head = re.split(r"[·\-]", text, maxsplit=1)[0].strip()
        if head in CITY_ALIASES:
            return True
        if text in CITY_ALIASES:
            return True
        if re.fullmatch(r"[\u4e00-\u9fa5]{2,6}(区|县)", text):
            return True
        return False

    @staticmethod
    def _classify_tag(text: str) -> str:
        """把标签文本归类：experience / education / tag（"经验""学历"关键词，其余为 tags）。"""
        if "经验" in text or re.search(r"^(\d+[-~至]?\d*年|应届|在校/应届|无经验)", text):
            return "experience"
        if any(k in text for k in ("大专", "本科", "硕士", "博士", "学历")):
            return "education"
        return "tag"

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------
    async def goto_login(self, page: Page) -> None:
        """打开拉勾登录页（passport.lagou.com）。"""
        try:
            await page.goto(
                "https://passport.lagou.com/login/login.html",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(800)
        except Exception as exc:  # noqa: BLE001
            logger.error("goto_login 失败: %s", exc)

    async def is_logged_in(self, page: Page) -> bool:
        """判断登录态：访问首页，被重定向到 passport 或出现登录表单即未登录；异常返回 False。"""
        try:
            await page.goto(
                "https://www.lagou.com/", wait_until="domcontentloaded", timeout=30000
            )
            await page.wait_for_timeout(1500)
            url = (page.url or "").lower()
            if "passport" in url or "login" in url:
                return False
            if await _first(page, *self.SEL_USER_MENU) is not None:
                return True
            if await _first(page, *self.SEL_LOGIN_FORM_HINT) is not None:
                return False
            if await _first(page, *self.SEL_LOGGED_OUT_HINT) is not None:
                return False
            if await self._cookie_logged_in(page):
                return True
            # 保守判定：无已登录证据即视为未登录
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("is_logged_in 异常: %s", exc)
            return False

    async def login_qr(self, page: Page, qr_callback) -> bool:
        """扫码登录（拉勾扫码入口不稳定）。

        先尝试寻找"扫码登录"入口；找不到则直接返回 False 并提示使用短信登录。
        找到则截图二维码回调 qr_callback(qr_path) 供 GUI 展示，随后轮询登录结果。
        """
        try:
            current = (page.url or "").lower()
            if "login" not in current and "passport" not in current:
                await self.goto_login(page)
            tab = await _first(page, *self.SEL_LOGIN_QR_TAB)
            if tab is None:
                logger.info("拉勾建议使用短信登录（未找到扫码登录入口）")
                return False
            try:
                await tab.click(timeout=5000)
                await page.wait_for_timeout(800)
            except Exception as exc:  # noqa: BLE001
                logger.debug("点击扫码入口失败，忽略: %s", exc)
            try:
                await page.wait_for_selector(_css_join(self.SEL_QR_IMAGE), timeout=10000)
            except Exception:  # noqa: BLE001
                pass
            qr = await _first(page, *self.SEL_QR_IMAGE)
            if qr is None:
                logger.info("拉勾建议使用短信登录（未找到二维码元素）")
                return False
            try:
                _DATA_DIR.mkdir(parents=True, exist_ok=True)
                qr_path = str(
                    _DATA_DIR
                    / f"qr_lagou_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                )
                await qr.screenshot(path=qr_path)
                if callable(qr_callback):
                    qr_callback(qr_path)
                logger.info("二维码已截图: %s", qr_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("二维码截图/回调失败: %s", exc)
            # 轮询登录状态
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
        """短信验证码登录：切标签 -> 填手机号 -> 获取验证码 -> 填验证码 -> 登录 -> 轮询结果。"""
        try:
            current = (page.url or "").lower()
            if "login" not in current and "passport" not in current:
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
        """按关键词+城市搜索岗位并滚动加载采集，返回 RawJob 列表；异常/未登录返回 []。"""
        try:
            base = self._load_base()
            if base is None:
                logger.error("app.collectors.base 未就绪，search_jobs 返回空列表")
                return []
            # 城市直接用中文名；未知或"全国"时不传 city 参数
            city_param = self._city_param(city)
            url = f"https://www.lagou.com/wn/jobs?kd={quote(keyword)}"
            if city_param and city_param != "全国":
                url += f"&city={quote(city_param)}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # SPA 渲染等待（最多 30 秒）
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_LIST_CONTAINER), timeout=30000
                )
            except Exception:  # noqa: BLE001
                logger.warning("30 秒内未等到列表容器（可能改版或无结果）: %s", url)
            now_url = (page.url or "").lower()
            if "passport" in now_url or "login" in now_url:
                logger.error("搜索页被重定向到登录页，请先登录后再采集")
                return []
            results: list = []
            seen: set = set()
            await self._collect_items(page, base, city, results, seen)
            # 翻页：点击"加载更多"最多 max_pages-1 次，无按钮则滚动到底最多 2 次
            clicks = 0
            scrolls = 0
            max_clicks = max(0, max_pages - 1)
            while clicks < max_clicks:
                more = await _first(page, *self.SEL_LOAD_MORE)
                if more is not None:
                    try:
                        await more.click(timeout=5000)
                        clicks += 1
                        await page.wait_for_timeout(self.LOAD_MORE_WAIT_MS)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("点击'加载更多'失败: %s", exc)
                        break
                else:
                    if scrolls >= self.MAX_SCROLL_TIMES:
                        break
                    scrolls += 1
                    try:
                        await page.evaluate(self._SCROLL_JS)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("滚动到底失败: %s", exc)
                        break
                    await page.wait_for_timeout(self.LOAD_MORE_WAIT_MS)
                added = await self._collect_items(page, base, city, results, seen)
                if added == 0:
                    logger.info("翻页后无新增岗位，停止加载")
                    break
            logger.info(
                "search_jobs 完成 keyword=%s city=%s 条数=%s", keyword, city, len(results)
            )
            return results
        except Exception as exc:  # noqa: BLE001
            logger.error("search_jobs 异常: %s", exc)
            return []

    async def _collect_items(self, page: Page, base, city: str | None, results: list, seen: set) -> int:
        """抓取当前列表全部条目并入 results（按 job_id 去重），返回新增条数。"""
        added = 0
        items_loc = await _match(page, *self.SEL_LIST_ITEM)
        if items_loc is None:
            return 0
        try:
            items = await items_loc.all()
        except Exception as exc:  # noqa: BLE001
            logger.debug("获取列表项失败: %s", exc)
            return 0
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
            added += 1
        return added

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
        # 岗位链接与 job_id（title 元素本身可能是 <a>，先取它，再按 href 特征兜底）
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
            job_url = urljoin("https://www.lagou.com/", job_url)
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
        # 城市 / 区 / 经验 / 学历 / 标签（.item__labels 内 span 归类）
        parsed_city, district, experience, education, tags = await self._parse_labels(
            page, item, city
        )
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

    async def _parse_labels(self, page: Page, item, city_hint: str | None):
        """从 .item__labels 解析 (city, district, experience, education, tags)。"""
        city, district, experience, education = "", "", "", ""
        tags: list[str] = []
        labels_loc = await _match(item, *self.SEL_LABELS)
        if labels_loc is None:
            if city_hint:
                city = city_hint.strip()
            return city, district, experience, education, tags
        full_text = ""
        try:
            full_text = (await labels_loc.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            full_text = ""
        city, district = self._split_area(full_text)
        if not city and city_hint:
            city = city_hint.strip()
        spans = []
        try:
            spans = await labels_loc.locator("span").all()
        except Exception:  # noqa: BLE001
            spans = []
        for sp in spans:
            try:
                t = (await sp.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not t or self._is_city_segment(t):
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
        return city, district, experience, education, tags

    async def parse_job(self, page: Page, job_url: str):
        """打开岗位详情页解析完整信息并回填字段；详情需登录或解析失败返回 None。"""
        try:
            base = self._load_base()
            if base is None:
                logger.error("app.collectors.base 未就绪，parse_job 返回 None")
                return None
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(800)
            now_url = (page.url or "").lower()
            if "passport" in now_url or "login" in now_url:
                logger.warning("详情页被重定向到登录页，parse_job 返回 None: %s", job_url)
                return None
            try:
                await page.wait_for_selector(
                    _css_join(self.SEL_JOB_DETAIL), timeout=15000
                )
            except Exception:  # noqa: BLE001
                logger.warning("详情页容器未等到（可能已改版或需登录）: %s", job_url)
                return None
            # 岗位描述
            description = ""
            desc_el = await _first(page, *self.SEL_JOB_DESC)
            if desc_el is not None:
                try:
                    description = (await desc_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    description = ""
            # 标题 / 公司 / 薪资（详情更全则覆盖列表字段）
            title = ""
            t_el = await _first(page, *self.SEL_DETAIL_TITLE)
            if t_el is not None:
                try:
                    title = (await t_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    title = ""
            company = ""
            c_el = await _first(page, *self.SEL_DETAIL_COMPANY)
            if c_el is not None:
                try:
                    company = (await c_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    company = ""
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
            # 城市 / 区（详情页有标签区则尽力解析）
            city, district = "", ""
            labels_el = await _first(page, *self.SEL_DETAIL_LABELS)
            if labels_el is not None:
                try:
                    label_text = (await labels_el.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    label_text = ""
                city, district = self._split_area(label_text)
            job_id = self._extract_job_id(job_url)
            return self._make_raw_job(
                job_id=job_id,
                title=title,
                company=company,
                salary_min=salary_min,
                salary_max=salary_max,
                salary_raw=salary_raw,
                city=city,
                district=district,
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
        """详情页点"投递"按钮打开投递弹窗（只打开，不提交）。"""
        try:
            job_url = getattr(job, "job_url", "") or ""
            if not job_url:
                logger.warning("job 缺少 job_url，open_apply 跳过")
                return
            await page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(800)
            try:
                await page.wait_for_selector(_css_join(self.SEL_APPLY_BTN), timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            btn = await _first(page, *self.SEL_APPLY_BTN)
            if btn is None:
                logger.warning("未找到'投递'按钮: %s", job_url)
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
        """在投递弹窗中按简历名匹配简历并确认投递；出现成功提示或弹窗消失返回 True。"""
        try:
            # 简历选择：优先按 resume_name 文本匹配，匹配不到点默认项
            chosen = False
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
            # 点击最终"确认投递"按钮
            submit_btn = await _first(page, *self.SEL_SUBMIT_BTN)
            if submit_btn is not None:
                try:
                    await submit_btn.click(timeout=5000)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("点击'确认投递'失败: %s", exc)
            # 成功判定：出现"投递成功"类提示
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
            # 成功提示未出现：弹窗（或确认按钮）消失也视为成功
            dialog = await _first(page, *self.SEL_APPLY_DIALOG)
            if dialog is not None:
                try:
                    await dialog.wait_for(state="hidden", timeout=5000)
                    logger.info("submit_apply 成功（投递弹窗已消失）")
                    return True
                except Exception:  # noqa: BLE001
                    pass
            else:
                if submit_btn is not None:
                    try:
                        await submit_btn.wait_for(state="hidden", timeout=5000)
                        logger.info("submit_apply 成功（确认按钮已消失）")
                        return True
                    except Exception:  # noqa: BLE001
                        pass
            logger.warning("submit_apply 未能确认投递结果")
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("submit_apply 异常: %s", exc)
            return False

    async def send_greeting(self, page: Page, job, text: str) -> bool:
        """拉勾网无打招呼/私信流程，直接跳过。"""
        logger.info("拉勾网不支持打招呼，已跳过")
        return False


__all__ = ["CITY_ALIASES", "LagouCollector"]
