# -*- coding: utf-8 -*-
"""半自动投递服务模块（ApplyService）。

对应《系统设计文档》第 9 章：
- §9.1 半自动流程：程序完成全部机械操作（打开投递页、预填表单），
  **最终提交必须由用户确认**（GUI 注入确认回调，弹模态框）；
- §9.2 风控策略：每日每站上限、两次投递随机间隔（30~90 秒）、
  投递时间窗（默认 09:00~21:00）、投递前登录态二次校验；Boss 直聘上限更保守；
- §9.3 投递记录：成功后写 applications 表
  （job_ref_id / source / resume_version / applied_at / status / note）。

设计原则（安全底线）：**宁可拒绝也不越限** —— 任一风控不满足、
依赖缺失或异常都按“拒绝投递”处理，绝不越过限制。

模块只依赖标准库 + 契约模块的**延迟导入**（采集器 / 浏览器驱动 / 数据库均可能
仍在并行开发中），保证本模块 import 永远成功、运行期安全降级。
"""

from __future__ import annotations

import importlib
import logging
import random
from datetime import datetime, time

logger = logging.getLogger(__name__)

#: 当日投递数查询失败时的返回值：一个不可能达到的极大值，
#: 使 `daily_applied_count(site) < daily_limit_for(site)` 恒为假，
#: 投递被拒绝（安全方向：宁可拒绝也不越限）。
_COUNT_QUERY_FAILED = 1 << 40


def _lazy(module: str, attr: str | None = None):
    """延迟导入（容错）：返回模块或属性；导入失败记 error 并返回 None。

    其它模块与本模块并行开发、可能尚未就绪；延迟导入保证本模块顶层 import 永不失败，
    运行期在依赖缺失时安全降级（拒绝投递 / 使用默认值）。
    """
    try:
        mod = importlib.import_module(module)
    except Exception as exc:  # 任何导入失败一律按“依赖缺失”处理
        logger.error("延迟导入失败 %s: %s", module, exc)
        return None
    if attr is None:
        return mod
    return getattr(mod, attr, None)


