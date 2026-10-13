"""站点采集适配器基类：统一接口（§6）与字段归一化（§8.4）。

每个站点一个适配器类，继承 :class:`Collector` 并实现全部抽象方法；
站点差异（DOM 结构、接口签名、投递链路）只出现在适配器里，服务层
与站点完全无关。RawJob 是各站点归一化后的统一岗位 schema。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# 站点标识 → 中文名（登录中心站点卡片展示用，键对应设计文档 §2.1）
SITES: dict[str, str] = {
    "51job": "前程无忧",
    "zhaopin": "智联招聘",
    "lagou": "拉勾网",
    "liepin": "猎聘",
    "zhipin": "Boss直聘",
}


@dataclass
class RawJob:
    """站点无关的统一岗位数据（设计文档 §8.4 字段归一化 schema）。

    采集器把各站点的原始数据整理成 RawJob，再由服务层（CrawlService）
    调用 :meth:`to_job` 落库。薪资统一为千元/月（INTEGER）。
    """

    source: str                                   # 站点标识（对应 SITES 键）
    job_id: str                                   # 源站岗位 ID
    title: str                                    # 岗位名称
    company: str                                  # 公司名称
    company_size: str = ""                        # 公司规模
    industry: str = ""                            # 行业
    salary_min: int | None = None                 # 月薪下限（千元）
    salary_max: int | None = None                 # 月薪上限（千元）
    salary_raw: str = ""                          # 原始薪资文本
    city: str = ""                                # 城市
    district: str = ""                            # 区
    experience: str = ""                          # 经验要求
    education: str = ""                           # 学历要求
    tags: list[str] = field(default_factory=list)  # 标签（入库时序列化为 JSON 文本）
    description: str = ""                         # 岗位描述（AI 输入源）
    publish_date: str = ""                        # 发布日期
    job_url: str = ""                             # 岗位链接
    crawl_time: str = field(                      # 采集时间（ISO 格式）
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )
    status: str = "active"                        # active / offline
    is_favorite: int = 0                          # 收藏标记
    match_score: float | None = None              # AI 匹配分（缓存）

    def to_job(self):
        """转换为 app.db.models.Job 实例（供落库）。

        app.db.models 尚未就绪时返回 None 并记录 warning，不阻断采集流程。
        """
        try:
            from app.db.models import Job
        except Exception as exc:  # noqa: BLE001 - 模型模块未就绪
            logger.warning(
                "无法导入 app.db.models.Job（%s），RawJob 不转换：source=%s job_id=%s",
                exc,
                self.source,
                self.job_id,
            )
            return None
        return Job(
            source=self.source,
            job_id=self.job_id,
            title=self.title,
            company=self.company,
            company_size=self.company_size,
            industry=self.industry,
            salary_min=self.salary_min,
            salary_max=self.salary_max,
            salary_raw=self.salary_raw,
            city=self.city,
            district=self.district,
            experience=self.experience,
            education=self.education,
            tags=json.dumps(self.tags, ensure_ascii=False),  # §8.4：tags 列存 JSON 文本
            description=self.description,
            publish_date=self.publish_date,
            job_url=self.job_url,
            crawl_time=self.crawl_time,
            status=self.status,
            is_favorite=self.is_favorite,
            match_score=self.match_score,
        )


# ---------------------------------------------------------------------------
# 薪资解析（统一单位：千元/月，取整）
# ---------------------------------------------------------------------------

# 全角数字/符号 → 半角，便于统一正则
_FULLWIDTH_MAP = str.maketrans("０１２３４５６７８９．－～", "0123456789.-~")

# 单位 → 换算为“千元”的倍率
_UNIT_FACTOR: dict[str, float] = {"万": 10.0, "千": 1.0, "k": 1.0}

# 正则分支 1：区间，如 "15-25K"、"1.5-2万/月"、"8000-12000元/月"、"8千-1.2万"
# （每个数字可带自己的单位；缺省单位继承另一端；都不带单位且带“元”则按元换算）
_SALARY_RANGE_RE = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*(?P<ua>万|千|k)?\s*[-~—–至]\s*"
    r"(?P<b>\d+(?:\.\d+)?)\s*(?P<ub>万|千|k)?\s*(?:元\s*/\s*月|/月|元)?"
)
# 正则分支 2：下限，如 "10K以上"、"1.2万以上"、"8000元以上"
_SALARY_ABOVE_RE = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*(?P<u>万|千|k)?\s*(?:元)?\s*以上"
)
# 正则分支 3：上限，如 "8千以下"
_SALARY_BELOW_RE = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*(?P<u>万|千|k)?\s*(?:元)?\s*以下"
)
# 正则分支 4：单值，如 "1万/月"、"15000元/月"、"8k"
_SALARY_SINGLE_RE = re.compile(
    r"(?P<a>\d+(?:\.\d+)?)\s*(?P<u>万|千|k)?\s*(?:元\s*/\s*月|元|/月)?"
)


def _to_qian(value: float, unit: str | None, is_yuan: bool) -> int:
    """把匹配到的数值换算为千元/月并取整。

    :param value: 原始数值
    :param unit: 单位（万/千/k），None 表示未标注
    :param is_yuan: 是否按“元”换算（除以 1000）
    """
    if is_yuan:
        return int(round(value / 1000.0))
    return int(round(value * _UNIT_FACTOR.get(unit, 1.0)))


def normalize_salary(text: str) -> tuple[int | None, int | None]:
    """解析中文常见薪资格式为统一单位（千元/月，取整）。

    正则分支（按顺序尝试）：
    1. 区间："15-25K"、"1.5-2万/月"、"8000-12000元/月"、"8千-1.2万"、
       "10-15K*14薪"（单位缺省继承另一端；"年薪" 自动折算为月薪 ÷12）；
    2. 下限："10K以上"、"1.2万以上"、"8000元以上" → (值, None)；
    3. 上限："8千以下" → (None, 值)；
    4. 单值："1万/月"、"15000元/月"、"8k" → (值, 值)；
    5. 面议/面谈/无法识别 → (None, None)。

    测试用例（示例输入输出）：
    - normalize_salary("15-25K")         == (15, 25)
    - normalize_salary("1.5-2万/月")     == (15, 20)
    - normalize_salary("面议")           == (None, None)
    - normalize_salary("10K以上")        == (10, None)
    - normalize_salary("8000-12000元/月") == (8, 12)

    :param text: 原始薪资文本，如 "15-25K"、"1.5-2万/月"、"面议"
    :return: (salary_min, salary_max)，单位千元/月；无法解析时为 (None, None)
    """
    if not text:
        return None, None
    s = text.strip().lower().replace(" ", "").replace("\u3000", "").translate(_FULLWIDTH_MAP)
    if not s or "面议" in s or "面谈" in s:
        return None, None

    # 年薪 → 折算为月薪（除以 12）
    annual = "年薪" in s and "月薪" not in s

    # 分支 1：区间
    m = _SALARY_RANGE_RE.search(s)
    if m:
        ua = m.group("ua") or m.group("ub")
        ub = m.group("ub") or m.group("ua")
        # 两端都无单位但文本带“元” → 按元换算（除以 1000）
        is_yuan = ("元" in m.group(0)) and not (m.group("ua") or m.group("ub"))
        low = _to_qian(float(m.group("a")), ua, is_yuan)
        high = _to_qian(float(m.group("b")), ub, is_yuan)
        if annual:
            low, high = max(low // 12, 1), max(high // 12, 1)
        if low > high:
            low, high = high, low
        return low, high

    # 分支 2：下限（X 以上）
    m = _SALARY_ABOVE_RE.search(s)
    if m:
        is_yuan = ("元" in m.group(0)) and not m.group("u")
        low = _to_qian(float(m.group("a")), m.group("u"), is_yuan)
        if annual:
            low = max(low // 12, 1)
        return low, None

    # 分支 3：上限（X 以下）
    m = _SALARY_BELOW_RE.search(s)
    if m:
        is_yuan = ("元" in m.group(0)) and not m.group("u")
        high = _to_qian(float(m.group("a")), m.group("u"), is_yuan)
        if annual:
            high = max(high // 12, 1)
        return None, high

    # 分支 4：单值
    m = _SALARY_SINGLE_RE.search(s)
    if m:
        is_yuan = ("元" in m.group(0)) and not m.group("u")
        value = _to_qian(float(m.group("a")), m.group("u"), is_yuan)
        if annual:
            value = max(value // 12, 1)
        return value, value

    logger.debug("未能解析薪资文本：%r", text)
    return None, None


# ---------------------------------------------------------------------------
# Collector 抽象基类（设计文档 §6 统一接口）
# ---------------------------------------------------------------------------


class Collector(ABC):
    """站点采集适配器抽象基类（对应设计文档 §6 统一接口）。

    每个站点一个子类，实现本站点全部抽象方法；服务层只依赖本接口，
    与具体站点无关。所有方法均为 async，页面对象由 BrowserManager 提供。
    """

    source: str = ""  # 站点标识（对应 SITES 键）

    # 通用登录特征 Cookie 名（精确匹配；各站可扩展，见 _cookie_logged_in）。
    # 注意：只收录"登录后才有"的标识型 Cookie（uid/userid 等），
    # 不要收录 token/guid 等匿名也会种下的名称，否则会误判已登录。
    COMMON_LOGIN_COOKIES: frozenset = frozenset(
        {"uid", "userid", "user_id", "memberid", "member_id"}
    )

    # ------------------------------------------------------------- 登录

    async def _cookie_logged_in(self, page, extra_names: set | None = None) -> bool:
        """Cookie 辅助信号：存在登录特征 Cookie 即视为已登录。

        DOM 选择器可能因站点改版失效，Cookie 是独立且可靠的登录判定通道。
        只做精确名匹配，避免把匿名分析类 Cookie（guid/sensor/stoken 等）误判为登录态。
        """
        names = set(self.COMMON_LOGIN_COOKIES) | (extra_names or set())
        if not names:
            return False
        try:
            cookies = await page.context.cookies()
            cookie_names = {c.get("name", "") for c in cookies}
            return bool(names & cookie_names)
        except Exception:  # noqa: BLE001
            return False

    @abstractmethod
    async def goto_login(self, page) -> None:
        """打开本站点登录页。"""
        ...

    @abstractmethod
    async def is_logged_in(self, page) -> bool:
        """判断当前会话是否有效（访问个人中心，被重定向到登录页即未登录）。"""
        ...

    @abstractmethod
    async def login_qr(self, page, qr_callback) -> bool:
        """扫码登录。

        :param page: 当前页面
        :param qr_callback: 二维码出现时回调 qr_callback(qr_path) 供 GUI 展示；
            回调在二维码截图保存后触发
        :return: 登录成功返回 True；超时返回 False
        """
        ...

    @abstractmethod
    async def login_sms(self, page, phone: str, code: str) -> bool:
        """短信验证码登录。

        :param page: 当前页面
        :param phone: 手机号
        :param code: 验证码
        :return: 登录成功返回 True
        """
        ...

    # ------------------------------------------------------------- 采集

    @abstractmethod
    async def search_jobs(
        self, page, keyword: str, city: str | None, max_pages: int = 3
    ) -> list[RawJob]:
        """按关键词+城市搜索岗位并翻页采集，返回归一化岗位列表。

        :param page: 当前页面
        :param keyword: 搜索关键词（如 "Java开发"）
        :param city: 城市（如 "北京"），None 表示不限
        :param max_pages: 最多翻页数，默认 3
        :return: RawJob 列表（仅列表字段，详情待 parse_job 补充）
        """
        ...

    @abstractmethod
    async def parse_job(self, page, job_url: str) -> RawJob | None:
        """打开岗位详情页解析为 RawJob；解析失败返回 None。

        :param page: 当前页面
        :param job_url: 岗位详情页链接
        """
        ...

    # ------------------------------------------------------------- 投递

    @abstractmethod
    async def open_apply(self, page, job: RawJob) -> None:
        """打开投递页并预填（半自动流程，最终提交由用户确认）。

        :param page: 当前页面
        :param job: 待投递岗位
        """
        ...

    @abstractmethod
    async def submit_apply(self, page, resume_name: str) -> bool:
        """执行投递最后一步提交（半自动触发）。

        :param page: 当前页面
        :param resume_name: 使用的简历版本名
        :return: 提交成功返回 True
        """
        ...

    @abstractmethod
    async def send_greeting(self, page, job: RawJob, text: str) -> bool:
        """Boss 直聘专用：给 HR 发送打招呼语/消息。

        :param page: 当前页面
        :param job: 目标岗位
        :param text: 招呼语文本（可 AI 生成）
        :return: 发送成功返回 True
        """
        ...
