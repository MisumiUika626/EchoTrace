"""Optional Edge browser acceptance using fictional records and an HTTP model stub.

Requires Playwright (pip install playwright) and an installed Microsoft Edge.
Never connects to a real model or real QCE.
"""
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from conftest import FIXTURE, FakeModel
from echotrace.app import create_app


def main():
    output = ROOT / "data" / "verification" / str(time.time_ns())
    output.mkdir(parents=True)
    temp = ROOT / "web" / ".build-tmp"
    temp.mkdir(exist_ok=True)
    os.environ.update(TEMP=str(temp), TMP=str(temp))
    model = FakeModel()
    app = create_app(output / "app-data", llm_transport=httpx.MockTransport(model),
                     qce_transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    app.state.config.update({"llm": {"base_url": "http://fiction.invalid/v1", "api_key": "fiction-only"},
                             "privacy": {"acknowledged": True}})
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(.05)
    errors, failed_http, requests = [], [], []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: failed_http.append(f"{response.status} {response.url}") if response.status >= 500 else None)
            page.on("request", lambda request: requests.append(request.url))
            page.goto(f"http://127.0.0.1:{port}")
            expect(page.get_by_text("还没有人物档案", exact=True)).to_be_visible()
            page.screenshot(path=str(output / "01-home.png"), full_page=True)
            page.get_by_role("button", name="导入聊天记录", exact=True).first.click()
            page.locator('input[type="file"]').set_input_files(str(FIXTURE))
            page.get_by_role("button", name="解析并预览", exact=True).click()
            page.locator("label.speaker").filter(has_text="fiction-me").locator('input').check()
            page.get_by_label("人物称呼", exact=True).fill("林间（虚构）")
            page.get_by_label("对方 QQ 号或唯一标识").fill("fiction-them")
            page.get_by_role("button", name="确认并导入", exact=True).click()
            page.get_by_role("button", name="查看人物", exact=False).first.click(timeout=30000)
            expect(page.locator(".health-total")).to_contain_text("5,000")
            page.screenshot(path=str(output / "02-messages.png"), full_page=True)
            page.get_by_role("button", name="开始蒸馏", exact=True).click()
            page.get_by_label("我同意本次将所选上下文发送给设置中的模型服务商").check()
            page.get_by_role("button", name="确认，开始蒸馏", exact=True).click()
            expect(page.locator('.job-card')).to_have_count(2)
            expect(page.locator('.job-card .badge.green')).to_have_count(2, timeout=60000)
            page.locator('.job-card').filter(has_text="人物蒸馏").get_by_role("button", name="查看人物").click()
            page.locator(".tabs").get_by_role("button", name="人格肖像").click()
            expect(page.locator(".persona-section")).to_have_count(8)
            page.locator(".evidence-link").first.click()
            expect(page.locator(".message.highlight")).to_have_count(1)
            page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
            page.screenshot(path=str(output / "03-persona.png"), full_page=True)
            page.locator(".tabs").get_by_role("button", name="关系雷达").click()
            expect(page.locator(".radar-dimensions button")).to_have_count(8)
            expect(page.locator(".radar-panel svg")).to_be_visible()
            expect(page.locator(".timeline-event").first).to_be_visible()
            page.locator(".timeline-event .evidence-link").first.click()
            expect(page.locator(".message.highlight")).to_have_count(1)
            page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
            page.get_by_label("观察范围", exact=True).select_option("last_30")
            expect(page.locator(".dimension-panel")).to_contain_text("所选范围的统计锚点")
            page.get_by_label("观察范围", exact=True).select_option("all")
            page.screenshot(path=str(output / "04-radar.png"), full_page=True)
            page.locator(".tabs").get_by_role("button", name="重要话语").click()
            expect(page.locator(".quote-card").first).to_be_visible()
            quote = page.locator(".quote-card blockquote").first.inner_text()
            page.get_by_label("搜索重要话语与共同记忆…").fill(quote[:4])
            expect(page.locator(".quote-card").first).to_be_visible()
            page.locator(".tabs").get_by_role("button", name="人物 Skill").click()
            expect(page.locator(".quality-row")).to_have_count(6)
            expect(page.locator(".quality-panel .badge")).to_contain_text("通过")
            calls_before_rebuild = len(model.calls)
            page.get_by_role("button", name="本地重建为新版本", exact=True).click()
            expect(page.locator('.job-card')).to_have_count(3)
            expect(page.locator('.job-card .badge.green')).to_have_count(3, timeout=30000)
            assert len(model.calls) == calls_before_rebuild, "Local Skill rebuild contacted the model"
            page.locator('.job-card').filter(has_text="人物蒸馏").first.get_by_role("button", name="查看人物").click()
            page.locator(".tabs").get_by_role("button", name="人物 Skill").click()
            expect(page.locator(".quality-row")).to_have_count(6)
            expect(page.locator(".quality-panel .badge")).to_contain_text("通过")
            page.screenshot(path=str(output / "05-skill.png"), full_page=True)
            page.locator(".tabs").get_by_role("button", name="对话求助").click()
            page.get_by_label("你想说什么，或卡在了哪里？").fill("周末想邀请对方看展，怎么问比较自然？")
            page.get_by_label("我同意本次将所选上下文发送给设置中的模型服务商").check()
            page.get_by_role("button", name="帮我想回复", exact=True).click()
            expect(page.locator(".advice-card")).to_have_count(3)
            page.get_by_role("button", name="重新生成这一条", exact=True).first.click()
            expect(page.get_by_role("button", name="重新生成这一条", exact=True).first).to_be_enabled()
            page.locator(".advice-card .evidence-link").first.click()
            expect(page.locator(".message.highlight")).to_have_count(1)
            page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
            page.screenshot(path=str(output / "06-advice.png"), full_page=True)
            page.get_by_role("button", name="与复刻人格对话", exact=True).click()
            page.get_by_role("button", name="开始模拟", exact=True).click()
            expect(page.locator(".simulation-turn")).to_have_count(2)
            expect(page.locator(".simulation-turn").last).to_contain_text("好呀，周末再一起看看")
            page.locator(".tabs").get_by_role("button", name="版本演化").click()
            expect(page.locator(".version-card")).to_have_count(2)
            page.locator(".sidebar").get_by_role("button", name="设置", exact=True).click()
            expect(page.get_by_label("模型 ID", exact=True)).to_have_value("deepseek-v4.1-flash")
            page.get_by_role("button", name="高级设置", exact=True).click()
            expect(page.locator(".scale-field input")).to_have_count(8)
            page.locator(".sidebar").get_by_role("button", name="导出中心", exact=True).click()
            expect(page.get_by_text("先连接你的 QQ 导出工具", exact=True)).to_be_visible()
            expect(page.get_by_role("button", name="我先导入本地文件")).to_be_visible()
            page.screenshot(path=str(output / "07-qce-guide.png"), full_page=True)
            navigation_viewports = [{"width": 1280, "height": 600}, {"width": 1024, "height": 480},
                                    {"width": 820, "height": 400}, {"width": 390, "height": 600}]
            for viewport in navigation_viewports:
                page.set_viewport_size(viewport)
                page.goto(f"http://127.0.0.1:{port}/#people")
                if viewport["width"] <= 680:
                    page.locator(".mobile-menu").click()
                settings_button = page.locator(".sidebar").get_by_role("button", name="设置", exact=True)
                expect(settings_button).to_be_in_viewport(ratio=1)
                settings_button.click()
                expect(page.get_by_label("模型 ID", exact=True)).to_have_value("deepseek-v4.1-flash")
                if viewport["width"] == 1024:
                    page.screenshot(path=str(output / "09-settings-short-window.png"))
            for route in ("people", "person/1", "export", "jobs", "settings"):
                page.set_viewport_size({"width": 390, "height": 844})
                page.goto(f"http://127.0.0.1:{port}/#{route}")
                page.wait_for_timeout(250)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1"), f"Mobile overflow: {route}"
            page.screenshot(path=str(output / "08-mobile.png"), full_page=True)
            browser.close()
        assert not errors, errors
        assert not failed_http, failed_http
        assert all(url.startswith(f"http://127.0.0.1:{port}") for url in requests), requests
        result = {"status": "passed", "fictional_messages": 5000, "model_calls": len(model.calls),
                  "local_skill_rebuild": {"version": 2, "model_calls": 0},
                  "settings_navigation_viewports": navigation_viewports,
                  "browser": "Microsoft Edge / Playwright", "javascript_errors": errors,
                  "http_5xx": failed_http, "external_browser_requests": 0,
                  "screenshots": [p.name for p in output.glob("*.png")]}
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({**result, "output": str(output)}, ensure_ascii=False))
    finally:
        server.should_exit = True
        thread.join(timeout=15)


if __name__ == "__main__":
    main()
