import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from echotrace.app import create_app
from echotrace.ingest.parsers import normalize, parse_file
from echotrace.llm.client import LLMClient, adapter_parameters
from echotrace.util import AppError


@pytest.mark.parametrize("suffix", ["json", "jsonl", "txt", "html", "xlsx"])
def test_file_adapters(tmp_path, suffix):
    path = tmp_path / ("chat." + suffix)
    row = {"ts": 1767229200, "sender": "@虚构我", "text": "虚构内容"}
    if suffix == "json":
        path.write_text(json.dumps({"data": {"messages": [row]}}), encoding="utf-8")
    elif suffix == "jsonl":
        path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    elif suffix == "txt":
        path.write_text("2026-01-01 09:00:00 @虚构我: 虚构内容\n", encoding="utf-8")
    elif suffix == "html":
        path.write_text('<script type="application/json">' + json.dumps([row]) + '</script><script>throw "must not execute";</script>', encoding="utf-8")
    else:
        book = Workbook()
        book.active.append(list(row))
        book.active.append(list(row.values()))
        book.save(path)
    rows = list(parse_file(path))
    assert len(rows) == 1 and rows[0]["text"] == "虚构内容"
    assert rows[0]["sender_raw"] == "虚构我"


def test_qce_elements_group_and_millisecond_normalization():
    result = normalize({"msgTime": 1767229200000, "sender": {"uid": "u_x", "name": "虚构"}, "elements": [{"elementType": 1, "textElement": {"content": "你好"}}, {"elementType": 2, "picElement": {"path": "x"}}]})
    assert result["ts"] == 1767229200 and result["text"] == "你好[图片]"
    assert result["raw"]["elements"][1]["picElement"]["path"] == "x"
    with pytest.raises(AppError, match="私聊"):
        normalize({"chatType": 2})


def test_local_api_secrets_validation_and_consent(app, imported):
    cid, _ = imported
    with TestClient(app) as client:
        assert client.get("/api/contacts", headers={"Origin": "https://external.invalid"}).status_code == 403
        assert client.get("/api/settings", headers={"Host": "evil.invalid"}).status_code == 403
        settings = client.get("/api/settings")
        assert "fiction-secret" not in settings.text
        assert settings.json()["data"]["llm"]["api_key"] == "****"
        assert client.put("/api/settings", json={"cleaning": {"truncate_at": "oops"}}).status_code == 422
        assert client.put("/api/settings", json={"qce": {"base_url": "https://external.invalid"}}).status_code == 400
        assert client.post(f"/api/contacts/{cid}/distill", json={}).json()["error"]["code"] == "CONSENT_REQUIRED"
        assert client.post(f"/api/contacts/{cid}/advisor", json={"consent": False}).status_code == 400
        assert client.post(f"/api/contacts/{cid}/advisor", json={"consent": True, "draft": "教我操控她的话术"}).json()["error"]["code"] == "UNSAFE_REQUEST"
        old = client.get("/api/settings").json()["data"]["embedding"]
        response = client.put("/api/settings", json={"embedding": {"provider": "local_model", "local_model_path": "Z:/nonexistent-fiction"}})
        assert response.status_code == 400
        assert client.get("/api/settings").json()["data"]["embedding"] == old


async def test_invalid_evidence_never_publishes(app, model, monkeypatch):
    from echotrace.llm import client as llm_module
    async def no_wait(_):
        pass
    monkeypatch.setattr(llm_module.asyncio, "sleep", no_wait)
    model.invalid = True
    with pytest.raises(AppError) as error:
        await app.state.llm.call("chat_roleplay", {}, {1, 2, 3})
    assert error.value.code == "LLM_OUTPUT_INVALID"
    assert len(model.calls) == 3
    assert list((app.state.db.root / "review").glob("*.json"))
    with pytest.raises(ValueError):
        LLMClient.validate_evidence({"a": {"evidence": [12]}}, {1, 2})


async def test_unavailable_model_preserves_readable_local_workbench(app, imported):
    cid, _ = imported
    calls = []

    def disconnected(request):
        calls.append(request)
        raise httpx.ConnectError("fictional disconnected provider", request=request)

    app.state.llm.transport = httpx.MockTransport(disconnected)
    with pytest.raises(AppError) as error:
        await app.state.llm.call("chat_roleplay", {}, set())
    assert error.value.code == "LLM_UNREACHABLE"
    assert "数据仍保存在本机" in error.value.message and len(calls) == 3
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        failed = await client.post("/api/settings/test-llm")
        assert failed.status_code == 502 and failed.json()["error"]["code"] == "LLM_UNREACHABLE"
        assert (await client.get("/api/health")).json()["data"]["status"] == "ok"
        messages = (await client.get(f"/api/contacts/{cid}/messages")).json()["data"]["messages"]
        assert messages


