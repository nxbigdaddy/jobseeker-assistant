"""调度服务 —— APScheduler 每日定时增量采集。

对应设计文档 §14：
- 随应用启动 BackgroundScheduler；
- 默认每天 DAILY_CRAWL_TIME（08:00）执行一次增量采集；
- 运行状态通过状态回调通知 GUI 状态栏。
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config.settings import DAILY_CRAWL_TIME

logger = logging.getLogger(__name__)


class SchedulerService:
    """封装 APScheduler：管理每日增量采集任务。"""

    _instance: "SchedulerService | None" = None

    def __init__(self) -> None:
        self._scheduler: BackgroundScheduler | None = None
        self._job_id = "daily_incremental_crawl"
        self._status_callback = None  # cb(text: str) 状态栏更新

    @classmethod
    def get(cls) -> "SchedulerService":
        """单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_status_callback(self, cb) -> None:
        """注册状态回调：cb(text: str)，任务开始/结束时调用。"""
        self._status_callback = cb

    def start(self) -> bool:
        """启动调度器并注册每日任务。已运行时直接返回 True。"""
        if self._scheduler and self._scheduler.running:
            return True
        hour, _, minute = DAILY_CRAWL_TIME.partition(":")
        try:
            self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
            self._scheduler.add_job(
                self._run_daily,
                CronTrigger(hour=int(hour), minute=int(minute)),
                id=self._job_id,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=3600,
            )
            self._scheduler.start()
            logger.info("调度器已启动，每日 %s 执行增量采集", DAILY_CRAWL_TIME)
            self._notify(f"每日增量采集已调度：{DAILY_CRAWL_TIME}")
            return True
        except Exception:  # noqa: BLE001
            logger.exception("调度器启动失败")
            return False

    def stop(self) -> None:
        """停止调度器。"""
        if self._scheduler and self._scheduler.running:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                logger.exception("调度器停止异常")
            self._scheduler = None
            logger.info("调度器已停止")
            self._notify("每日增量采集已停用")

    def is_running(self) -> bool:
        return bool(self._scheduler and self._scheduler.running)

    def trigger_now(self) -> bool:
        """立即触发一次增量采集（GUI"立即采集"按钮）。"""
        if not self._scheduler:
            logger.warning("调度器未启动，无法立即触发")
            return False
        try:
            self._scheduler.add_job(self._run_daily, trigger="date", id="manual_crawl", replace_existing=True)
            self._notify("立即采集任务已提交")
            return True
        except Exception:  # noqa: BLE001
            logger.exception("立即触发失败")
            return False

    # ------------------------------------------------------------ 内部
    def _run_daily(self) -> None:
        """定时任务入口：在 APScheduler 线程中运行异步增量采集。"""
        self._notify("正在执行每日增量采集……")
        try:
            asyncio.run(self._work())
        except Exception:  # noqa: BLE001
            logger.exception("每日增量采集失败")
            self._notify("每日增量采集失败，详见日志")

    async def _work(self) -> None:
        """实际采集逻辑：延迟导入 CrawlService，跑增量任务。"""
        from app.services.crawl_service import CrawlService  # 延迟导入

        results = await CrawlService.get().incremental_run(
            days=7,
            fetch_detail=True,
            on_progress=lambda text: logger.info("[增量] %s", text),
        )
        total_fetched = sum(r.get("fetched", 0) for r in results)
        total_inserted = sum(r.get("inserted", 0) for r in results)
        logger.info("增量采集完成：共 %d 条（新增 %d 条）", total_fetched, total_inserted)
        self._notify(f"增量采集完成：{total_fetched} 条 / 新增 {total_inserted} 条")

    def _notify(self, text: str) -> None:
        if self._status_callback:
            try:
                self._status_callback(text)
            except Exception:  # noqa: BLE001
                logger.exception("状态回调异常")
