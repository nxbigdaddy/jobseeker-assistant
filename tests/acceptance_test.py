# -*- coding: utf-8 -*-
"""P2 全链路验收测试（需要 chromium 已安装）。

覆盖链路：
1. 数据库干净初始化；
2. 模拟采集器（MockCollector，不访问真实站点）注册；
3. 创建搜索任务 → CrawlService.run_task → 岗位入库；
4. JobListPage 展示岗位；
5. ApplyService 半自动投递（确认回调）→ 投递记录入库；
6. ResumeService 简历管理。

用法：python acceptance_test.py
"""
import sys
import threading

sys.path.insert(0, '.')

from app.db.database import init_db, get_connection
from app.db import repository as repo
from app.db.models import SearchTask
from app.collectors.base import Collector, RawJob, SITES

PASS = []


def ok(name, cond, extra=""):
    PASS.append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {extra}")
    assert cond, f"断言失败: {name}"


class MockCollector(Collector):
    """模拟采集器：返回固定岗位数据，不访问网络。"""

    source = "mock"

    async def goto_login(self, page):
        return None

    async def is_logged_in(self, page):
        return True  # 模拟已登录

    async def login_qr(self, page, qr_callback):
        return True

    async def login_sms(self, page, phone, code):
        return True

    async def search_jobs(self, page, keyword, city, max_pages=3):
        jobs = []
        for i in range(3):
            jobs.append(RawJob(
                source=self.source, job_id=f"MOCK-{keyword}-{i}",
                title=f"{keyword}工程师{i}", company="模拟公司", company_size="100-499人",
                industry="互联网", salary_min=15, salary_max=25, salary_raw="15-25K",
                city=city or "北京", district="海淀区", experience="3-5年", education="本科",
                tags=["Java", "Spring"], description=f"负责{keyword}相关开发工作，要求熟悉主流框架。",
                publish_date="2026-09-01", job_url=f"http://mock/{i}", crawl_time="2026-09-01 10:00:00",
            ))
        return jobs

    async def parse_job(self, page, job_url):
        return None

    async def open_apply(self, page, job):
        return None

    async def submit_apply(self, page, resume_name):
        return True

    async def send_greeting(self, page, job, text):
        return True


def main():
    # 1. 干净初始化
    init_db()
    conn = get_connection()
    for t in ("applications", "jobs", "crawl_runs", "search_tasks"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    conn.close()
    ok("DB 初始化", True)

    # 2. 注册模拟采集器
    from app.services.crawl_service import CrawlService
    from app.services.apply_service import ApplyService
    from app.services.auth_service import AuthService
    CrawlService.register("mock", MockCollector)
    ApplyService.register("mock", MockCollector)
    AuthService.register("mock", MockCollector)
    ok("采集器注册", "mock" in CrawlService.COLLECTORS)

    # 3. 创建任务并采集
    import asyncio
    from app.services.crawl_service import CrawlService as CS

    conn = get_connection()
    task = SearchTask(name="验收任务", sites=["mock"], keywords=["Java"],
                      cities=["北京"], salary_min=0, salary_max=0, enabled=1, last_run_at=None)
    task_id = repo.insert_search_task(conn, task)
    conn.commit()  # 必须提交，后续用新连接读取
    conn.close()
    ok("任务创建", task_id > 0)

    async def run_crawl():
        from app.db import repository as rp
        with get_connection() as conn2:
            t = rp.list_search_tasks(conn2, enabled_only=True)[0]
            result = await CS.get().run_task(t, fetch_detail=False)
        return result

    result = asyncio.run(run_crawl())
    print("  采集结果:", {k: result.get(k) for k in ("fetched", "inserted", "errors")})
    ok("采集执行", result.get("fetched", 0) >= 3)

    with get_connection() as conn:
        jobs = repo.list_jobs(conn)
    ok("岗位入库", len(jobs) >= 3, f"({len(jobs)} 条)")
    ok("岗位字段完整", all(j.title and j.company and j.salary_min for j in jobs))

    # 4. 投递（半自动：确认回调）
    import asyncio

    async def run_apply():
        from app.services.apply_service import ApplyService as AP
        from app.db import repository as rp
        svc = AP.get()
        svc.reset_cancel()
        svc.set_confirm_callback(lambda job, rn, plat: True)  # 模拟用户确认
        with get_connection() as conn3:
            job_row = rp.list_jobs(conn3)[0]
        # RawJob 构造（投递需要 job.job_id 等）
        from app.collectors.base import RawJob as RJ
        job = RJ(source=job_row.source, job_id=job_row.job_id, title=job_row.title,
                 company=job_row.company, salary_raw=job_row.salary_raw or "",
                 city=job_row.city, job_url=job_row.job_url, crawl_time="", description=job_row.description or "")
        ok_flag = await svc.apply_job("mock", job, "默认简历")
        return ok_flag

    applied = asyncio.run(run_apply())
    ok("半自动投递", applied, f"(apply_job={applied})")

    with get_connection() as conn:
        apps = repo.list_applications(conn)
    ok("投递记录入库", len(apps) >= 1, f"({len(apps)} 条)")
    ok("投递状态", apps[0].status == "submitted")

    # 5. 简历管理
    from app.services.resume_service import ResumeService
    rsvc = ResumeService.get()
    import tempfile
    tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    tmp.write("姓名：张三\n技能：Python Java\n经历：5年后端\n")
    tmp.close()
    r = rsvc.add_resume("测试简历", "Java开发", tmp.name)
    ok("简历入库", r is not None)
    ok("简历文本提取", len(rsvc.get_resume(r["id"]).content) > 10)
    ok("简历删除", rsvc.delete_resume(r["id"]))
    import os
    os.unlink(tmp.name)

    # 6. GUI 展示（离屏）
    import os as _os
    _os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    from app.ui.main_window import MainWindow
    app = QApplication([])
    w = MainWindow()
    ok("GUI 启动", w.windowTitle() == "个人求职助手")
    job_page = w.page("岗位列表")
    with get_connection() as conn:
        jobs = repo.list_jobs(conn)
    job_page.set_jobs([j.as_dict() for j in jobs])
    ok("岗位列表展示", job_page._model.rowCount() >= 3, f"(行数={job_page._model.rowCount()})")

    print(f"\n=== 验收完成：{len(PASS)} 项全部通过 ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