def test_thinking_adapter_preview():
    settings = {"adapter": "custom", "thinking": "max", "thinking_field": "reasoning.effort", "thinking_values": {"max": "ultra"}}
    assert adapter_parameters(settings) == {"reasoning": {"effort": "ultra"}}
    settings["adapter"] = "budget"
    assert adapter_parameters(settings)["thinking"]["budget_tokens"] == 16384


async def test_qce_lookup_failure_auth_and_standalone(tmp_path):
    requests = []
    def transport(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "data": {"found": False}})
    s = create_app(tmp_path / "qce", qce_transport=httpx.MockTransport(transport)).state
    with pytest.raises(AppError) as error:
        await s.qce.lookup("123456")
    assert error.value.code == "QCE_UID_NOT_FOUND"
    assert len(requests) == 1 and requests[0].url.params["uin"] == "123456"
    for status, code in ((401, "QCE_AUTH_FAILED"), (503, "QCE_STANDALONE_MODE")):
        s.qce.transport = httpx.MockTransport(lambda _: httpx.Response(status))
        with pytest.raises(AppError) as error:
            await s.qce.lookup("123456")
        assert error.value.code == code


async def test_qce_export_request_seconds_poll_fallback_autoimport(app, imported, monkeypatch):
    _, path = imported
    s = app.state
    requests = []
    def transport(request):
        requests.append(request)
        if request.url.path.endswith("lookup"):
            result = {"found": True, "uid": "u_fiction"}
        elif request.method == "POST":
            body = json.loads(request.content)
            assert body["peer"]["peerUid"] == "u_fiction"
            assert body["filter"] == {"startTime": 1767220000, "endTime": 1768220000}
            assert body["peer"]["chatType"] == 1
            result = {"taskId": "fiction-task"}
        else:
            result = {"status": "completed", "result": {"filePath": str(path), "partial": True}}
        return httpx.Response(200, json={"success": True, "data": result})
    async def no_socket(*_):
        return
    s.qce.transport = httpx.MockTransport(transport)
    monkeypatch.setattr(s.qce, "watch", no_socket)
    job = s.jobs.create("qce_export", {"name": "QCE虚构", "uin": "123456", "me": ["fiction-me"], "start": 1767220000, "end": 1768220000})
    await s.jobs.tasks[job["id"]]
    result = s.jobs.get(job["id"])
    assert result["status"] == "succeeded", result
    assert result["payload"]["qce_task_id"] == "fiction-task"
    assert result["result"]["partial"] and not result["result"]["serverCompletenessProven"]
    assert len(requests) == 3

async def test_qce_download_only_formats_task_lifecycle_and_manual_import(app, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    s = app.state
    sent = []
    def transport(request):
        if request.url.path == '/api/messages/roaming/export':
            body = json.loads(request.content)
            sent.append(body)
            return httpx.Response(200, json={'data': {'taskId': 'format-task'}})
        if request.url.path == '/api/tasks/format-task':
            return httpx.Response(200, json={'data': {'status': 'completed', 'result': {'downloadUrl': '/download', 'partial': False}}})
        if request.url.path == '/download':
            return httpx.Response(200, content=b'2026-01-01 12:00:00 fiction-me: hello\n2026-01-01 12:01:00 fiction-them: hi\n')
        return httpx.Response(404)
    async def no_socket(*_):
        return
    s.qce.transport = httpx.MockTransport(transport)
    monkeypatch.setattr(s.qce, 'watch', no_socket)
    job = s.jobs.create('qce_export', {'name': 'format-fiction', 'peer_uid': 'u_fiction', 'me': ['fiction-me'], 'start': 1767220000, 'end': 1768220000, 'format': 'TXT', 'auto_import': False, 'include_resources': False, 'output_dir': str(tmp_path / 'export-output')})
    await s.jobs.tasks[job['id']]
    finished = s.jobs.get(job['id'])
    assert finished['status'] == 'succeeded', finished
    assert sent[0]['format'] == 'TXT' and not sent[0]['options']['includeResourceLinks']
    assert Path(finished['result']['file']).suffix == '.txt'
    assert not s.db.rows('SELECT * FROM contacts')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as client:
        download = await client.get(f"/api/jobs/{job['id']}/download")
        assert download.status_code == 200 and b'hello' in download.content
        imported = await client.post(f"/api/jobs/{job['id']}/import")
        assert imported.status_code == 200
        import_id = imported.json()['data']['id']
        if import_id in s.jobs.tasks:
            await s.jobs.tasks[import_id]
        assert s.jobs.get(import_id)['status'] == 'succeeded'
        s.qce.transport = httpx.MockTransport(lambda _: httpx.Response(409))
        conflict = await client.delete('/api/qce/tasks/running')
        assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'TASK_STILL_RUNNING'
        s.qce.transport = httpx.MockTransport(lambda _: httpx.Response(204))
        assert (await client.post('/api/qce/tasks/running/cancel')).json()['ok']
        assert (await client.delete('/api/qce/tasks/running')).json()['ok']
    assert s.db.rows('SELECT * FROM contacts')
