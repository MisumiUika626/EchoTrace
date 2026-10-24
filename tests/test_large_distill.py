import asyncio
import json
import sqlite3
import threading
from contextlib import closing

import httpx
import pytest

from conftest import claim
from echotrace.distill.evidence import EvidenceIndex, DIMENSION_FILTERS
from echotrace.distill.extraction import extract, validate_observations
from echotrace.distill.persona import active_dir
from echotrace.distill.quotes import validate_quotes
from echotrace.llm.client import estimate_tokens
from echotrace.util import AppError, dumps, read_json, write_json, finish_thread
from test_workflow import finish


def message(mid, **changes):
    return {"id": mid, "sender": "them", "ts": 1767229200 + mid * 60, "text": f"虚构原话{mid}，先核对安排。", "kind": "text", **changes}


def extraction_job(state):
    state.db.execute("INSERT INTO jobs(id,kind,status,payload) VALUES('extract-test','distill','running',?)",
                     (dumps({"token_limit": 2000000}),))
    return "extract-test"


async def test_split_tree_resumes_only_failed_child_and_accounts_truncation(app, model, tmp_path, monkeypatch):
    import echotrace.llm.client as client_module
    async def no_wait(_):
        pass
    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)
    s, disconnected = app.state, True
    jid = extraction_job(s)
    context = {"messages": [message(i) for i in range(1, 9)], "target": "them"}
    def provider(request):
        response = model(request)
        _, ctx, _ = model.calls[-1]
        if len(ctx["messages"]) == 8:
            body = response.json()
            body["choices"][0]["finish_reason"] = "length"
            return httpx.Response(200, json=body)
        if disconnected and ctx["messages"][0]["id"] == 5:
            raise httpx.ConnectError("synthetic failure", request=request)
        return response
    s.llm.transport = httpx.MockTransport(provider)
    path = tmp_path / "extract.json"
    with pytest.raises(AppError, match="无法连接"):
        await extract(s.llm, s.jobs, jid, {}, "extract_quotes", context, path, "test-fingerprint")
    assert path.with_name(path.name + ".0.json").exists()
    saved_child = path.with_name(path.name + ".0.json").read_bytes()
    seen = len(model.calls)
    disconnected = False
    output = await extract(s.llm, s.jobs, jid, {}, "extract_quotes", context, path, "test-fingerprint")
    assert len(output["quotes"]) == 8
    assert len(model.calls) == seen + 1
    assert model.calls[-1][1]["messages"][0]["id"] == 5
    assert path.with_name(path.name + ".0.json").read_bytes() == saved_child
    assert s.jobs.get(jid)["tokens"] == 3 * 150  # truncated parent and two successful children


async def test_local_repairs_keep_valid_siblings_across_a_restart(app, model, monkeypatch, tmp_path):
    import echotrace.llm.client as client_module
    async def no_wait(_):
        pass
    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)
    s, disconnected = app.state, True
    jid = extraction_job(s)
    path = tmp_path / "repair-quotes.json"
    context = {"messages": [message(i) for i in range(1, 4)], "target": "them"}
    good = None
    def provider(request):
        nonlocal good
        response = model(request)
        _, ctx, _ = model.calls[-1]
        body = response.json()
        data = json.loads(body["choices"][0]["message"]["content"])
        if "output_repair" in ctx:
            if disconnected:
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            data["quotes"] = [data["quotes"][item["index"]] for item in ctx["output_repair"]["items"]]
        else:
            good = data["quotes"][2].copy()
            data["quotes"][0]["evidence"] = [999999]
            data["quotes"][1]["text"] = "这句话不存在于原文"
        body["choices"][0]["message"]["content"] = dumps(data)
        return httpx.Response(200, json=body)
    s.llm.transport = httpx.MockTransport(provider)
    with pytest.raises(AppError) as error:
        await extract(s.llm, s.jobs, jid, {}, "extract_quotes", context, path, "repair-test")
    assert error.value.code == "LLM_UNREACHABLE"
    assert not path.with_name(path.name + ".split.json").exists()
    assert list((s.db.root / "review").glob("repair-*.json"))
    disconnected = False
    output = await extract(s.llm, s.jobs, jid, {}, "extract_quotes", context, path, "repair-test")
    assert output["quotes"][2] == good
    assert all("output_repair" in ctx for _, ctx, _ in model.calls[1:])
    assert [x["index"] for x in model.calls[-1][1]["output_repair"]["items"]] == [0, 1]
    assert all(m["role"] != "assistant" for _, _, body in model.calls for m in body["messages"])
    assert not list((s.db.root / "review").glob("repair-*.json"))


