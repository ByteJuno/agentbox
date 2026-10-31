"""可选真实浏览器回归：真实静态资源 + 隔离 aiohttp + 合成 CLI，不消费模型额度。

先安装 Playwright 及 Chromium，再运行：
  AGENT_HUB_TG=0 <venv>/python hub/tests/browser_compat.py
已有浏览器可用 AGENT_HUB_BROWSER_EXECUTABLE 指定。所有状态只写临时目录。
"""
import asyncio
import os

from playwright.async_api import async_playwright
from test_hub_compat import CompatTest


async def main():
    fixture = CompatTest()
    await fixture.asyncSetUp()
    errors, failures = [], []
    try:
        async with async_playwright() as p:
            options = {"headless": True}
            if os.environ.get("AGENT_HUB_BROWSER_EXECUTABLE"):
                options["executable_path"] = os.environ["AGENT_HUB_BROWSER_EXECUTABLE"]
            async with await p.chromium.launch(**options) as browser:
                context = await browser.new_context(viewport={"width": 1440, "height": 1000}, reduced_motion="reduce", locale="zh-CN")
                page = await context.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                # 不记录查询串，WS/下载的令牌不能写进输出。
                page.on("response", lambda r: failures.append((r.status, r.url.split("?")[0])) if r.status >= 400 else None)
                await page.goto(str(fixture.client.make_url("/")))
                await page.locator("#login-user").fill("lab")
                await page.locator("#login-pass").fill(fixture.token)
                await page.locator("#login-btn").click()
                await page.locator("#app:not(.hidden)").wait_for()
                base = "/sessions/" + fixture.w["id"]

                async def route(path):
                    await page.evaluate("target => { location.hash = target; }", path)
                    await page.wait_for_timeout(300)

                await route(base + "/chat")
                await page.locator("#chat-input").fill("Synthetic browser prompt")
                await page.locator("#chat-send").click()
                for _ in range(100):
                    if fixture.executions:
                        break
                    await asyncio.sleep(.02)
                assert len(fixture.executions) == 1, "browser send did not start exactly one turn"
                tid = fixture.executions[0][0]
                assert fixture.hub.sessions[tid].mode == "bypassPermissions"
                await page.reload()
                await page.locator("#chat-input").wait_for(state="visible")
                await page.wait_for_function("document.querySelector('#chat-send').getAttribute('aria-label') === '中断'")
                session = fixture.hub.sessions[tid]
                fixture.hub.forward_raw(session, "assistant", {"type": "assistant", "message": {"content": [
                    {"type": "thinking", "thinking": "Synthetic reasoning"},
                    {"type": "text", "text": "Synthetic browser answer"},
                    {"type": "tool_use", "id": "read-browser", "name": "Read", "input": {"file_path": session.cwd + "/README.md"}}]}})
                fixture.hub.forward_raw(session, "user", {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "read-browser", "content": "Synthetic file content"}]}})
                await fixture.complete(tid)
                await page.get_by_text("Synthetic browser answer", exact=True).first.wait_for()
                await page.wait_for_function("document.querySelector('#chat-send').getAttribute('aria-label') === '发送'")
                assert len(fixture.executions) == 1, "reload executed the prompt twice"
                for tab in ("files", "changes", "skills"):
                    await route(base + "/" + tab)
                    assert await page.locator("#tab-" + tab).is_visible()
                await route("/settings/interface")
                assert await page.locator("#set-max-parallel").is_visible()
                assert await page.locator("#set-history-timeout").is_visible()
                await page.locator("#set-max-parallel").fill("2")
                await page.locator("#set-max-parallel").dispatch_event("change")
                await page.locator("#set-history-timeout").fill("65")
                await page.locator("#set-history-timeout").dispatch_event("change")
                await page.locator("#set-save").click()
                await page.wait_for_function("document.querySelector('#set-savebar').hidden")
                settings = await fixture.request("GET", "/api/settings")
                assert settings["max_parallel_chats"] == 2
                assert settings["chat_history_timeout_sec"] == 65
                await route("/settings/monitor")
                await page.locator("#mon-tiles .mon-tile").first.wait_for()
                await route("/settings/about")
                await page.locator("#about-kv dd").first.wait_for()
                await route("/usage")
                await page.wait_for_timeout(300)
                await page.set_viewport_size({"width": 390, "height": 844})
                await route(base + "/chat")
                assert await page.locator("#chat-input").is_visible()
                await context.close()
        assert not errors, errors
        assert not failures, failures
        print("PASS: real frontend login, send/reload/tool rows, files/git/skills, grouped settings save, monitor/about/usage, narrow viewport; synthetic CLI only")
    finally:
        await fixture.asyncTearDown()


if __name__ == "__main__":
    asyncio.run(main())
