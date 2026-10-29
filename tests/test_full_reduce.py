import json

import httpx
import pytest

from echotrace.distill.full_reduce import (
    deduplicate_findings,
    map_chunk,
    plan_reduce_groups,
    reduce_findings,
    validate_findings,
)
from echotrace.distill.radar import timeline_events
from echotrace.llm.schemas import SCHEMAS
from echotrace.util import AppError, dumps, read_json
from test_large_distill import extraction_job


def _messages():
    return [
        {"id": "m000001", "sequence": 0, "sender": "me", "text": "我觉得她最近更喜欢先确认时间。", "ts": 1},
        {"id": "m000002", "sequence": 1, "sender": "them", "text": "先看看大家哪天有空，再决定去哪。", "ts": 2},
        {"id": "m000003", "sequence": 2, "sender": "them", "text": "临时变化也没关系。", "ts": 3},
    ]


def _finding(**changes):
    item = {
        "topic": "安排前确认条件",
        "value": "对方在共同安排前倾向先确认参与者的时间。",
        "evidence": ["m000002"],
        "source_kind": "direct",
        "inferred": False,
        "confidence": 0.8,
        "note": "",
        "quote": "先看看大家哪天有空",
    }
    item.update(changes)
    return item


def test_full_map_accepts_open_topic_and_direct_target_evidence():
    document = {"findings": [_finding(topic="自定义开放主题")], "open_questions": []}

    parsed = SCHEMAS["full_map"].model_validate(document).model_dump()
    validated = validate_findings(parsed, _messages(), {"m000001", "m000002", "m000003"})

    assert validated["findings"][0]["topic"] == "自定义开放主题"


def test_open_topic_can_explicitly_feed_timeline(app, imported):
    cid, _ = imported
    finding = _finding(timeline_event=True)

    events = timeline_events(app.state.db, cid, [finding])

    assert len(events) == 1
    assert events[0]["evidence"] == ["m000002"]


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"evidence": ["m999999"]}, "授权范围"),
        ({"evidence": ["m000002", "m000002"]}, "重复"),
        ({"evidence": ["m000001"]}, "目标本人"),
        ({"source_kind": "reported", "evidence": ["m000001"], "note": ""}, "转述"),
        ({"source_kind": "inference", "inferred": True, "note": ""}, "推断"),
        ({"quote": "并不存在的逐字原话"}, "逐字"),
    ],
)
def test_evidence_validator_rejects_unauthorized_or_misattributed_findings(changes, match):
    document = {"findings": [_finding(**changes)], "open_questions": []}

    with pytest.raises(ValueError, match=match):
        validate_findings(document, _messages(), {"m000001", "m000002", "m000003"})


def test_reported_claim_from_other_speaker_requires_explicit_limit():
    document = {"findings": [_finding(
        source_kind="reported", evidence=["m000001"], inferred=True,
        note="这是另一位发言人的转述，未得到目标本人确认。", quote="",
    )], "open_questions": []}

    validated = validate_findings(document, _messages(), {"m000001", "m000002"})

    assert validated["findings"][0]["source_kind"] == "reported"


def test_overlap_deduplication_merges_evidence_without_voting_twice():
    first = _finding(evidence=["m000002"], confidence=0.7)
    duplicate = _finding(evidence=["m000002", "m000003"], confidence=0.9)
    distinct = _finding(topic="面对变化", value="对方允许安排临时调整。",
                        evidence=["m000003"], quote="临时变化也没关系")

    result = deduplicate_findings([first, duplicate, distinct])

    assert len(result) == 2
    assert result[0]["evidence"] == ["m000002", "m000003"]
    assert result[0]["confidence"] == 0.9
    assert result[1]["topic"] == "面对变化"


def test_owned_ids_prevent_overlap_only_observation_from_becoming_new_output():
    document = {"findings": [_finding(evidence=["m000002"])], "open_questions": []}

    with pytest.raises(ValueError, match="首次覆盖"):
        validate_findings(document, _messages(), {"m000001", "m000002", "m000003"},
                          owned_ids={"m000003"})


async def test_full_map_repairs_once_then_reuses_success_checkpoint(app, tmp_path, monkeypatch):
    import echotrace.llm.client as client_module

    async def no_wait(_):
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)
    calls = []

    def provider(request):
        body = json.loads(request.content)
        calls.append(body)
        output = {"findings": [_finding(evidence=["m999999"] if len(calls) == 1 else ["m000002"])],
                  "open_questions": []}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": dumps(output)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })

    app.state.llm.transport = httpx.MockTransport(provider)
    job_id = extraction_job(app.state)
    path = tmp_path / "map.json"
    first = await map_chunk(app.state.llm, app.state.jobs, job_id, {}, _messages(),
                            {"m000002"}, path, "same")
    second = await map_chunk(app.state.llm, app.state.jobs, job_id, {}, _messages(),
                             {"m000002"}, path, "same")

    assert first == second
    assert first["status"] == "succeeded"
    assert len(calls) == 2
    assert "校验失败" in calls[1]["messages"][-1]["content"]


