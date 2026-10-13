# -*- coding: utf-8 -*-
"""采集服务模块（CrawlService）。

职责：
- 解析搜索任务（站点 / 关键词 / 城市），驱动各站点采集器（Collector）执行岗位搜索；
- 可选逐条抓取岗位详情以补充描述；将归一化后的岗位写入本地 SQLite；
- 记录每次任务的采集运行记录（crawl_runs），运行结束后标记列表缺失岗位为 offline；
- 支持全量运行与增量运行（仅保留近 N 天发布的岗位入库）。

设计约定：
- 站点适配器通过 :meth:`CrawlService.register` 注册，服务层与站点完全解耦
  （与 AuthService 相同的注册机制）；
- 所有方法均为异步；单个站点 / 关键词 / 城市的失败不会中断整个任务；
- 所有数据库访问均延迟导入（try/except ImportError 容错），数据库不可用时
  记录日志并返回空结果，不影响应用其他模块。

用法示例::

    CrawlService.register("51job", Wuyou51Collector)
    service = CrawlService.get()
    results = await service.run_all_enabled(on_progress=print)
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import logging
import random
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, ClassVar, Optional

logger = logging.getLogger(__name__)

__all__ = ["CrawlService", "CrawlSummary"]

#: 列表页翻页上限：列表接口按发布时间倒序，采集前若干页即可满足增量需求
MAX_PAGES = 3

#: 站点间随机间隔（毫秒），用于降低触发站点风控的概率
INTER_SITE_DELAY_MS = (2000, 5000)

#: 发布日期常见文本格式（按优先级尝试解析）
_PUB_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y年%m月%d日",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)

#: 详情页可补充到列表数据的字段
_DETAIL_FIELDS = (
    "description", "salary_min", "salary_max", "salary_raw", "experience",
    "education", "company_size", "industry", "district", "tags", "publish_date",
)


@dataclass
class CrawlSummary:
    """一次「站点 × 关键词 × 城市」组合的采集小结。"""

    site: str
    keyword: str
    city: Optional[str]
    fetched: int
    inserted: int
    error: Optional[str] = None


class CrawlService:
    """岗位采集服务：任务解析 → 逐站采集 → 归一化入库 → 运行记录。"""

    #: 站点标识 → Collector 子类 注册表（与 AuthService 相同的注册机制）
    COLLECTORS: ClassVar[dict] = {}

    _instance: ClassVar[Optional["CrawlService"]] = None

    @classmethod
    def get(cls) -> "CrawlService":
        """返回全局单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def register(cls, site: str, collector_cls: type) -> None:
        """注册站点采集器类，供采集服务按站点标识查找。"""
        cls.COLLECTORS[site] = collector_cls

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #

    async def run_task(self, task: SearchTask, fetch_detail: bool = True,
                       on_progress: Optional[Callable[[str], None]] = None) -> dict:
        """运行单个搜索任务（全量模式）。

        :param task: SearchTask 实例（sites / keywords / cities 为 JSON 字符串）
        :param fetch_detail: 是否逐条抓取详情页补充岗位描述
        :param on_progress: 可选进度回调 ``on_progress(text: str)``
        :return: ``{"fetched": int, "inserted": int, "errors": list[str], "summary": list[CrawlSummary]}``
        """
        return await self._run_task(task, fetch_detail=fetch_detail, on_progress=on_progress, days=None)

    async def run_all_enabled(self, fetch_detail: bool = True,
                              on_progress: Optional[Callable[[str], None]] = None) -> list:
        """运行所有启用的搜索任务（全量模式），返回每个任务的运行结果字典列表。"""
        tasks = self._load_enabled_tasks()
        results = []
        for task in tasks:
            results.append(await self._run_task(task, fetch_detail=fetch_detail, on_progress=on_progress, days=None))
        return results

    async def incremental_run(self, days: int = 7, fetch_detail: bool = True,
                              on_progress: Optional[Callable[[str], None]] = None) -> list:
        """增量采集所有启用任务：仅保留近 ``days`` 天内发布的岗位入库。

        列表接口按发布时间倒序返回，采集前若干页即可满足增量需求；
        ``days`` 优先取设置表 ``daily_new_job_days``，其次使用传入值。
        """
        tasks = []
        with self._maybe_connection() as conn:
            if conn is None:
                return []
            repo = self._lazy_import("app.db.repository")
            if repo is None:
                return []
            try:
                raw = repo.get_setting(conn, "daily_new_job_days", str(days))
                days = max(1, int(raw))
            except Exception:
                days = max(1, int(days))  # 设置缺失或非法时使用传入默认值
            try:
                tasks = repo.list_search_tasks(conn, enabled_only=True)
            except Exception as exc:
                logger.warning("读取启用任务失败：%s", exc)
                return []
        results = []
        for task in tasks:
            results.append(await self._run_task(task, fetch_detail=fetch_detail, on_progress=on_progress, days=days))
        return results

    # ------------------------------------------------------------------ #
    # 任务级实现
    # ------------------------------------------------------------------ #

    async def _run_task(self, task, fetch_detail, on_progress, days):
        """任务级实现：记录运行记录 → 逐站点采集 → 汇总落库。

        :param days: 增量过滤天数；None 表示全量（不过滤）
        """
        task_name = getattr(task, "name", None) or str(getattr(task, "id", ""))
        self._progress(on_progress, f"开始运行任务「{task_name}」")
        with self._maybe_connection() as conn:
            if conn is None:
                logger.warning("数据库不可用，任务「%s」返回空结果", task_name)
                return {"fetched": 0, "inserted": 0, "errors": ["数据库不可用"], "summary": []}
            repo = self._lazy_import("app.db.repository")
            models = self._lazy_import("app.db.models")
            if repo is None or models is None:
                return {"fetched": 0, "inserted": 0, "errors": ["数据库模块不可用"], "summary": []}

            # 每个任务记录一次采集运行（开始）
            run_pk = None
            try:
                run = models.CrawlRun(
                    task_id=task.id,
                    started_at=_now_text(),
                    finished_at=None,
                    fetched=0,
                    inserted=0,
                    status="running",
                    error=None,
                )
                run_pk = repo.insert_crawl_run(conn, run)
            except Exception as exc:
                logger.warning("写入采集运行记录失败：%s", exc)

            fatal_error = None
            try:
                summary, errors, fetched, inserted = await self._crawl(
                    task, conn, repo, models,
                    fetch_detail=fetch_detail, on_progress=on_progress, days=days,
                )
            except Exception as exc:  # 兜底：任何未捕获异常都不向调用方抛出
                fatal_error = exc
                logger.exception("任务「%s」执行出错", task_name)
                summary, errors, fetched, inserted = [], [f"任务执行出错：{exc}"], 0, 0

            # 结束采集运行记录（异常同样落库 status='error'）
            status = "error" if (fatal_error is not None or errors) else "success"
            error_text = str(fatal_error) if fatal_error is not None else ("；".join(errors) if errors else None)
            try:
                if run_pk:
                    repo.finish_crawl_run(conn, run_pk, fetched, inserted, status, error=error_text)
                repo.set_task_last_run(conn, task.id, _now_text())
            except Exception as exc:
                logger.warning("结束采集运行记录失败：%s", exc)

        self._progress(on_progress, f"任务「{task_name}」完成：抓取 {fetched} 条，入库 {inserted} 条，错误 {len(errors)} 条")
        return {"fetched": fetched, "inserted": inserted, "errors": errors, "summary": summary}

    async def _crawl(self, task, conn, repo, models, fetch_detail, on_progress, days):
        """遍历「站点 × 关键词 × 城市」执行采集。

        返回 ``(summary, errors, fetched, inserted)``；单个组合失败仅记录，
        不影响后续组合继续执行。
        """
        sites = _parse_json_list(getattr(task, "sites", None))
        keywords = _parse_json_list(getattr(task, "keywords", None))
        cities = _parse_json_list(getattr(task, "cities", None))

        if not sites:
            msg = f"任务「{getattr(task, 'name', '')}」未配置站点，跳过"
            logger.warning(msg)
            return [], [msg], 0, 0
        if not keywords:
            keywords = [""]      # 关键词为空时交给站点采集器决定默认搜索
        if not cities:
            cities = [None]      # 城市为空时按站点默认城市采集

        driver_mod = self._lazy_import("app.browser.driver")
        if driver_mod is None:
            return [], ["浏览器驱动模块不可用"], 0, 0
        browser_manager_cls = driver_mod.BrowserManager

        summary: list[CrawlSummary] = []
        errors: list[str] = []
        prev_site = None

        for site in sites:
            site_key = str(site)
            collector_cls = self.COLLECTORS.get(site_key)
            if collector_cls is None:
                msg = f"站点 {site_key} 未注册采集器，跳过"
                errors.append(msg)
                logger.warning(msg)
                continue
            try:
                collector = collector_cls()
            except Exception as exc:
                msg = f"站点 {site_key} 采集器初始化失败：{exc}"
                errors.append(msg)
                logger.warning(msg)
                continue

            browser = browser_manager_cls.get(site_key)
            try:
                await browser.start()
            except Exception as exc:
                msg = f"站点 {site_key} 浏览器启动失败：{exc}"
                errors.append(msg)
                logger.warning(msg)
                continue

            # 站点间随机间隔（2~5 秒），降低触发风控的概率
            if prev_site is not None and site_key != prev_site:
                try:
                    await browser.sleep(random.randint(*INTER_SITE_DELAY_MS))
                except Exception as exc:
                    logger.warning("站点间等待失败：%s", exc)
            prev_site = site_key

            # 站点级 seen 集合：mark_offline 必须以“站点”为粒度（同一站点
            # 多个关键词/城市的并集），否则会误标其他组合抓到的岗位。
            seen_ids: list[str] = []
            site_crawled_ok = False
            for keyword in keywords:
                for city in cities:
                    keyword_text = str(keyword) if keyword is not None else ""
                    city_text = str(city) if city is not None else ""
                    try:
                        page = await browser.new_page()
                    except Exception as exc:
                        msg = f"站点 {site_key} 创建页面失败：{exc}"
                        errors.append(msg)
                        logger.warning(msg)
                        summary.append(CrawlSummary(site_key, keyword_text, city_text, 0, 0, msg))
                        continue

                    combo_fetched = 0
                    try:
                        try:
                            logged_in = await collector.is_logged_in(page)
                        except Exception as exc:
                            logged_in = False
                            logger.warning("站点 %s 登录态校验异常：%s", site_key, exc)
                        if not logged_in:
                            logger.warning("站点 %s 未登录，跳过 关键词「%s」/城市「%s」",
                                           site_key, keyword_text, city_text)
                            summary.append(CrawlSummary(site_key, keyword_text, city_text, 0, 0, "站点未登录，跳过"))
                            continue

                        jobs = await collector.search_jobs(page, keyword, city, max_pages=MAX_PAGES)
                        if days:  # 增量模式：只保留近 days 天内发布的岗位
                            jobs = _filter_recent(jobs, days)
                        site_crawled_ok = True
                        combo_fetched = len(jobs)

                        for raw in jobs:
                            raw_id = str(getattr(raw, "job_id", "") or "")
                            if raw_id:
                                seen_ids.append(raw_id)
                            if fetch_detail:
                                raw = await self._fetch_detail(collector, page, raw)
                            job = _to_job(raw, models)
                            if job is None:
                                continue
                            try:
                                repo.upsert_job(conn, job)
                            except Exception as exc:
                                logger.warning("岗位 %s/%s 入库失败：%s", site_key, raw_id, exc)

                        # 简化：统一计数 inserted = fetched，新增/更新区分留给后续版本
                        self._progress(on_progress, f"站点 {site_key}：关键词「{keyword_text}」城市「{city_text}」抓取 {combo_fetched} 条")
                        summary.append(CrawlSummary(site_key, keyword_text, city_text, combo_fetched, combo_fetched))
                    except Exception as exc:
                        msg = f"站点 {site_key} 采集 关键词「{keyword_text}」城市「{city_text}」失败：{exc}"
                        errors.append(msg)
                        logger.warning(msg)
                        summary.append(CrawlSummary(site_key, keyword_text, city_text, combo_fetched, combo_fetched, msg))
                    finally:
                        try:
                            await browser.close_all_pages()
                        except Exception as exc:
                            logger.warning("站点 %s 关闭页面失败：%s", site_key, exc)

            # 站点级收尾：本次列表未出现的岗位标记为 offline（仅当确实采集成功过）
            if site_crawled_ok and seen_ids:
                try:
                    repo.mark_offline(conn, site_key, seen_ids)
                except Exception as exc:
                    logger.warning("站点 %s 标记 offline 失败：%s", site_key, exc)

            # 站点收尾：释放浏览器进程与 profile 锁。
            # 若不 stop，chromium 进程会残留并持有 user_data_dir 锁，
            # 后续同站点操作（投递/再采集）launch_persistent_context 会冲突退出（exit code 21）。
            try:
                await browser.stop()
            except Exception as exc:
                logger.warning("站点 %s 关闭浏览器失败：%s", site_key, exc)

        fetched_total = sum(s.fetched for s in summary)
        inserted_total = sum(s.inserted for s in summary)
        return summary, errors, fetched_total, inserted_total

    async def _fetch_detail(self, collector, page, raw):
        """抓取岗位详情并合并回列表数据；失败时保留列表数据。"""
        job_url = getattr(raw, "job_url", None)
        if not job_url:
            return raw
        try:
            detail = await collector.parse_job(page, job_url)
        except Exception as exc:
            logger.warning("岗位 %s 详情抓取失败，使用列表数据：%s", getattr(raw, "job_id", "?"), exc)
            return raw
        if detail is None:
            return raw
        try:
            return _merge_job(raw, detail)
        except Exception as exc:
            logger.warning("岗位 %s 详情合并失败，使用列表数据：%s", getattr(raw, "job_id", "?"), exc)
            return raw

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #

    def _load_enabled_tasks(self) -> list:
        """读取所有启用任务；数据库不可用时返回空列表。"""
        with self._maybe_connection() as conn:
            if conn is None:
                return []
            repo = self._lazy_import("app.db.repository")
            if repo is None:
                return []
            try:
                return repo.list_search_tasks(conn, enabled_only=True)
            except Exception as exc:
                logger.warning("读取启用任务失败：%s", exc)
                return []

    @staticmethod
    def _lazy_import(module_name):
        """延迟导入契约模块；导入失败记日志并返回 None。"""
        try:
            return importlib.import_module(module_name)
        except ImportError as exc:
            logger.warning("模块 %s 导入失败：%s（相关功能将被跳过）", module_name, exc)
            return None

    @staticmethod
    def _progress(on_progress, text: str) -> None:
        """安全调用可选进度回调。"""
        if on_progress is None:
            return
        try:
            on_progress(text)
        except Exception as exc:
            logger.warning("进度回调异常：%s", exc)

    @contextmanager
    def _maybe_connection(self):
        """延迟获取数据库连接；不可用时产出 None 而非抛出异常。

        数据库模块缺失、连接失败或使用期异常都会被记录日志并降级，
        保证采集流程不因数据库问题整体崩溃。
        """
        try:
            from app.db.database import get_connection
        except ImportError:
            logger.warning("数据库模块 app.db.database 不可用，跳过所有 DB 操作")
            yield None
            return
        try:
            with get_connection() as conn:
                try:
                    yield conn
                except Exception as exc:
                    logger.warning("数据库操作异常：%s", exc)
        except Exception as exc:
            logger.warning("数据库连接不可用：%s", exc)
            yield None


