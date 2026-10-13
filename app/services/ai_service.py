# -*- coding: utf-8 -*-
"""大模型（LLM）服务模块 AiService。

对应《系统设计文档》§3.1 的 AI 功能清单（A1/A2/A3/A4/A5/A6/A7/A8/A9）与 §11 的模块设计：

- 配置（base_url / api_key / model / temperature / enabled）从数据库 ai_settings 表读取；
- 提示词（系统角色 + 用户模板）优先取数据库 prompts 表，缺失或禁用时回退到内置模板；
- 通过 openai 兼容 SDK（DeepSeek / 通义 / Ollama 等）调用大模型，同步调用包在
  ``asyncio.to_thread`` 中执行，支持超时、指数退避重试与 json 模式降级；
- 所有依赖（openai、db 层、config）均为延迟导入，未安装 openai 或数据库不可用时
  不影响模块导入，仅在使用相关功能时报错。

依赖：仅标准库（asyncio / json / logging / re / threading）+ 延迟导入的第三方依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading

logger = logging.getLogger(__name__)

__all__ = ["AiService"]

# ---------------------------------------------------------------------------
# 内置提示词（数据库 prompts 表缺失/禁用时的回退）
# ---------------------------------------------------------------------------

# 系统角色设定：{feature: 中文角色设定}
ROLE_MAP: dict[str, str] = {
    "resume_optimize": "你是资深 HR 与简历优化顾问，擅长把求职者简历改写为针对目标岗位亮点突出、关键词匹配的高质量简历。",
    "match_score": "你是资深招聘专家，擅长客观评估求职者与目标岗位的匹配程度并指出短板。",
    "apply_strategy": "你是招聘平台风控与投递策略分析师，擅长在降低账号风险的前提下给出保守、可执行的投递策略。",
    "generate_greeting": "你是求职沟通顾问，擅长根据平台风格撰写简洁真诚的打招呼语或正式求职信。",
    "recommend_jobs": "你是资深职业顾问，擅长根据求职者简历对岗位列表进行匹配度排序并给出推荐理由。",
    "follow_up": "你是求职沟通顾问，擅长撰写礼貌、专业、不催促的投递跟进话术。",
    "interview_predict": "你是资深面试教练，擅长根据岗位 JD 与简历预测高频面试问题并给出回答要点。",
    "salary_analysis": "你是薪酬分析师，擅长解读薪资统计数据并给出求职者个人定位与谈薪建议。",
    "weekly_report": "你是求职教练，擅长复盘每周投递数据并给出改进与下周行动建议。",
}

# 用户提示词模板：{feature: 含 {占位符} 的模板字符串}
FALLBACK_PROMPTS: dict[str, str] = {
    "resume_optimize": (
        "请针对以下目标岗位 JD，对求职者简历进行针对性优化。\n"
        "要求：\n"
        "1. 突出与岗位最相关的经历与技能，补充关键词；\n"
        "2. 给出可直接使用的改写内容或改写建议；\n"
        "3. 指出可删除或弱化的内容；\n"
        "4. 输出 Markdown 分节：亮点改写、关键词补充、删除建议。\n\n"
        "【目标岗位 JD】\n{jd}\n\n"
        "【求职者简历】\n{resume}"
    ),
    "match_score": (
        "请评估求职者与目标岗位的匹配程度。\n"
        "只输出严格 JSON，不要输出其他文字，格式：\n"
        '{"score": 0-100 的整数, "reasons": [匹配理由列表], "gaps": [短板/差距列表]}\n\n'
        "【岗位 JD】\n{jd}\n\n"
        "【简历】\n{resume}"
    ),
    "apply_strategy": (
        "请结合平台风控特点，给出保守的投递策略建议。\n"
        "要求：\n"
        "1. 给出每日每站建议投递上限；\n"
        "2. 给出建议投递时段；\n"
        "3. 给出投递节奏与注意事项（保守优先，避免触发账号风控）。\n\n"
        "【平台】\n{platform}\n\n"
        "【历史投递情况】\n{history}"
    ),
    "generate_greeting": (
        "请根据平台风格生成打招呼语或求职信。\n"
        "要求：\n"
        "1. 若平台是聊天式平台（如 Boss 直聘）：生成简洁真诚的打招呼语，不超过 50 字；\n"
        "2. 其他平台：生成正式求职信；\n"
        "3. 结合岗位 JD 与简历摘要，突出匹配点。\n\n"
        "【平台】\n{platform}\n\n"
        "【岗位 JD】\n{jd}\n\n"
        "【简历摘要】\n{resume}"
    ),
    "recommend_jobs": (
        "以下是近期采集的岗位列表（JSON 数组，每条含 title/company/city/salary_raw/description）。\n"
        "请根据求职者简历对岗位进行匹配度排序，推荐最匹配的岗位。\n"
        "只输出严格 JSON，不要输出其他文字，格式：\n"
        '{"ranked": [按匹配度从高到低排列的岗位下标列表（0 起）]}，最多 {top_n} 个。\n\n'
        "【求职者简历】\n{resume}\n\n"
        "【岗位列表】\n{jobs}"
    ),
    "follow_up": (
        "请根据岗位 JD 与已投递天数，撰写一条得体的跟进话术。\n"
        "要求：\n"
        "1. 语气礼貌、专业、不催促；\n"
        "2. 说明投递时间与意向，表达对岗位的兴趣；\n"
        "3. 简短（150 字以内）。\n\n"
        "【岗位 JD】\n{jd}\n\n"
        "【已投递天数】{days} 天"
    ),
    "interview_predict": (
        "请根据岗位 JD 与求职者简历，预测面试中可能被问到的高频问题。\n"
        "输出 Markdown 列表：每个问题附回答要点与准备建议。\n\n"
        "【岗位 JD】\n{jd}\n\n"
        "【简历】\n{resume}"
    ),
    "salary_analysis": (
        "请基于本地采集的薪资统计数据，分析该岗位的薪资行情，并给出个人定位建议。\n"
        "要求：\n"
        "1. 解读薪资分布（区间、中位数、与所在城市水平对比）；\n"
        "2. 给出求职者的合理期望定位与谈薪建议。\n\n"
        "【岗位关键词】\n{keyword}\n\n"
        "【城市】\n{city}\n\n"
        "【本地统计数据摘要】\n{stats}"
    ),
    "weekly_report": (
        "请基于以下一周的投递/查看/面试记录，生成周度复盘报告。\n"
        "要求：\n"
        "1. 总结本周投递情况（数量、查看率、回复率、面试数等）；\n"
        "2. 指出问题与改进建议；\n"
        "3. 给出下周行动建议。\n\n"
        "【本周记录摘要】\n{records}"
    ),
}

# 占位符 {name} 匹配：仅替换简单标识符，避免 format_map 的 KeyError
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class AiService:
    """个人求职助手的大模型服务（单例）。"""

    _instance: "AiService | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        # 以 (base_url, api_key) 为键缓存 OpenAI 客户端，配置变更后自动重建
        self._client_cache: dict[tuple[str, str], object] = {}

    # ------------------------------------------------------------------
    # 单例与配置
    # ------------------------------------------------------------------

    @classmethod
    def get(cls) -> "AiService":
        """获取全局单例。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _settings(self) -> object | None:
        """延迟读取 AI 配置；任何失败（依赖缺失/DB 不可用/无记录）返回 None。"""
        try:
            from app.db.repository import get_ai_settings
            from app.db.database import get_connection
        except Exception as exc:  # 依赖模块尚未就绪
            logger.warning("无法导入 AI 配置依赖: %s", exc)
            return None
        try:
            conn = get_connection()
            try:
                return get_ai_settings(conn)
            finally:
                close = getattr(conn, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning("读取 AI 配置失败: %s", exc)
            return None

    def is_configured(self) -> bool:
        """是否已配置可用的大模型 API（enabled 且 api_key/base_url 非空）。

        数据库不可用时返回 False。
        """
        settings = self._settings()
        if settings is None:
            return False
        try:
            enabled = bool(getattr(settings, "enabled", False))
            api_key = YOUR_SECRET, "api_key", "") or "").strip()
            base_url = str(getattr(settings, "base_url", "") or "").strip()
            return enabled and bool(api_key) and bool(base_url)
        except Exception:
            return False

    def _ensure_configured(self) -> None:
        """未配置时抛 RuntimeError，由 GUI 捕获展示。"""
        if not self.is_configured():
            raise RuntimeError("未配置大模型 API，请在 AI 设置页配置")

    def _client(self) -> object:
        """懒加载 OpenAI 兼容客户端（按 base_url+api_key 缓存）。"""
        settings = self._settings()
        if settings is None:
            raise RuntimeError("未配置大模型 API，请在 AI 设置页配置")
        base_url = str(getattr(settings, "base_url", "") or "").strip()
        api_key = YOUR_SECRET, "api_key", "") or "").strip()
        if not base_url or not api_key:
            YOUR_SECRET RuntimeError("未配置大模型 API，请在 AI 设置页配置")
        key = (base_url, api_key)
        cached = self._client_cache.get(key)
        if cached is not None:
            return cached
        try:
            from openai import OpenAI
        except ImportError:
            raise RuntimeError("openai 未安装，请 pip install openai") from None
        client = OpenAI(base_url=base_url, api_key=YOUR_SECRET)
        self._client_cache[key] = client
        return client

    # ------------------------------------------------------------------
    # 配置常量（延迟读取，缺省与系统设计一致：30s / 2 次 / 0.7）
    # ------------------------------------------------------------------

    @staticmethod
    def _llm_timeout() -> float:
        try:
            from app.config.settings import LLM_TIMEOUT_SECONDS
            return float(LLM_TIMEOUT_SECONDS)
        except Exception:
            return 30.0

    @staticmethod
    def _llm_retries() -> int:
        try:
            from app.config.settings import LLM_RETRIES
            return int(LLM_RETRIES)
        except Exception:
            return 2

    @staticmethod
    def _default_temperature() -> float:
        try:
            from app.config.settings import DEFAULT_LLM_TEMPERATURE
            return float(DEFAULT_LLM_TEMPERATURE)
        except Exception:
            return 0.7

    # ------------------------------------------------------------------
    # 提示词
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_format(text: str, kwargs: dict) -> str:
        """安全填充模板占位符：{name} -> kwargs.get(name, '')，缺键不抛异常。"""
        if not text:
            return ""

        def _repl(match: "re.Match[str]") -> str:
            value = kwargs.get(match.group(1), "")
            return str(value)

        return _PLACEHOLDER_RE.sub(_repl, text)

    def _load_template(self, feature: str) -> str:
        """读取数据库 prompts 表模板；缺失/禁用/为空时回退内置模板。"""
        try:
            from app.db.repository import get_prompt
            from app.db.database import get_connection
            conn = get_connection()
            try:
                prompt = get_prompt(conn, feature)
            finally:
                close = getattr(conn, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            if prompt is not None:
                enabled = getattr(prompt, "enabled", True)
                if enabled is not False:  # 仅显式 disabled 才禁用
                    content = str(getattr(prompt, "content", "") or "").strip()
                    if content:
                        return content
        except Exception as exc:
            logger.warning("读取提示词 %s 失败，使用内置模板: %s", feature, exc)
        return FALLBACK_PROMPTS.get(feature, "")

    def _fill(self, feature: str, **kwargs) -> tuple[str, str]:
        """构造 (system, user)：系统角色取自 ROLE_MAP，用户模板取 DB 或内置。"""
        system = ROLE_MAP.get(feature, "你是专业的求职助手 AI 顾问。")
        template = self._load_template(feature)
        user = self._safe_format(template, kwargs)
        return system, user

    # ------------------------------------------------------------------
    # 大模型调用
    # ------------------------------------------------------------------

    @staticmethod
    def _call_sync(
        client: object,
        model: str,
        system: str,
        user: str,
        json_mode: bool,
        temperature: float,
    ) -> str:
        """同步调用 openai 兼容 SDK（在 to_thread 中执行），返回 message.content。"""
        kwargs: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(**kwargs)
        if not resp.choices:
            return ""
        content = resp.choices[0].message.content
        return content or ""

    @staticmethod
    def _is_bad_request(exc: BaseException) -> bool:
        """判断是否为模型不支持参数（如 response_format）导致的错误。"""
        try:
            from openai import BadRequestError
            return isinstance(exc, BadRequestError)
        except Exception:
            return False

    async def chat(
        self,
        system: str,
        user: str,
        json_mode: bool = False,
        temperature: float | None = None,
    ) -> str:
        """调用大模型，返回回复文本（去空白）。

        - 同步调用包在 asyncio.to_thread 中，整体受 asyncio.wait_for 超时约束；
        - 失败按 LLM_RETRIES 次指数退避重试（1s / 2s）；
        - json_mode=True 时传 response_format={"type": "json_object"}，
          若模型不支持（BadRequestError）则降级去掉该参数重试一次；
        - 全部失败抛 RuntimeError。
        """
        settings = self._settings()
        if settings is None:
            raise RuntimeError("未配置大模型 API，请在 AI 设置页配置")
        client = self._client()
        model = str(getattr(settings, "model", "") or "deepseek-chat").strip() or "deepseek-chat"
        temp = temperature if temperature is not None else getattr(settings, "temperature", None)
        if temp is None:
            temp = self._default_temperature()
        timeout = self._llm_timeout()
        retries = self._llm_retries()
        attempts = max(0, retries) + 1

        use_json = bool(json_mode)
        json_downgraded = False
        last_exc: BaseException | None = None

        for attempt in range(attempts):
            try:
                content = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._call_sync, client, model, system, user, use_json, float(temp)
                    ),
                    timeout=timeout,
                )
                return (content or "").strip()
            except asyncio.TimeoutError:
                last_exc = TimeoutError(f"大模型请求超时（{timeout}s）")
            except Exception as exc:
                last_exc = exc
                # json 模式降级：模型不支持 response_format 时去掉该参数重试一次
                if use_json and not json_downgraded and self._is_bad_request(exc):
                    logger.warning("模型不支持 json_object，降级重试一次: %s", exc)
                    use_json = False
                    json_downgraded = True
                    continue
            if attempt < attempts - 1:
                await asyncio.sleep(2 ** attempt)  # 指数退避：1s / 2s
        raise RuntimeError(f"大模型调用失败：{last_exc}") from last_exc

    # ------------------------------------------------------------------
    # A1 简历针对性优化
    # ------------------------------------------------------------------

    async def optimize_resume(self, jd: str, resume_text: str) -> str:
        """针对目标岗位 JD 优化简历（A1）。"""
        self._ensure_configured()
        system, user = self._fill("resume_optimize", jd=jd or "", resume=resume_text or "")
        return await self.chat(system, user)

    # ------------------------------------------------------------------
    # A2 岗位匹配度评分
    # ------------------------------------------------------------------

    async def match_score(self, jd: str, resume_text: str) -> dict:
        """评估岗位匹配度（A2），返回 {"score", "reasons", "gaps"}；绝不抛异常。"""
        self._ensure_configured()
        system, user = self._fill("match_score", jd=jd or "", resume=resume_text or "")
        try:
            raw = await self.chat(system, user, json_mode=True)
        except Exception as exc:
            logger.warning("岗位匹配评分调用失败: %s", exc)
            return {"score": 0, "reasons": [], "gaps": [], "error": str(exc)}
        try:
            data = json.loads(raw or "")
            if not isinstance(data, dict):
                raise ValueError("模型未返回 JSON 对象")
            try:
                score = int(data.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            reasons = data.get("reasons", []) or []
            gaps = data.get("gaps", []) or []
            if not isinstance(reasons, list):
                reasons = []
            if not isinstance(gaps, list):
                gaps = []
            return {"score": max(0, min(100, score)), "reasons": reasons, "gaps": gaps}
        except Exception as exc:
            logger.warning("匹配评分解析失败: %s，原始内容: %s", exc, (raw or "")[:200])
            return {"score": 0, "reasons": [], "gaps": [], "error": "解析失败"}

    # ------------------------------------------------------------------
    # A3 投递频次/策略建议
    # ------------------------------------------------------------------

    async def apply_strategy(self, platform: str, history_summary: str) -> str:
        """给出投递频次与策略建议（A3）。"""
        self._ensure_configured()
        system, user = self._fill(
            "apply_strategy", platform=platform or "", history=history_summary or ""
        )
        return await self.chat(system, user)

    # ------------------------------------------------------------------
    # A4 招呼语/求职信生成
    # ------------------------------------------------------------------

    async def generate_greeting(self, jd: str, resume_summary: str, platform: str) -> str:
        """生成打招呼语/求职信（A4）。"""
        self._ensure_configured()
        system, user = self._fill(
            "generate_greeting",
            jd=jd or "",
            resume=resume_summary or "",
            platform=platform or "",
        )
        return await self.chat(system, user)

    # ------------------------------------------------------------------
    # A5 岗位智能推荐
    # ------------------------------------------------------------------

    @staticmethod
    def _slim_job(job: dict) -> dict:
        """裁剪岗位字段：仅保留推荐所需字段，description 截断到前 200 字。"""
        if not isinstance(job, dict):
            return {"title": "", "company": "", "city": "", "salary_raw": "", "description": ""}
        return {
            "title": str(job.get("title") or ""),
            "company": str(job.get("company") or ""),
            "city": str(job.get("city") or ""),
            "salary_raw": str(job.get("salary_raw") or job.get("salary") or ""),
            "description": str(job.get("description") or "")[:200],
        }

    @staticmethod
    def _rank_jobs(
        limited: list[dict], ranked: object, top_n: int
    ) -> list[dict]:
        """把模型返回的 ranked（下标列表或 job 子集）映射回原始岗位字典。"""
        if not isinstance(ranked, list) or not ranked:
            return limited[:top_n]
        result: list[dict] = []
        for item in ranked:
            if isinstance(item, dict):
                # job 子集：优先按 标题+公司 匹配回原始条目
                title = str(item.get("title", ""))
                company = str(item.get("company", ""))
                found = next(
                    (orig for orig in limited
                     if str(orig.get("title", "")) == title
                     and str(orig.get("company", "")) == company),
                    None,
                )
                result.append(found if found is not None else item)
            else:
                try:
                    idx = int(item)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(limited):
                    result.append(limited[idx])
        if not result:
            return limited[:top_n]
        return result[:top_n]

    async def recommend_jobs(
        self, resume_text: str, jobs: list[dict], top_n: int = 10
    ) -> list[dict]:
        """岗位智能推荐（A5）：返回带推荐排序的岗位 dict 列表。

        最多取前 50 条、每条仅保留 title/company/city/salary_raw/description（前 200 字）
        传入模板；期望模型返回 JSON {"ranked": [下标或 job 子集]}。任何异常
        （解析失败/调用失败等）都回退为按原序返回前 top_n 条。
        """
        self._ensure_configured()
        if top_n <= 0:
            return []
        source = jobs or []
        limited = source[:50]
        if not limited:
            return []
        try:
            slim = [self._slim_job(job) for job in limited]
            system, user = self._fill(
                "recommend_jobs",
                resume=resume_text or "",
                jobs=json.dumps(slim, ensure_ascii=False),
                top_n=str(top_n),
            )
            raw = await self.chat(system, user, json_mode=True)
            data = json.loads(raw or "")
            ranked = data.get("ranked") if isinstance(data, dict) else None
            return self._rank_jobs(limited, ranked, top_n)
        except Exception as exc:
            logger.warning("岗位智能推荐失败，按原序返回前 %d 条: %s", top_n, exc)
            return source[:top_n]

    # ------------------------------------------------------------------
    # A6 投递跟进话术
    # ------------------------------------------------------------------

    async def follow_up(self, jd: str, days: int) -> str:
        """投递 N 天未查看时的跟进话术（A6）。"""
        self._ensure_configured()
        system, user = self._fill("follow_up", jd=jd or "", days=str(days))
        return await self.chat(system, user)

    # ------------------------------------------------------------------
    # A7 面试问题预测
    # ------------------------------------------------------------------

    async def interview_predict(self, jd: str, resume_text: str) -> str:
        """预测面试高频问题并给出回答要点（A7）。"""
        self._ensure_configured()
        system, user = self._fill("interview_predict", jd=jd or "", resume=resume_text or "")
        return await self.chat(system, user)

    # ------------------------------------------------------------------
    # A8 薪资行情分析
    # ------------------------------------------------------------------

    async def salary_analysis(self, keyword: str, city: str, stats_summary: str) -> str:
        """薪资行情分析与个人定位建议（A8）。"""
        self._ensure_configured()
        system, user = self._fill(
            "salary_analysis",
            keyword=keyword or "",
            city=city or "",
            stats=stats_summary or "",
        )
        return await self.chat(system, user)

    # ------------------------------------------------------------------
    # A9 周度投递复盘报告
    # ------------------------------------------------------------------

    async def weekly_report(self, records_summary: str) -> str:
        """生成周度投递复盘报告（A9）。"""
        self._ensure_configured()
        system, user = self._fill("weekly_report", records=records_summary or "")
        return await self.chat(system, user)
