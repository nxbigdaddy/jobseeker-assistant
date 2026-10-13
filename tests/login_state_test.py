# -*- coding: utf-8 -*-
"""验证 is_logged_in 保守判定修复：本地 fixture 模拟三种页面状态。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, '.')

FIXTURES = Path("data/fixtures")
FIXTURES.mkdir(parents=True, exist_ok=True)

# 模拟"已登录"：含用户菜单
(FIXTURES / "logged_in.html").write_text(
    '<html><body><div class="username">张三</div><a href="/i/">个人中心</a></body></html>',
    encoding="utf-8")
# 模拟"未登录-有登录表单"
(FIXTURES / "login_form.html").write_text(
    '<html><body><input type="password" placeholder="密码"><div class="login-form"></div></body></html>',
    encoding="utf-8")
# 模拟"未登录-无任何标记"（旧代码会误判已登录）
(FIXTURES / "blank.html").write_text(
    '<html><body><div>普通首页内容，无用户菜单无登录表单</div></body></html>',
    encoding="utf-8")
# 模拟"未登录-但有'个人中心/退出'文字链接"（曾导致 text= 选择器误判已登录）
(FIXTURES / "text_links.html").write_text(
    '<html><body><header><a href="/login">个人中心</a><a href="/login">退出</a>'
    '<a href="/login">我的51job</a></header></body></html>',
    encoding="utf-8")


async def main():
    import functools
    import http.server
    import threading

    # 本地 HTTP 服务（file:// 不以 http 开头，会走导航分支；http:// 才能测 DOM 分支）
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}/"

    from playwright.async_api import async_playwright
    from app.collectors.wuyou51 import Wuyou51Collector
    from app.collectors.zhaopin import ZhaopinCollector
    from app.collectors.liepin import LiepinCollector

    collectors = {
        "51job": Wuyou51Collector(),
        "zhaopin": ZhaopinCollector(),
        "liepin": LiepinCollector(),
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        all_ok = True
        for name, col in collectors.items():
            page = await browser.new_page()
            r_ok = await col.is_logged_in(page)  # about:blank 场景（异常应返回 False）
            status = "PASS" if r_ok is False else "FAIL"
            if r_ok is not False:
                all_ok = False
            print(f"[{status}] {name} about:blank -> is_logged_in={r_ok}（应为 False）")

            await page.goto(base + "logged_in.html")
            r_ok = await col.is_logged_in(page)
            status = "PASS" if r_ok is True else "FAIL"
            if r_ok is not True:
                all_ok = False
            print(f"[{status}] {name} 已登录页 -> is_logged_in={r_ok}（应为 True）")

            await page.goto(base + "login_form.html")
            r_ok = await col.is_logged_in(page)
            status = "PASS" if r_ok is False else "FAIL"
            if r_ok is not False:
                all_ok = False
            print(f"[{status}] {name} 登录表单页 -> is_logged_in={r_ok}（应为 False）")

            await page.goto(base + "blank.html")
            r_ok = await col.is_logged_in(page)
            status = "PASS" if r_ok is False else "FAIL"
            if r_ok is not False:
                all_ok = False
            print(f"[{status}] {name} 无标记页 -> is_logged_in={r_ok}（应为 False，旧代码误判 True）")

            await page.goto(base + "text_links.html")
            r_ok = await col.is_logged_in(page)
            status = "PASS" if r_ok is False else "FAIL"
            if r_ok is not False:
                all_ok = False
            print(f"[{status}] {name} 含个人中心文字链接页 -> is_logged_in={r_ok}（应为 False，text= 误判回归）")

            await page.close()
        await browser.close()

    print("=== 全部通过 ===" if all_ok else "=== 存在失败 ===")
    sys.exit(0 if all_ok else 1)


asyncio.run(main())