async def test_full_map_stops_after_one_failed_repair_and_records_failure(app, tmp_path, monkeypatch):
    import echotrace.llm.client as client_module

    async def no_wait(_):
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)
    calls = []

    def provider(request):
        calls.append(1)
        output = {"findings": [_finding(evidence=["m999999"])], "open_questions": []}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": dumps(output)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })

    app.state.llm.transport = httpx.MockTransport(provider)
    job_id = extraction_job(app.state)
    path = tmp_path / "failed-map.json"
    result = await map_chunk(app.state.llm, app.state.jobs, job_id, {}, _messages(),
                             {"m000002"}, path, "same")

    assert len(calls) == 2
    assert result["status"] == "failed"
    assert result["error"]["code"] == "LLM_OUTPUT_INVALID"
    assert read_json(path) == result


def _many_findings(count=36):
    return [_finding(topic=f"主题{index}", value=f"观察{index}：" + "甲" * 560,
                     evidence=["m000002"] if index % 2 == 0 else ["m000003"], quote="")
            for index in range(count)]


def _reduce_provider(calls, preserve=False):
    def provider(request):
        body = json.loads(request.content)
        prompt = next(message["content"] for message in body["messages"] if message["role"] == "user")
        context = json.loads([line for line in prompt.splitlines() if line.startswith("{")][-1])
        calls.append(context)
        incoming = context["findings"]
        output_findings = incoming if preserve else [incoming[0]]
        output = {"findings": output_findings, "contradictions": [], "unknowns": []}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": dumps(output)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        })
    return provider


async def test_small_full_reduce_uses_direct_request(app, tmp_path):
    calls = []
    app.state.llm.transport = httpx.MockTransport(_reduce_provider(calls))
    job_id = extraction_job(app.state)

    result = await reduce_findings(app.state.llm, app.state.jobs, job_id, {},
                                   [_finding(), _finding(topic="变化", value="允许临时调整", evidence=["m000003"], quote="")],
                                   _messages(), tmp_path / "reduce.json", "direct")

    assert result["status"] == "succeeded" and result["reduce_layers"] == 0
    assert len(calls) == 1 and calls[0]["intermediate"] is False


async def test_large_full_reduce_uses_one_deterministic_intermediate_layer(app, tmp_path):
    app.state.config.update({"llm": {"context_window": 16384, "max_tokens": 4096}})
    findings = _many_findings()
    settings = app.state.llm.settings({})
    first = plan_reduce_groups(app.state.llm, settings, findings)
    second = plan_reduce_groups(app.state.llm, settings, findings)
    assert first == second and len(first) > 1
    calls = []
    app.state.llm.transport = httpx.MockTransport(_reduce_provider(calls))
    job_id = extraction_job(app.state)

    result = await reduce_findings(app.state.llm, app.state.jobs, job_id, {}, findings,
                                   _messages(), tmp_path / "reduce.json", "layered")

    assert result["status"] == "succeeded" and result["reduce_layers"] == 1
    assert len(calls) == len(first) + 1
    assert all(call["intermediate"] is True for call in calls[:-1])
    assert calls[-1]["intermediate"] is False


async def test_full_reduce_refuses_a_second_intermediate_layer(app, tmp_path):
    app.state.config.update({"llm": {"context_window": 16384, "max_tokens": 4096}})
    findings = _many_findings()
    calls = []
    app.state.llm.transport = httpx.MockTransport(_reduce_provider(calls, preserve=True))
    job_id = extraction_job(app.state)

    with pytest.raises(AppError) as error:
        await reduce_findings(app.state.llm, app.state.jobs, job_id, {}, findings,
                              _messages(), tmp_path / "reduce.json", "too-large")

    assert error.value.code == "REDUCE_CONTEXT_TOO_LARGE"
    assert calls and all(call["intermediate"] is True for call in calls)


async def test_intermediate_checkpoints_are_bound_to_group_content(app, tmp_path):
    app.state.config.update({"llm": {"context_window": 16384, "max_tokens": 4096}})
    path = tmp_path / "reduce.json"
    first_findings = _many_findings()
    first_calls = []
    app.state.llm.transport = httpx.MockTransport(_reduce_provider(first_calls, preserve=True))
    job_id = extraction_job(app.state)
    with pytest.raises(AppError, match="一层中间归并"):
        await reduce_findings(app.state.llm, app.state.jobs, job_id, {}, first_findings,
                              _messages(), path, "first")

    second_findings = [{**item, "value": item["value"].replace("甲", "乙")}
                       for item in first_findings]
    second_calls = []
    app.state.llm.transport = httpx.MockTransport(_reduce_provider(second_calls))
    result = await reduce_findings(app.state.llm, app.state.jobs, job_id, {}, second_findings,
                                   _messages(), path, "second")

    assert result["status"] == "succeeded"
    assert second_calls and result["output"]["findings"][0]["value"].endswith("乙" * 560)
