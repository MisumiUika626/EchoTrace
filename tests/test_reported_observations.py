import httpx
import pytest

from conftest import claim
from echotrace.distill.extraction import extract, normalize_reported_observations, validate_observations
from echotrace.llm.client import LLMClient
from echotrace.llm.schemas import SCHEMAS
from echotrace.util import AppError, dumps, read_json
from test_large_distill import extraction_job, message


def reported(mid, quote, **changes):
    return {**claim([mid]), "inferred": True, "note": "依据另一位发言人的描述，不能确认本人想法。",
            "dimension": "relationship", "quote": quote, "date": "2026-01-01", **changes}


@pytest.mark.parametrize("sender", ["me", "A"])
def test_verified_other_speaker_quote_is_preserved_as_inference_not_subject_quote(sender):
    source = message(1, sender=sender)
    item = reported(1, source["text"], note="推断依据" * 100)
    output = {"observations": [item]}
    corrections = normalize_reported_observations(output, [source])
    validate_observations(output, [source])
    SCHEMAS["distill_chunk"].model_validate(output)
    assert item["quote"] == "" and item["inferred"] is True
    assert item["note"].startswith("仅依据另一位发言人的话推断，非目标本人原话")
    assert corrections[0]["original_quote"] == source["text"]
    assert corrections[0]["original_note"] == "推断依据" * 100
    assert item["evidence"] == [1] and item["value"] == claim([1])["value"]
    assert normalize_reported_observations(output, [source]) == []


@pytest.mark.parametrize("case", ["direct", "fabricated", "mixed", "missing", "target"])
def test_normalization_does_not_excuse_invalid_evidence_or_wrong_attribution(case):
    own, target = message(1, sender="me"), message(2)
    item = reported(1, own["text"])
    if case == "direct":
        item["inferred"] = False
    elif case == "fabricated":
        item["quote"] = "这段话不存在"
    elif case == "mixed":
        item["evidence"] = [1, 2]
    elif case == "missing":
        item["evidence"] = [1, 999]
    elif case == "target":
        item["evidence"] = [2]
    output = {"observations": [item]}
    assert normalize_reported_observations(output, [own, target]) == []
    with pytest.raises(ValueError):
        LLMClient.validate_evidence(output, {1, 2})
        validate_observations(output, [own, target])


def test_target_quote_is_unchanged():
    source = message(1)
    item = reported(1, source["text"], inferred=False, note="")
    output = {"observations": [item]}
    assert normalize_reported_observations(output, [source]) == []
    validate_observations(output, [source])
    assert item["quote"] == source["text"]


@pytest.mark.parametrize("resume_old_checkpoint", [False, True])
async def test_reported_quote_recovery_keeps_all_rows_and_does_not_rebill(app, model, tmp_path, monkeypatch, resume_old_checkpoint):
    import echotrace.llm.client as client_module

    async def no_wait(_):
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)
    s = app.state
    jid = extraction_job(s)
    own, target = message(1, sender="me"), message(2)
    messages = [own, target]
    context = {"messages": messages}
    valid = {**claim([2]), "dimension": "expression_dna", "quote": target["text"], "date": "2026-01-01"}
    output = {"observations": [reported(1, own["text"]), valid], "open_questions": []}

    def provider(request):
        response = model(request)
        body = response.json()
        result = output
        if "output_repair" in model.calls[-1][1]:
            result = {"observations": [output["observations"][0]]}
        body["choices"][0]["message"]["content"] = dumps(result)
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    if resume_old_checkpoint:
        # Generate an authentic legacy repair checkpoint using strict validation
        # without the newly introduced normalizer.
        with pytest.raises(AppError) as error:
            await s.llm.call("distill_chunk", context, {1, 2}, jid, {}, lambda doc: validate_observations(doc, messages))
        assert error.value.code == "LLM_OUTPUT_INVALID"
        assert len(model.calls) == 3
        s.llm = LLMClient(s.config, s.jobs, httpx.MockTransport(lambda _: pytest.fail("Local recovery must not contact provider")))
    calls_before = len(model.calls)
    tokens_before = s.jobs.get(jid)["tokens"]
    path = tmp_path / "observations.json"
    result = await extract(s.llm, s.jobs, jid, {}, "distill_chunk", context, path, "unchanged-fingerprint")
    assert len(result["observations"]) == 2
    assert result["observations"][0]["quote"] == ""
    assert result["observations"][0]["inferred"] is True
    assert result["observations"][1]["quote"] == target["text"]
    assert not path.with_name(path.name + ".split.json").exists()
    assert len(model.calls) == calls_before + (0 if resume_old_checkpoint else 1)
    assert s.jobs.get(jid)["tokens"] == tokens_before + (0 if resume_old_checkpoint else 150)
    audit = next((s.db.root / "review").glob("attribution-*.json"))
    assert read_json(audit)["corrections"][0]["original_quote"] == own["text"]
    assert not list((s.db.root / "review").glob("repair-*.json"))
    assert await extract(s.llm, s.jobs, jid, {}, "distill_chunk", context, path, "unchanged-fingerprint") == result


@pytest.mark.parametrize("resume_old_checkpoint", [False, True])
async def test_fabricated_quote_still_needs_model_repair_while_reported_row_is_retained(app, model, tmp_path, monkeypatch, resume_old_checkpoint):
    import echotrace.llm.client as client_module

    async def no_wait(_):
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)
    s = app.state
    jid = extraction_job(s)
    own, target = message(1, sender="me"), message(2)
    context = {"messages": [own, target]}
    legacy = resume_old_checkpoint

    def provider(request):
        response = model(request)
        _, ctx, _ = model.calls[-1]
        result = {"observations": [reported(1, own["text"]), reported(2, "捏造的原话")]}
        if "output_repair" in ctx:
            if legacy:
                result = {"observations": [result["observations"][item["index"]] for item in ctx["output_repair"]["items"]]}
            else:
                assert [item["index"] for item in ctx["output_repair"]["items"]] == [1]
                result = {"observations": [reported(2, target["text"])]}
        body = response.json()
        body["choices"][0]["message"]["content"] = dumps(result)
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    if legacy:
        with pytest.raises(AppError):
            await s.llm.call("distill_chunk", context, {1, 2}, jid, {}, lambda doc: validate_observations(doc, [own, target]))
        legacy = False
    calls_before = len(model.calls)
    result = await extract(s.llm, s.jobs, jid, {}, "distill_chunk", context, tmp_path / "mixed.json", "mixed")
    assert len(model.calls) - calls_before == (1 if resume_old_checkpoint else 2)
    assert result["observations"][0]["quote"] == ""
    assert result["observations"][1]["quote"] == target["text"]