class ApplyService:
    """半自动投递服务（单例）。

    负责：风控前置检查（§9.2）→ 登录态校验 → 打开投递页预填（§9.1）
    → 人工确认点 → 最后一步提交 → 写投递记录（§9.3）→ 随机间隔。
    GUI 通过 :meth:`set_confirm_callback` 注入模态确认框回调。
    """

    #: 站点采集器注册表：site 标识 -> Collector 子类。
    #: 与其它服务保持一致：各站点模块启动时调用 ``ApplyService.register(site, cls)`` 注册。
    COLLECTORS: dict[str, type] = {}

    _instance: "ApplyService | None" = None

    def __init__(self) -> None:
        #: 确认回调 cb(job, resume_name, site_display) -> bool；未设置时投递一律拒绝。
        self._confirm_callback = None
        #: 取消标志：cancel_current() 置位，当前进行中的投递在下一个检查点（含确认点）中断。
        self._cancelled = False

    # ------------------------------------------------------------------
    # 单例 / 注册 / 回调
    # ------------------------------------------------------------------

    @classmethod
    def get(cls) -> "ApplyService":
        """返回进程内单例（GUI 单线程事件循环使用，无需加锁）。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def register(cls, site: str, collector_cls: type) -> None:
        """注册站点采集器（site -> Collector 子类）。"""
        cls.COLLECTORS[site] = collector_cls

    def set_confirm_callback(self, cb) -> None:
        """注入半自动确认回调：``cb(job, resume_name, site_display) -> bool``。

        GUI 实现为模态确认框，返回用户是否确认投递。
        未设置时投递一律拒绝（§9.2 默认人工确认模式）。
        """
        self._confirm_callback = cb

    async def cancel_current(self) -> None:
        """置取消标志；当前进行中的投递会在下一个检查点（含确认点）中断。

        取消标志保持置位（安全方向：宁可拒绝也不继续投递），
        开始新一轮投递批次前请先调用 :meth:`reset_cancel` 清除。
        """
        self._cancelled = True

    def reset_cancel(self) -> None:
        """清除取消标志（新一轮投递批次开始前调用）。"""
        self._cancelled = False

    # ------------------------------------------------------------------
    # 风控基础（§9.2）—— 独立成方法以便单元测试
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        """返回当前时间（datetime）。测试可替换本方法以模拟不同时刻。"""
        return datetime.now()

    def _check_cancelled(self) -> bool:
        """取消标志置位则记录并返回 True（调用方应在当前检查点立即中断）。"""
        if self._cancelled:
            logger.info("投递已取消，中断当前流程")
            return True
        return False

    def daily_limit_for(self, site: str) -> int:
        """每日投递上限：Boss 直聘（zhipin）用 BOSS_DAILY_LIMIT，其余站点用
        APPLY_DAILY_LIMIT；可被 settings 表覆盖（boss_daily_limit / apply_daily_limit）。

        读取失败回退默认值（保守方向：宁可拒绝也不越限）。
        """
        if site == "zhipin":
            default = _lazy("app.config.settings", "BOSS_DAILY_LIMIT") or 10
            key = "boss_daily_limit"
        else:
            default = _lazy("app.config.settings", "APPLY_DAILY_LIMIT") or 20
            key = "apply_daily_limit"
        get_setting = _lazy("app.db.repository", "get_setting")
        get_conn = _lazy("app.db.database", "get_connection")
        if get_setting is not None and get_conn is not None:
            try:
                conn = get_conn()
                return int(get_setting(conn, key, default))
            except Exception as exc:
                logger.warning("读取设置 %s 失败，使用默认值 %s: %s", key, default, exc)
        return int(default)

    async def daily_applied_count(self, site: str) -> int:
        """当日（applied_at 以 'YYYY-MM-DD' 开头）该站点已投递记录数。

        直接用 get_connection() 查询 applications 表；查询不可用时返回极大值，
        使上限检查恒不通过（安全方向：宁可拒绝也不越限）。
        """
        get_conn = _lazy("app.db.database", "get_connection")
        if get_conn is None:
            return _COUNT_QUERY_FAILED
        try:
            conn = get_conn()
            today = self._now().strftime("%Y-%m-%d")
            cur = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE source = ? AND applied_at LIKE ?",
                (site, today + "%"),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
        except Exception as exc:
            logger.error("查询当日投递数失败（site=%s）: %s", site, exc)
            return _COUNT_QUERY_FAILED

    def _time_window(self) -> tuple[time, time] | None:
        """读取投递时间窗（§9.2）：默认 APPLY_TIME_START~APPLY_TIME_END
        （"09:00"~"21:00"），可被 settings 表覆盖（apply_time_start / apply_time_end）。

        配置非法（无法解析或 start > end）返回 None —— 调用方应拒绝投递（安全方向）。
        """
        start_default = _lazy("app.config.settings", "APPLY_TIME_START") or "09:00"
        end_default = _lazy("app.config.settings", "APPLY_TIME_END") or "21:00"
        get_setting = _lazy("app.db.repository", "get_setting")
        get_conn = _lazy("app.db.repository", "get_connection")
        start_s, end_s = str(start_default), str(end_default)
        if get_setting is not None and get_conn is not None:
            try:
                conn = get_conn()
                start_s = str(get_setting(conn, "apply_time_start", start_s))
                end_s = str(get_setting(conn, "apply_time_end", end_s))
            except Exception as exc:
                logger.warning("读取时间窗设置失败，使用默认值: %s", exc)
        try:
            start_t = time.fromisoformat(start_s)
            end_t = time.fromisoformat(end_s)
        except ValueError as exc:
            logger.error("时间窗配置非法（%s~%s），拒绝投递: %s", start_s, end_s, exc)
            return None
        if start_t > end_t:
            logger.error("时间窗配置非法（start>end: %s~%s），拒绝投递", start_s, end_s)
            return None
        return start_t, end_t

    def _confirm(self, job, resume_name: str, site_display: str) -> bool:
        """半自动确认点（§9.1 步骤 4，核心安全边界）。

        - 取消标志置位 → 中断；
        - 确认回调未设置 → 记 warning 并拒绝（人工确认模式默认开启，§9.2）；
        - 回调返回 False（用户拒绝）→ 记 info 并拒绝，不写投递记录；
        - 回调抛异常 → 记 error 并拒绝（宁可拒绝也不越限）。
        """
        if self._cancelled:
            logger.info("投递已取消，跳过确认")
            return False
        cb = self._confirm_callback
        if cb is None:
            logger.warning("未设置确认回调，拒绝投递（安全策略：人工确认模式，§9.2）")
            return False
        try:
            confirmed = bool(cb(job, resume_name, site_display))
        except Exception as exc:
            logger.error("确认回调异常，拒绝投递: %s", exc)
            return False
        if not confirmed:
            logger.info("用户拒绝投递（%s / %s），不写入投递记录", site_display, resume_name)
        return confirmed

    # ------------------------------------------------------------------
    # 投递记录（§9.3）
    # ------------------------------------------------------------------

    @staticmethod
    def _row_get(row, key):
        """从查询结果行读取字段：兼容 sqlite3.Row / dict / 对象 / 元组。"""
        if row is None:
            return None
        value = getattr(row, key, None)
        if value is not None:
            return value
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _row_id(row) -> int | None:
        """从查询结果行提取主键 id（兼容 sqlite3.Row / dict / 对象 / 元组）。"""
        value = ApplyService._row_get(row, "id")
        if value is not None:
            return value
        try:
            return row[0]  # 元组 / Row 取第一列
        except (TypeError, IndexError, AttributeError):
            return None

    def _resolve_job_ref_id(self, conn, site: str, job) -> int | None:
        """解析 jobs 表主键 job_ref_id（§9.3）：

        1) RawJob 自带主键字段（id）→ 直接使用；
        2) 否则 repository 提供 get_job_by_source_id(conn, source, job_id) → 调用；
        3) 否则用 get_job 列表匹配（job_id + source）；
        找不到返回 None —— 投递记录中记 note="job_ref 未找到"，仍插入（§9.3 契约）。
        """
        pk = getattr(job, "id", None)
        if pk is not None:
            return pk
        source_id = getattr(job, "job_id", None)
        if source_id is None:
            logger.warning("RawJob 缺少 job_id，无法解析 job_ref_id")
            return None
        repo = _lazy("app.db.repository")
        if repo is None:
            return None
        get_by = getattr(repo, "get_job_by_source_id", None)
        if get_by is not None:
            try:
                return self._row_id(get_by(conn, site, source_id))
            except Exception as exc:
                logger.warning("get_job_by_source_id 查询失败，尝试列表匹配: %s", exc)
        get_jobs = getattr(repo, "get_job", None)
        if get_jobs is None:
            return None
        try:
            rows = get_jobs(conn)
            for row in rows or []:
                if (self._row_get(row, "job_id") == source_id
                        and self._row_get(row, "source") == site):
                    rid = self._row_id(row)
                    if rid is not None:
                        return rid
        except Exception as exc:
            logger.warning("get_job 列表匹配失败: %s", exc)
        return None

    def _record_application(self, site: str, resume_name: str, job_ref_id: int | None) -> None:
        """提交成功后写投递记录（§9.3 applications 表）。

        job_ref 未找到时 note="job_ref 未找到" 仍插入；
        写记录失败仅记 error，不改变 apply_job 的成功结论（站点提交动作已完成）。
        """
        get_conn = _lazy("app.db.database", "get_connection")
        insert = _lazy("app.db.repository", "insert_application")
        Application = _lazy("app.db.models", "Application")
        if get_conn is None or insert is None or Application is None:
            logger.error("投递记录写入依赖缺失，跳过记录（站点提交已成功）")
            return
        try:
            conn = get_conn()
            app = Application(
                job_ref_id=job_ref_id,
                source=site,
                resume_version=resume_name,
                applied_at=self._now().strftime("%Y-%m-%d %H:%M:%S"),
                status="submitted",
                note="job_ref 未找到" if job_ref_id is None else "",
            )
            insert(conn, app)
            conn.commit()  # 必须提交，否则其他连接读不到记录
            conn.close()
        except Exception as exc:
            logger.error("写投递记录失败（站点提交已成功）: %s", exc)

    # ------------------------------------------------------------------
    # 主流程：半自动投递（§9.1 流程 + §9.2 风控）
    # ------------------------------------------------------------------

    async def apply_job(self, site: str, job, resume_name: str,
                        greeting: str | None = None) -> bool:
        """半自动投递单条岗位，返回是否成功提交（True）。

        每步检查取消标志；任一风控不满足即拒绝并记录原因（宁可拒绝也不越限）。
        """
        browser = None
        try:
            # ---- 0. 取消标志（入口检查，后续每步复查） ----
            if self._check_cancelled():
                return False

            # ---- 1. 风控前置检查（§9.2 频率控制，任何不满足直接拒绝） ----

            # 1a. 时间窗：只在配置的时间窗内运行（§9.2）
            window = self._time_window()
            if window is None:  # 配置非法 → 拒绝
                return False
            start_t, end_t = window
            now_t = self._now().time()
            if not start_t <= now_t <= end_t:
                logger.warning("当前时间 %s 不在投递时间窗（%s~%s）内，拒绝投递",
                               now_t, start_t, end_t)
                return False

            if self._check_cancelled():
                return False

            # 1b. 每日上限：daily_applied_count(site) < daily_limit_for(site)（§9.2）
            count = await self.daily_applied_count(site)
            limit = self.daily_limit_for(site)
            if count >= limit:
                logger.warning("当日投递已达上限（%d/%d），拒绝投递", count, limit)
                return False

            if self._check_cancelled():
                return False

            # 1c. 重复投递检查（§12.2：applications.job_ref_id 唯一，同一岗位不重复投递）。
            #     提前解析 job_ref_id，步骤 6 写记录时复用。
            job_ref_id = None
            get_conn = _lazy("app.db.repository", "get_connection")
            if get_conn is not None:
                try:
                    conn = get_conn()
                    job_ref_id = self._resolve_job_ref_id(conn, site, job)
                    get_app = _lazy("app.db.repository", "get_application_by_job")
                    if (job_ref_id is not None and get_app is not None
                            and get_app(conn, job_ref_id) is not None):
                        logger.warning("该岗位已投递过（job_ref_id=%s），拒绝重复投递",
                                       job_ref_id)
                        return False
                except Exception as exc:
                    logger.warning("重复投递检查异常，忽略（继续投递流程）: %s", exc)

            if self._check_cancelled():
                return False

            # ---- 2. 登录态二次校验（§9.2：投递前校验登录态，失效立即中断） ----
            # 先查注册表再启动浏览器：未注册站点不开浏览器（省资源且更安全）。
            collector_cls = self.COLLECTORS.get(site)
            if collector_cls is None:
                logger.error("站点 %s 的采集器未注册，拒绝投递", site)
                return False
            BrowserManager = _lazy("app.browser.driver", "BrowserManager")
            if BrowserManager is None:
                logger.error("浏览器驱动模块不可用，拒绝投递")
                return False
            browser = BrowserManager.get(site)
            await browser.start()
            page = await browser.new_page()
            collector = collector_cls()
            if not await collector.is_logged_in(page):
                logger.error("站点 %s 未登录，拒绝投递（§9.2 登录态二次校验）", site)
                return False

            if self._check_cancelled():
                return False

            # ---- 3. 打开投递页并预填（§9.1 机械操作） ----
            try:
                opened = await collector.open_apply(page, job)
            except Exception as exc:
                logger.error("打开投递页失败: %s", exc)
                return False
            if opened is False:
                logger.error("打开投递页失败（collector 返回 False）")
                return False

            if self._check_cancelled():
                return False

            # ---- 4. 半自动确认点（§9.1 核心：最终提交必须用户确认；取消在此中断） ----
            sites = _lazy("app.collectors.base", "SITES") or {}
            if not self._confirm(job, resume_name, sites.get(site, site)):
                return False

            if self._check_cancelled():
                return False

            # ---- 5. 最后一步提交（确认后才执行；失败不写投递记录） ----
            if site == "zhipin":
                # Boss 直聘特殊链路（§9.1）：发送打招呼语（可 AI 生成）
                ok = await collector.send_greeting(page, job, greeting or "")
            else:
                ok = await collector.submit_apply(page, resume_name)
            if not ok:
                logger.error("提交失败（site=%s），不写投递记录", site)
                return False

            # ---- 6. 写投递记录（§9.3；提交已成功，必须落记录，否则后续可能重复投递） ----
            self._record_application(site, resume_name, job_ref_id)

            # ---- 7. 风控间隔（§9.2：两次投递随机间隔 30~90 秒；已取消则无需等待） ----
            if not self._cancelled:
                try:
                    min_s = _lazy("app.config.settings", "APPLY_INTERVAL_MIN") or 30
                    max_s = _lazy("app.config.settings", "APPLY_INTERVAL_MAX") or 90
                    lo, hi = int(min_s), int(max_s)
                    if lo > hi:  # 配置倒置时交换，保持间隔可用
                        lo, hi = hi, lo
                    await browser.sleep(random.randint(lo, hi) * 1000)
                except Exception as exc:
                    logger.warning("风控间隔等待失败（不影响本次结果）: %s", exc)

            return True
        except Exception as exc:  # 任何未预期异常都视为失败（安全方向）
            logger.error("投递流程异常，视为失败: %s", exc, exc_info=True)
            return False
        finally:
            # ---- 8. 收尾：关闭浏览器页面（§9.1 流程最后一步） ----
            if browser is not None:
                try:
                    await browser.close_all_pages()
                except Exception as exc:
                    logger.warning("关闭浏览器页面失败: %s", exc)
