"""Recover contradictory source labels without promoting unsupported claims."""
import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from conftest import claim
from echotrace.distill.nuwa import (
    NuwaWorkflow, normalize_research_sources, validate_research_sources,
)
from echotrace.llm.client import LLMClient
from echotrace.llm.schemas import SCHEMAS
from echotrace.util import AppError, dumps, read_json, write_json
from test_large_distill import extraction_job, message


FILE = "04-external-views.md"


def finding(ids, **changes):
    return {**claim(ids), "source_kind": "direct", "inferred": True,
            "note": "仅依据所给聊天作推断，不代表独立第三方证实。", **changes}


def result():
    return {"findings": [finding([1]), finding([1, 2]),
                         finding([2], source_kind="reported", inferred=False)],
            "contradictions": [finding([1])], "gaps": ["缺少独立第三方评价。"]}


def context():
    return {"file": FILE, "dimension": "他者观察与批评",
            "evidence_messages": [message(1), message(2, sender="me")]}


@pytest.fixture
def no_retry_wait(monkeypatch):
    import echotrace.llm.client as client_module

    async def no_wait(_):
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)


def test_only_explicit_inference_labels_change_and_all_content_is_preserved():
    output = result()
    before = copy.deepcopy(output)
    corrections = normalize_research_sources(output, context()["evidence_messages"])
    validate_research_sources(output, context()["evidence_messages"], FILE)
    SCHEMAS["nuwa_research"].model_validate(output)
    assert len(corrections) == 3
    assert corrections[-1]["field"] == "contradictions"
    assert output["findings"][2] == before["findings"][2]
    for field in ("findings", "contradictions"):
        for original, fixed in zip(before[field], output[field]):
            expected = {**original, "source_kind": "inference"} if original["source_kind"] == "direct" else original
            assert fixed == expected
    assert output["gaps"] == before["gaps"]
    assert normalize_research_sources(output, context()["evidence_messages"]) == []


@pytest.mark.parametrize("changes", [
    {"inferred": False}, {"note": ""}, {"evidence": [999]},
    {"evidence": [1, 999]}, {"evidence": []},
])
def test_incomplete_or_unknown_sources_are_never_reclassified(changes):
    output = {"findings": [finding([1], **changes)], "contradictions": [], "gaps": []}
    before = copy.deepcopy(output)
    assert normalize_research_sources(output, context()["evidence_messages"]) == []
    assert output == before
    with pytest.raises(ValueError):
        parsed = SCHEMAS["nuwa_research"].model_validate(output).model_dump()
        LLMClient.validate_evidence(parsed, {1, 2})
        validate_research_sources(parsed, context()["evidence_messages"], FILE)


def test_actual_primary_source_in_other_dimension_remains_direct():
    output = {"findings": [finding([1], inferred=False, note="")], "contradictions": [], "gaps": []}
    assert normalize_research_sources(output, context()["evidence_messages"]) == []
    validate_research_sources(output, context()["evidence_messages"], "01-persona.md")
    output["findings"][0]["evidence"] = [2]
    with pytest.raises(ValueError, match="一手观点"):
        validate_research_sources(output, context()["evidence_messages"], "01-persona.md")


@pytest.mark.parametrize("resume", [False, True])
async def test_real_gateway_and_workflow_checkpoint_recover_without_rebilling(app, model, tmp_path, no_retry_wait, resume):
    s = app.state
    jid = extraction_job(s)
    ctx = context()
    validate = lambda doc: validate_research_sources(doc, ctx["evidence_messages"], FILE)
    normalize = lambda doc: normalize_research_sources(doc, ctx["evidence_messages"])

    def provider(request):
        body = model(request).json()
        body["choices"][0]["message"]["content"] = dumps(result())
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    original_review = None
    if resume:
        with pytest.raises(AppError) as error:
            await s.llm.call("nuwa_research", ctx, {1, 2}, jid, {}, validate)
        assert error.value.code == "LLM_OUTPUT_INVALID"
        review = next((s.db.root / "review").glob(f"{jid}-nuwa_research-*.json"))
        original_review = review.read_bytes()
        s.llm = LLMClient(s.config, s.jobs, httpx.MockTransport(lambda _: pytest.fail("Must recover locally")))
    calls_before, tokens_before = len(model.calls), s.jobs.get(jid)["tokens"]
    workflow = NuwaWorkflow.__new__(NuwaWorkflow)
    workflow.llm, workflow.jobs, workflow.job_id, workflow.payload = s.llm, s.jobs, jid, {}
    workflow.root, workflow.semaphore = tmp_path / "nuwa", asyncio.Semaphore(1)
    workflow.root.mkdir()
    checkpoint = FILE.replace(".md", ".json")
    output = await workflow.request("nuwa_research", checkpoint, ctx, validate, normalizer=normalize)
    assert len(output["findings"]) == 3 and len(output["contradictions"]) == 1
    assert output["findings"][0]["source_kind"] == "inference"
    assert output["findings"][2]["source_kind"] == "reported"
    assert len(model.calls) - calls_before == (0 if resume else 1)
    assert s.jobs.get(jid)["tokens"] - tokens_before == (0 if resume else 150)
    audit = read_json(next((s.db.root / "review").glob("attribution-*.json")))
    assert audit["stage"] == "nuwa_research" and len(audit["corrections"]) == 3
    if original_review:
        assert review.read_bytes() == original_review
    stored = (workflow.root / checkpoint).read_bytes()
    assert await workflow.request("nuwa_research", checkpoint, ctx, validate, normalizer=normalize) == output
    assert (workflow.root / checkpoint).read_bytes() == stored
    assert len(model.calls) - calls_before == (0 if resume else 1)