# ---------------------------------------------------------------------- #
# 模块级辅助函数
# ---------------------------------------------------------------------- #

def _now_text() -> str:
    """当前时间字符串（YYYY-MM-DD HH:MM:SS）。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_json_list(value) -> list:
    """解析任务字段中的 JSON 数组；已是列表则原样返回，失败或为空返回 []。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    text = str(value).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        logger.warning("JSON 解析失败：%r，按空列表处理", text)
        return []
    return data if isinstance(data, list) else []


def _filter_recent(jobs, days) -> list:
    """增量过滤：仅保留 publish_date 在近 ``days`` 天内的岗位。

    列表接口按发布时间倒序，前若干页即覆盖近期岗位；发布日期无法解析的
    岗位保守保留（不因解析失败而误删新岗位）。
    """
    if not jobs or not days or days <= 0:
        return list(jobs) if jobs else []
    cutoff = date.today() - timedelta(days=int(days))
    kept = []
    for job in jobs:
        publish_date = _parse_publish_date(getattr(job, "publish_date", None))
        if publish_date is None or publish_date >= cutoff:
            kept.append(job)
    return kept


def _parse_publish_date(text) -> Optional[date]:
    """尽力解析发布日期文本为 date；无法解析返回 None。"""
    if text is None:
        return None
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    text = str(text).strip()
    if not text:
        return None
    today = date.today()
    if "今天" in text or "刚刚" in text or "小时前" in text or "分钟前" in text:
        return today
    if "昨天" in text:
        return today - timedelta(days=1)
    if "前天" in text:
        return today - timedelta(days=2)
    m = re.search(r"(\d+)\s*天前", text)
    if m:
        return today - timedelta(days=int(m.group(1)))
    for fmt in _PUB_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    m = re.match(r"(\d{1,2})-(\d{1,2})(?:$|\s)", text)  # 如 "09-01"（视为今年）
    if m:
        try:
            return date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    return None


