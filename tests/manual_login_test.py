# -*- coding: utf-8 -*-
"""验证：反自动化检测 + 手动登录流程（模拟用户关闭浏览器）。"""
import asyncio
import sys

sys.path.insert(0, '.')

from app.collectors.base import Collector, RawJob


class MockCol(Collector):
    source = "mock"

    async def goto_login(self, page):
        await page.goto("about:blank")

    async def is_logged_in(self, page):
        return True

    async def login_qr(self, page, qr_callback):
        return True

    async def login_sms(self, page, phone, code):
        return True

    async def search_jobs(self, page, keyword, city, max_pages=3):
        return []

    async def parse_job(self, page, job_url):
        return None

    async def open_apply(self, page, job):
        return None

    async def submit_apply(self, page, resume_name):
        return True

    async def send_greeting(self, page, job, text):
        return True


async def main():
    from app.services.auth_service import AuthService
    from app.browser.driver import BrowserManager

    AuthService.register("mock", MockCol)

    # ---- 1. 反自动化检测验证 ----
    mgr = BrowserManager("mock_anti", headless=True)
    await mgr.start()
    page = await mgr.new_page()
    await page.goto("about:blank")
    wd = await page.evaluate("navigator.webdriver")
    print(f"[{'PASS' if wd is None else 'FAIL'}] navigator.webdriver 应为 None，实际: {wd}")
    await mgr.stop()

    # ---- 2. 手动登录流程（模拟用户登录后关闭浏览器）----
    svc = AuthService.get()
    task = asyncio.create_task(svc.manual_login("mock", timeout_seconds=60))
    await asyncio.sleep(4)  # 等浏览器打开
    print("[info] 浏览器已打开，模拟用户关闭浏览器…")
    mgr2 = BrowserManager.get("mock")
    await mgr2.context.close()  # 模拟用户点关闭窗口
    result = await asyncio.wait_for(task, timeout=90)
    print(f"[{'PASS' if result else 'FAIL'}] 手动登录验证结果: {result}（期望 True）")

    print("=== 验证完成 ===")


asyncio.run(main())