@pytest.mark.parametrize("case", ["unknown_id", "not_inferred", "truncated", "wrong_context", "wrong_job", "malformed", "no_correction"])
async def test_unrecoverable_reviews_still_require_provider_and_do_not_write_audit(app, tmp_path, case):
    s = app.state
    jid = extraction_job(s)
    ctx = context()
    raw = result()
    if case == "unknown_id":
        raw["findings"][0]["evidence"] = [999]
    elif case == "not_inferred":
        raw["findings"][0]["inferred"] = False
    elif case == "no_correction":
        normalize_research_sources(raw, ctx["evidence_messages"])
    review_context = {**ctx, "different_context": True} if case == "wrong_context" else ctx
    suffix = hashlib.sha256(dumps(review_context).encode()).hexdigest()[:12]
    saved_job = "other-job" if case == "wrong_job" else jid
    review_path = s.db.root / "review" / f"{saved_job}-nuwa_research-{suffix}.json"
    write_json(review_path, {"raw": "{" if case == "malformed" else dumps(raw),
                            "error": "LLM_OUTPUT_TRUNCATED" if case == "truncated" else "模型输出校验失败：来源错误"})
    old = review_path.read_bytes()
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(401)

    s.llm.transport = httpx.MockTransport(provider)
    with pytest.raises(AppError) as error:
        await s.llm.call("nuwa_research", ctx, {1, 2}, jid, {},
                         lambda doc: validate_research_sources(doc, ctx["evidence_messages"], FILE),
                         normalizer=lambda doc: normalize_research_sources(doc, ctx["evidence_messages"]))
    assert error.value.code == "LLM_AUTH_FAILED"
    assert len(calls) == 1
    assert not list((s.db.root / "review").glob("attribution-*.json"))
    assert review_path.read_bytes() == old
    assert s.jobs.get(jid)["tokens"] == 0


async def test_six_dimension_workflow_wires_normalizer_and_preserves_other_checkpoints(app, model, tmp_path):
    s = app.state
    jid = extraction_job(s)
    ctx = context()
    workflow = NuwaWorkflow.__new__(NuwaWorkflow)
    workflow.llm, workflow.jobs, workflow.job_id, workflow.payload = s.llm, s.jobs, jid, {}
    workflow.root, workflow.semaphore = tmp_path / "nuwa", asyncio.Semaphore(1)
    workflow.root.mkdir()
    workflow.packet = {"evidence_messages": ctx["evidence_messages"]}
    workflow.dimension_packets = {}
    workflow.research_dir = tmp_path / "research"
    workflow.research_dir.mkdir()
    workflow.checkpoint = lambda *args: None
    other = workflow.root / "01-persona.json"
    write_json(other, {"findings": [finding([1], inferred=False)], "contradictions": [], "gaps": []})
    before = other.read_bytes()

    def provider(request):
        body = model(request).json()
        if model.calls[-1][1]["file"] == FILE:
            body["choices"][0]["message"]["content"] = dumps(result())
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    outputs, summaries = await workflow.research()
    assert len(outputs) == len(summaries) == 6 and len(model.calls) == 5
    assert other.read_bytes() == before
    external = next(item for item in summaries if item["dimension"] == ctx["dimension"])
    assert external["primary"] == 0
    markdown = (workflow.research_dir / FILE).read_text('utf-8')
    assert "研究推断" in markdown and "聊天转述（未经独立核实）" in markdown
    assert "一手原话" not in markdown
