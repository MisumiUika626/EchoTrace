import httpx
import pytest
from fastapi.testclient import TestClient

from echotrace.util import dumps
from test_workflow import finish


def test_output_limit_can_be_saved_with_a_paused_job(app):
    s = app.state
    s.db.execute("INSERT INTO jobs(id,kind,status,payload) VALUES('paused-settings','distill','paused','{}')")
    with TestClient(app) as client:
        # The settings page submits all groups, including unchanged embeddings.
        form = client.get("/api/settings").json()["data"]
        for cap in (65536, 65537, 393216):
            form["llm"]["max_tokens"] = cap
            saved = client.put("/api/settings", json=form)
            assert saved.status_code == 200, saved.text
            assert client.get("/api/settings").json()["data"]["llm"]["max_tokens"] == cap
        assert s.jobs.get("paused-settings")["status"] == "paused"
        for invalid in (255, 393217, 8192.5):
            assert client.put("/api/settings", json={"llm": {"max_tokens": invalid}}).status_code == 422
            assert s.config.get()["llm"]["max_tokens"] == 393216
        assert client.put("/api/settings", json={"llm": {"context_window": 262144}}).status_code == 200
        for invalid in (16383, 2097153, 131072.5):
            assert client.put("/api/settings", json={"llm": {"context_window": invalid}}).status_code == 422
            assert s.config.get()["llm"]["context_window"] == 262144
        changed = client.put("/api/settings", json={"embedding": {"provider": "local_model"}})
        assert changed.status_code == 400 and changed.json()["error"]["code"] == "JOB_ALREADY_RUNNING"


@pytest.mark.parametrize("failed_stage,skill_only", [("NuwaResearch", False), ("NuwaResearch", True)])
async def test_increasing_output_limit_retries_only_unfinished_requests(app, imported, model, failed_stage, skill_only):
    s = app.state
    cid, _ = imported
    s.config.update({"llm": {"concurrency": 1}})
    payload = {"contact_id": cid, "consent": True, "token_limit": 2000000}
    if skill_only:
        await finish(s, s.jobs.create("distill", payload.copy()))
        payload.update(skill_only=True, base_version=1)

    successful, failed_signature, matches = set(), None, 0

    def provider(request):
        nonlocal failed_signature, matches
        response = model(request)
        stage, context, body = model.calls[-1]
        signature = (stage, dumps(context))
        if stage == failed_stage:
            matches += 1
            if matches == 2:
                failed_signature = signature
        if signature == failed_signature and body["max_tokens"] < 16384:
            result = response.json()
            result["choices"][0]["finish_reason"] = "length"
            return httpx.Response(200, json=result)
        successful.add(signature)
        return response

    s.llm.transport = httpx.MockTransport(provider)
    calls_start = len(model.calls)
    job = s.jobs.create("distill", payload)
    await s.jobs.tasks[job["id"]]
    failed = s.jobs.get(job["id"])
    assert failed["status"] == "failed" and "8,192" in failed["error"]
    assert "增加任务总预算不能解决" in failed["error"]
    assert failed_signature is not None
    assert failed["tokens"] > 0
    assert s.db.one("SELECT COUNT(*) AS n FROM token_usage WHERE job_id=?", (job["id"],))["n"] == len(model.calls) - calls_start

    target = next((s.db.root / "people").glob(f"*/versions/v{failed['payload']['version']}"))
    for cap in (12288, 16384):
        completed = {path: path.read_bytes() for pattern in ("chunks/*.json", "nuwa/0*.json") for path in target.glob(pattern)}
        assert completed
        already_successful, calls_before = successful.copy(), len(model.calls)
        s.config.update({"llm": {"max_tokens": cap}})
        resumed = s.jobs.resume(job["id"])
        await s.jobs.tasks[resumed["id"]]
        result = s.jobs.get(job["id"])
        assert result["status"] == ("failed" if cap == 12288 else "succeeded"), result
        requests = model.calls[calls_before:]
        assert requests and all(body["max_tokens"] <= cap for _, _, body in requests)
        assert any(body["max_tokens"] == cap for _, _, body in requests)
        signatures = [(stage, dumps(context)) for stage, context, _ in requests]
        assert failed_signature in signatures
        assert not already_successful.intersection(signatures), "Completed request was billed again"
        assert all(path.read_bytes() == content for path, content in completed.items())


async def test_output_limit_relaxation_still_rejects_a_different_model(app, imported, model):
    s = app.state
    cid, _ = imported
    job = s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 1})
    await s.jobs.tasks[job["id"]]
    before = len(model.calls)
    s.config.update({"llm": {"max_tokens": 32768, "model": "different-fiction-model"}})
    resumed = s.jobs.resume(job["id"], 2000000)
    await s.jobs.tasks[resumed["id"]]
    result = s.jobs.get(job["id"])
    assert result["status"] == "failed" and "检查点不一致" in result["error"]
    assert len(model.calls) == before