def _merge_job(base, detail):
    """用详情数据补充列表数据：详情字段非空时覆盖列表字段，返回 base。"""
    for field_name in _DETAIL_FIELDS:
        value = getattr(detail, field_name, None)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, dict)) and not value:
            continue
        setattr(base, field_name, value)
    return base


def _to_job(raw, models):
    """把 RawJob 归一化为 Job。

    优先调用 ``RawJob.to_job()``（若存在）；失败则按契约字段手动构造，
    仅传入 Job 实际声明的字段，避免契约外字段导致构造失败。
    """
    job_cls = models.Job
    to_job = getattr(raw, "to_job", None)
    if callable(to_job):
        try:
            job = to_job()
            if job is not None:
                return job
        except Exception as exc:
            logger.warning("RawJob.to_job() 失败，回退手动构造：%s", exc)

    try:
        tags = getattr(raw, "tags", None)
        if isinstance(tags, (list, tuple)):
            tags = json.dumps(list(tags), ensure_ascii=False)
        elif not tags:
            tags = "[]"
        is_favorite = getattr(raw, "is_favorite", None)
        payload = {
            "source": getattr(raw, "source", None),
            "job_id": getattr(raw, "job_id", None),
            "title": getattr(raw, "title", None),
            "company": getattr(raw, "company", None),
            "company_size": getattr(raw, "company_size", None),
            "industry": getattr(raw, "industry", None),
            "salary_min": getattr(raw, "salary_min", None),
            "salary_max": getattr(raw, "salary_max", None),
            "salary_raw": getattr(raw, "salary_raw", None),
            "city": getattr(raw, "city", None),
            "district": getattr(raw, "district", None),
            "experience": getattr(raw, "experience", None),
            "education": getattr(raw, "education", None),
            "tags": tags,
            "description": getattr(raw, "description", None),
            "publish_date": getattr(raw, "publish_date", None),
            "job_url": getattr(raw, "job_url", None),
            "crawl_time": getattr(raw, "crawl_time", None) or _now_text(),
            "status": getattr(raw, "status", None) or "active",
            "is_favorite": 0 if is_favorite in (None, 0, False) else 1,
            "match_score": getattr(raw, "match_score", None),
        }
        kwargs = payload
        try:
            known = {f.name for f in dataclasses.fields(job_cls)}
            kwargs = {k: v for k, v in payload.items() if k in known}
        except TypeError:
            pass  # 非 dataclass，直接按字段名传参
        return job_cls(**kwargs)
    except Exception as exc:
        logger.warning("岗位 %s 归一化失败：%s", getattr(raw, "job_id", "?"), exc)
        return None
