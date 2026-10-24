"""Quote extraction must be able to report no supported quotes without looping."""
import httpx
import pytest

from echotrace.distill.extraction import extract, validate_observations
from echotrace.distill.quotes import validate_quotes
from echotrace.llm.client import LLMClient
from echotrace.util import AppError, dumps, read_json, write_json
from conftest import claim
from test_large_distill import extraction_job, message


def quote(mid, text="虚构原话"):
    return {"evidence": [mid], "text": text, "topic": "约定", "reuse": 5,
            "emotion": 5, "relation": 5, "info": 5, "reason": "虚构测试", "sensitive": False}


@pytest.fixture
def no_retry_wait(monkeypatch):
    import echotrace.llm.client as client_module

    async def no_wait(_):
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)


@pytest.mark.parametrize("sender", [None, "me", "A", "system"])
async def test_no_subject_messages_produces_empty_checkpoint_without_model(app, tmp_path, sender):
    s = app.state
    jid = extraction_job(s)
    s.llm.transport = httpx.MockTransport(lambda _: pytest.fail("No subject source: do not call the provider"))
    messages = [] if sender is None else [message(1, sender=sender)]
    path = tmp_path / "quotes.json"
    output = await extract(s.llm, s.jobs, jid, {}, "extract_quotes", {"messages": messages}, path, "same")
    assert output == {"quotes": []}
    assert read_json(path)["reason"] == "no_target_messages"
    assert s.jobs.get(jid)["tokens"] == 0


async def test_old_reply_metadata_failure_at_split_boundary_recovers_without_rebilling(app, model, tmp_path, no_retry_wait):
    s = app.state
    jid = extraction_job(s)
    # The quoted platform ID is NOT an allowed database message ID. The
    # current speaker only says "好"; the embedded quotation isn't their speech.
    reply = message(1, sender="me", text=dumps({"text": "[回复消息]好", "elements": [
        {"type": "reply", "data": {"messageId": "9876543210123456789", "content": "虚构约定周末看展"}}]}))
    context = {"messages": [reply], "target": "them"}
    invalid = {"quotes": [quote(9876543210123456789, "虚构约定周末看展")]}

    def provider(request):
        body = model(request).json()
        body["choices"][0]["message"]["content"] = dumps(invalid)
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    with pytest.raises(AppError) as error:
        await s.llm.call("extract_quotes", context, {1}, jid, {}, lambda doc: validate_quotes(doc, [reply]))
    assert error.value.code == "LLM_OUTPUT_INVALID"
    review = next((s.db.root / "review").glob("repair-*.json"))
    old_review = review.read_bytes()
    before = s.jobs.get(jid)["tokens"]
    assert before == 450
    s.llm = LLMClient(s.config, s.jobs, httpx.MockTransport(lambda _: pytest.fail("Must recover locally")))
    path = tmp_path / "quotes.json"
    split = path.with_name(path.name + ".split.json")
    write_json(split, {"fingerprint": "unchanged", "reason": "LLM_OUTPUT_INVALID"})
    result = await extract(s.llm, s.jobs, jid, {}, "extract_quotes", context, path, "unchanged", depth=6)
    assert result == {"quotes": []}
    assert s.jobs.get(jid)["tokens"] == before
    assert review.read_bytes() == old_review  # Original rejected output retained.
    assert read_json(path)["fingerprint"] == "unchanged"
    assert await extract(s.llm, s.jobs, jid, {}, "extract_quotes", context, path, "unchanged", depth=6) == result


@pytest.mark.parametrize("resume_old_checkpoint", [False, True])
@pytest.mark.parametrize("keep_valid", [False, True])
async def test_empty_quote_repair_retracts_only_bad_rows_and_audits_them(app, model, tmp_path, no_retry_wait,
                                                                     resume_old_checkpoint, keep_valid):
    s = app.state
    jid = extraction_job(s)
    messages = [message(1), message(2)]
    context = {"messages": messages, "target": "them"}
    good = [quote(m["id"], m["text"]) for m in messages] if keep_valid else []
    rows = [quote(999, "无依据的虚构句子"), *good, quote(998, "另一条无依据的句子")]
    legacy = resume_old_checkpoint

    def provider(request):
        body = model(request).json()
        ctx = model.calls[-1][1]
        result = {"quotes": rows}
        if "output_repair" in ctx:
            assert [x["index"] for x in ctx["output_repair"]["items"]] == [0, len(rows) - 1]
            result = {"quotes": [rows[x["index"]] for x in ctx["output_repair"]["items"]] if legacy else []}
        body["choices"][0]["message"]["content"] = dumps(result)
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    if legacy:
        with pytest.raises(AppError):
            await s.llm.call("extract_quotes", context, {1, 2}, jid, {}, lambda doc: validate_quotes(doc, messages))
        legacy = False
        s.llm = LLMClient(s.config, s.jobs, httpx.MockTransport(provider))
    calls_before = len(model.calls)
    tokens_before = s.jobs.get(jid)["tokens"]
    path = tmp_path / "quotes.json"
    result = await extract(s.llm, s.jobs, jid, {}, "extract_quotes", context, path, "same")
    assert result["quotes"] == good
    call_count = 1 if resume_old_checkpoint else 2
    assert len(model.calls) - calls_before == call_count
    assert s.jobs.get(jid)["tokens"] - tokens_before == 150 * call_count
    audit = read_json(next((s.db.root / "review").glob("withdrawn-quotes-*.json")))
    assert [x["item"] for x in audit["items"]] == [rows[0], rows[-1]]
    assert audit["needs_review"] is True
    assert not list((s.db.root / "review").glob("repair-*.json"))
    assert not path.with_name(path.name + ".split.json").exists()


async def test_partial_nonempty_quote_repair_cannot_silently_drop_rows(app, model, no_retry_wait):
    s = app.state
    context = {"messages": [message(1)], "target": "them"}

    def provider(request):
        body = model(request).json()
        repairing = "output_repair" in model.calls[-1][1]
        output = {"quotes": [quote(1, message(1)["text"])] if repairing else [quote(998), quote(999)]}
        body["choices"][0]["message"]["content"] = dumps(output)
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    with pytest.raises(AppError, match="数量不得变化"):
        await s.llm.call("extract_quotes", context, {1}, validator=lambda doc: validate_quotes(doc, context["messages"]))
    assert len(model.calls) == 3
    assert not list((s.db.root / "review").glob("withdrawn-quotes-*.json"))


async def test_observation_repair_cannot_retract_rows_using_quote_exception(app, model, no_retry_wait):
    s = app.state
    context = {"messages": [message(1)]}

    def provider(request):
        body = model(request).json()
        repairing = "output_repair" in model.calls[-1][1]
        row = {**claim([999]), "dimension": "interests", "quote": "虚构", "date": "2026-01-01"}
        body["choices"][0]["message"]["content"] = dumps({"observations": [] if repairing else [row]})
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    with pytest.raises(AppError, match="数量不得变化"):
        await s.llm.call("distill_chunk", context, {1}, validator=lambda doc: validate_observations(doc, context["messages"]))
    assert len(model.calls) == 3