def test_quote_attribution_rejects_wrong_speaker_and_unrelated_ids():
    messages = [message(1), message(2, sender="me"), message(3)]
    with pytest.raises(ValueError):
        validate_quotes({"quotes": [{"text": messages[0]["text"], "evidence": [2, 1]}]}, messages)
    with pytest.raises(ValueError):
        validate_quotes({"quotes": [{"text": messages[0]["text"], "evidence": [1, 3]}]}, messages)
    with pytest.raises(ValueError):
        validate_observations({"observations": [{**claim([2]), "quote": messages[1]["text"]}]}, messages)


def test_cached_quotes_revalidated_against_database(app, imported):
    from echotrace.rag.indexer import from_model
    cid, _ = imported
    s = app.state
    target = s.db.one("SELECT id,text FROM messages WHERE sender='them' LIMIT 1")
    own = s.db.one("SELECT id FROM messages WHERE sender='me' LIMIT 1")
    quote = {"evidence": [own["id"], target["id"]], "text": target["text"]}
    with pytest.raises(AppError) as error:
        from_model(s.db, cid, [quote])
    assert error.value.code == "INVALID_EVIDENCE"


def test_single_giant_message_and_style_examples_are_bounded():
    from echotrace.distill.extraction import split_messages
    from echotrace.distill.pipeline import bounded_examples, summary_messages
    original = message(1, text="超长记录" * 25000)
    left, right = split_messages([original])
    assert left[0]["id"] == right[0]["id"] == 1
    assert left[0]["text"] + right[0]["text"] == original["text"]
    assert right[0]["fragment_offset"] == len(left[0]["text"])
    selected = bounded_examples([message(i, sender="me", text=original["text"]) for i in range(120)])
    assert selected and sum(estimate_tokens(m) for m in selected) <= 5000
    assert all(len(m["text"]) <= 1000 for m in selected)
    assert summary_messages(selected)[0]["characters"] == len(original["text"])
    assert "超长记录" not in dumps(summary_messages(selected))


async def test_cancel_waits_for_local_writer_before_resume(app):
    s = app.state
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def write_locally():
        started.set()
        release.wait(timeout=5)
        finished.set()
    async def handler(jid, payload):
        await finish_thread(write_locally)
        return {}
    s.jobs.handlers["synthetic-writer"] = handler
    job = s.jobs.create("synthetic-writer", {})
    jid = job["id"]
    try:
        assert await asyncio.to_thread(started.wait, 3)
        stopping = asyncio.create_task(s.jobs.cancel(jid, pause=True))
        await asyncio.sleep(.02)
        assert not stopping.done() and not finished.is_set()
        with pytest.raises(AppError) as error:
            s.jobs.resume(jid)
        assert error.value.code == "JOB_STOPPING"
        release.set()
        await stopping
        assert finished.is_set() and jid not in s.jobs.tasks
        await finish(s, s.jobs.resume(jid))
    finally:
        release.set()


async def test_full_request_window_and_stage_output_ceiling(app, model):
    s = app.state
    s.config.update({"llm": {"context_window": 16384, "max_tokens": 393216}})
    await s.llm.call("chat_roleplay", {}, set())
    assert model.calls[-1][2]["max_tokens"] == 2048
    before = len(model.calls)
    with pytest.raises(AppError) as error:
        await s.llm.call("chat_roleplay", {"long_input": "中文" * 12000}, set())
    assert error.value.code == "LLM_CONTEXT_TOO_LARGE" and len(model.calls) == before
    # The complete Nuwa method is part of the admission calculation, too.
    with pytest.raises(AppError) as error:
        await s.llm.call("nuwa_research", {"file": "01-persona.md", "evidence_messages": [], "long_input": "文" * 15000}, set())
    assert error.value.code == "LLM_CONTEXT_TOO_LARGE" and len(model.calls) == before
    s.config.update({"llm": {"adapter": "budget", "thinking": "max", "max_tokens": 8192}})
    with pytest.raises(AppError) as error:
        await s.llm.call("chat_roleplay", {}, set())
    assert error.value.code == "LLM_OUTPUT_BUDGET_TOO_SMALL"


