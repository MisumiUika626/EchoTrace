import httpx
import pytest

from echotrace.distill.extraction import extract
from echotrace.llm.client import LLMClient
from echotrace.util import AppError, dumps, read_json
from test_large_distill import extraction_job, message


@pytest.mark.parametrize("stage", ["distill_chunk", "extract_quotes"])
async def test_leaf_growth_survives_restart_without_repeating_completed_sibling(app, model, tmp_path, monkeypatch, stage):
    import echotrace.llm.client as client_module

    async def no_wait(_):
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)
    s = app.state
    s.config.update({"llm": {"max_tokens": 393216}})
    jid = extraction_job(s)
    context = {"messages": [message(1), message(2)], "target": "them"}
    path = tmp_path / "extract.json"
    disconnected = True

    def provider(request):
        response = model(request)
        _, ctx, body = model.calls[-1]
        if len(ctx["messages"]) == 2 or (ctx["messages"][0]["id"] == 2 and body["max_tokens"] < 16384):
            result = response.json()
            result["choices"][0]["finish_reason"] = "length"
            return httpx.Response(200, json=result)
        if disconnected and ctx["messages"][0]["id"] == 2:
            raise httpx.ConnectError("synthetic disconnect", request=request)
        return response

    transport = httpx.MockTransport(provider)
    s.llm.transport = transport
    with pytest.raises(AppError) as error:
        await extract(s.llm, s.jobs, jid, {}, stage, context, path, "growth-test")
    assert error.value.code == "LLM_UNREACHABLE"
    sibling = path.with_name(path.name + ".0.json")
    saved = sibling.read_bytes()
    assert [body["max_tokens"] for _, ctx, body in model.calls if len(ctx["messages"]) == 1 and ctx["messages"][0]["id"] == 2] == [4096, 8192, 16384, 16384, 16384]
    assert s.jobs.get(jid)["tokens"] == 4 * 150
    assert s.llm.reserved[jid] == 0

    # A fresh client reads the saved allowance, just as a process restart would.
    resumed = LLMClient(s.config, s.jobs, transport)
    disconnected = False
    seen = len(model.calls)
    output = await extract(resumed, s.jobs, jid, {}, stage, context, path, "growth-test")
    assert len(model.calls) == seen + 1
    assert model.calls[-1][2]["max_tokens"] == 16384
    assert len(output["observations" if stage == "distill_chunk" else "quotes"]) == 2
    assert sibling.read_bytes() == saved
    assert s.jobs.get(jid)["tokens"] == 5 * 150
    assert not list((s.db.root / "review").glob("output-*.json"))


@pytest.mark.parametrize("at_depth_boundary", [False, True])
async def test_leaf_growth_reaches_effective_ceiling_and_reports_actual_limits(app, model, tmp_path, at_depth_boundary):
    s = app.state
    s.config.update({"llm": {"max_tokens": 393216, "context_window": 16384}})
    jid = extraction_job(s)
    context = {"messages": [message(i) for i in range(1, 4 if at_depth_boundary else 2)], "target": "them"}

    def provider(request):
        response = model(request)
        result = response.json()
        result["choices"][0]["message"]["content"] = ""
        result["choices"][0]["finish_reason"] = "length"
        result["usage"]["completion_tokens_details"] = {"reasoning_tokens": 50}
        return httpx.Response(200, json=result)

    s.llm.transport = httpx.MockTransport(provider)
    path = tmp_path / "bounded.json"
    with pytest.raises(AppError) as error:
        await extract(s.llm, s.jobs, jid, {}, "distill_chunk", context, path, "bounded", depth=6 if at_depth_boundary else 0)
    assert error.value.code == "LLM_OUTPUT_TRUNCATED"
    caps = [body["max_tokens"] for _, _, body in model.calls]
    assert caps[:2] == [4096, 8192]
    assert 8192 < caps[-1] < 16384 and len(caps) == 3
    assert "393,216" in error.value.message and "16,384" in error.value.message
    assert "继续提高设置的输出上限未必有效" in error.value.message
    assert not path.with_name(path.name + ".split.json").exists()
    review = next(p for p in (s.db.root / "review").glob("*.json") if p.name.startswith(jid))
    data = read_json(review)
    assert data["requested_max_tokens"] == caps[-1]
    assert data["configured_max_tokens"] == 393216
    assert data["usage"]["completion_tokens_details"]["reasoning_tokens"] == 50
    assert s.jobs.get(jid)["tokens"] == 3 * 150 and s.llm.reserved[jid] == 0


async def test_growing_leaf_stops_before_exceeding_task_budget(app, model, tmp_path):
    s = app.state
    s.config.update({"llm": {"max_tokens": 393216}})
    jid = extraction_job(s)
    s.db.execute("UPDATE jobs SET payload=? WHERE id=?", (dumps({"token_limit": 16000}), jid))

    def provider(request):
        response = model(request)
        result = response.json()
        result["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=result)

    s.llm.transport = httpx.MockTransport(provider)
    with pytest.raises(AppError) as error:
        await extract(s.llm, s.jobs, jid, {}, "extract_quotes", {"messages": [message(1)]}, tmp_path / "budget.json", "budget")
    assert error.value.code == "TOKEN_BUDGET_EXCEEDED"
    assert [body["max_tokens"] for _, _, body in model.calls] == [4096, 8192]
    assert s.jobs.get(jid)["tokens"] == 300 and s.llm.reserved[jid] == 0
    state = next((s.db.root / "review").glob("output-*.json"))
    assert read_json(state)["growth"] == 2


@pytest.mark.parametrize("ceiling", [4096, 393216])
async def test_configured_ceiling_remains_binding_for_leaf(app, model, tmp_path, ceiling):
    s = app.state
    s.config.update({"llm": {"max_tokens": ceiling, "context_window": 524288}})
    jid = extraction_job(s)

    def provider(request):
        response = model(request)
        result = response.json()
        result["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=result)

    s.llm.transport = httpx.MockTransport(provider)
    with pytest.raises(AppError) as error:
        await extract(s.llm, s.jobs, jid, {}, "extract_quotes", {"messages": [message(1)]}, tmp_path / "ceiling.json", "ceiling")
    assert error.value.code == "LLM_OUTPUT_TRUNCATED"
    assert "当前已达到设置的单次输出上限" in error.value.message
    caps = [body["max_tokens"] for _, _, body in model.calls]
    assert caps == ([4096] if ceiling == 4096 else [4096, 8192, 16384, 32768, 65536, 131072, 262144, 393216])
    assert s.jobs.get(jid)["tokens"] == len(caps) * 150