async def test_recent_radar_ids_are_valid_when_only_recent_context_contains_them(app, imported, model, monkeypatch):
    import echotrace.distill.pipeline as pipeline_module
    cid, _ = imported
    s = app.state
    recent_ids = [r["id"] for r in s.db.rows("SELECT id FROM messages WHERE sender='them' ORDER BY ts DESC LIMIT 3")]
    calculate = pipeline_module.calculate
    def recent_stats(db, contact_id, start=None, end=None):
        stats = calculate(db, contact_id, start, end)
        if start is not None:
            for anchor in stats["anchors"].values():
                anchor["evidence"] = recent_ids
        return stats
    monkeypatch.setattr(pipeline_module, "calculate", recent_stats)
    def provider(request):
        response = model(request)
        stage, ctx, _ = model.calls[-1]
        if stage == "Radar":
            assert not set(recent_ids) <= {mid for ob in ctx["observations"] for mid in ob["evidence"]}
            body = response.json()
            output = json.loads(body["choices"][0]["message"]["content"])
            for dim in output["dimensions"]:
                dim["evidence"] = recent_ids
            body["choices"][0]["message"]["content"] = dumps(output)
            return httpx.Response(200, json=body)
        return response
    s.llm.transport = httpx.MockTransport(provider)
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    assert read_json(active_dir(s.db, cid) / "radar_model.json")["dimensions"][0]["evidence"] == recent_ids


async def test_many_chunks_use_a_fixed_worker_pool(app, imported, monkeypatch):
    import echotrace.distill.pipeline as pipeline_module
    cid, _ = imported
    s = app.state
    s.config.update({"llm": {"concurrency": 2}})
    prepare = pipeline_module.Pipeline.prepare
    def tiny_chunks(self, contact_id, target, payload, stats, check=lambda: None):
        manifest = prepare(self, contact_id, target, payload, stats, check)
        messages = [m for filename in manifest["chunks"] for m in read_json(target / "inputs" / filename)]
        manifest["chunks"] = []
        for offset in range(0, len(messages), 12):
            filename = f"tiny-{offset}.json"
            write_json(target / "inputs" / filename, messages[offset:offset + 12])
            manifest["chunks"].append(filename)
        assert len(manifest["chunks"]) > 20
        write_json(target / "manifest.json", manifest)
        return manifest
    monkeypatch.setattr(pipeline_module.Pipeline, "prepare", tiny_chunks)
    call, workers = s.llm.call, []
    async def inspect_pool(stage, *args, **kwargs):
        if stage == "distill_chunk":
            workers.append(sum(t.get_coro().__qualname__ == "Pipeline._run.<locals>.worker" for t in asyncio.all_tasks()))
            await asyncio.sleep(0)
        return await call(stage, *args, **kwargs)
    monkeypatch.setattr(s.llm, "call", inspect_pool)
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    assert len(workers) > 20 and max(workers) == 2


@pytest.mark.parametrize("size", [100000, 300000, 500000])
def test_large_corpus_disk_hierarchy_and_dimension_retrieval(tmp_path, size):
    # Synthetic pre-extracted observations: exercises storage/selection at scale,
    # without billing a provider or pretending to measure semantic quality.
    (tmp_path / "inputs").mkdir()
    (tmp_path / "chunks").mkdir()
    dimensions = ["personality", "expression_dna", "emotion", "interests", "relationship", "events"]
    names = []
    for start in range(0, size, 1000):
        filename = f"{start // 1000:05d}.json"
        names.append(filename)
        messages = [message(i) for i in range(start + 1, start + 1001)]
        observations = [{**claim([m["id"]], "相同表达在不同日期仍须保留"), "dimension": dimensions[(m["id"] // 20) % 6],
                         "quote": m["text"], "date": "1990-01-01"} for m in messages if m["id"] % 20 == 0]
        (tmp_path / "inputs" / filename).write_text(dumps(messages), encoding="utf-8")
        (tmp_path / "chunks" / filename).write_text(dumps({"observations": observations, "quotes": []}), encoding="utf-8")
    write_json(tmp_path / "manifest.json", {"chunks": names, "total": size, "sampled": size})
    index = EvidenceIndex(tmp_path)
    index.build()
    assert read_json(tmp_path / "coverage.json")["observations"] == size // 20
    with closing(sqlite3.connect(index.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == size
    selected = index.select()
    assert len(selected) <= 240 and sum(estimate_tokens(x) for x in selected) <= 12000
    assert len({item["date"][:7] for item in selected}) > 1
    assert all(item["date"].startswith("2026-") for item in selected)
    for filename, dimensions in DIMENSION_FILTERS.items():
        packet = index.packet(False, dimensions, 2000, 3500)
        assert packet["observations"], filename
        assert all(item["dimension"] in dimensions for item in packet["observations"])
        assert {mid for item in packet["observations"] for mid in item["evidence"]} <= {m["id"] for m in packet["evidence_messages"]}
    anonymized = dumps(index.packet(True))
    assert "虚构原话" not in anonymized
