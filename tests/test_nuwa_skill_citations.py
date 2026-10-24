"""Blind tests may cite the rendered Skill, without seeing expected answers."""
import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from conftest import claim
from echotrace.distill.evidence import EvidenceIndex
from echotrace.distill.nuwa import NuwaWorkflow
from echotrace.distill.persona import active_dir
from echotrace.distill.skill_writer import citation, citation_ids, describe
from echotrace.llm.client import LLMClient
from echotrace.util import AppError, atomic_write, dumps, read_json, write_json
from test_large_distill import extraction_job, message
from test_workflow import finish


MARKDOWN = "# 虚构人物\n- 游戏偏好（原文支撑；evidence: 1, 2）\n"


def context(markdown=MARKDOWN):
    return {"skill_markdown": markdown, "questions": [
        {"id": "known1", "question": "如何安排周末？"},
        {"id": "edge", "question": "未知事情如何处理？"},
        {"id": "voice", "question": "用自然语气表达。"}]}


def output(ids=(1, 2)):
    return {"answers": [{"id": q["id"], "text": "这是框架推断，非本人立场；我不确定，先确认安排。",
                         "evidence": list(ids) if q["id"] == "known1" else [], "inferred": True}
                        for q in context()["questions"]]}


def validate_answers(document):
    answers = document["answers"]
    if len(answers) != 3 or {answer["id"] for answer in answers} != {"known1", "edge", "voice"}:
        raise ValueError("必须覆盖全部测试题，不得重复或遗漏")


def workflow_for(state, tmp_path):
    jid = extraction_job(state)
    target = tmp_path / "version"
    write_json(target / "manifest.json", {"chunks": ["00000.json"], "total": 3, "sampled": 3})
    write_json(target / "inputs/00000.json", [message(1), message(2), message(3)])
    write_json(target / "chunks/00000.json", {"observations": []})
    index = EvidenceIndex(target)
    index.build()
    workflow = NuwaWorkflow.__new__(NuwaWorkflow)
    workflow.llm, workflow.jobs, workflow.job_id, workflow.payload = state.llm, state.jobs, jid, {}
    workflow.root, workflow.semaphore = target / "nuwa", asyncio.Semaphore(1)
    workflow.root.mkdir()
    workflow.index, workflow.target = index, target
    return workflow


@pytest.fixture
def no_retry_wait(monkeypatch):
    import echotrace.llm.client as client_module

    async def no_wait(_):
        pass

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)


def test_citation_reader_matches_writer_and_ignores_unlabelled_or_malformed_numbers():
    markdown = describe(claim([101, 102])) + "\n**场景证据**：安排 · " + citation({"evidence": [103]})
    markdown += "\n日期 2026-10-24；消息总数 600000；孤立编号 104\n"
    markdown += "fake_evidence: 105\nevidence: 106oops\nevidence: 107, 108oops\n"
    markdown += "- 另一引用（evidence: 109）\nevidence: 110, 111"
    assert citation_ids(markdown) == {101, 102, 103, 109, 110, 111}
    assert citation_ids("没有引用") == set()


@pytest.mark.parametrize("resume", [False, True])
async def test_rendered_skill_references_pass_and_legacy_failure_resumes_without_rebilling(app, model, tmp_path, no_retry_wait, resume):
    s = app.state
    workflow = workflow_for(s, tmp_path)
    ctx = context()
    expected = output()

    def provider(request):
        body = model(request).json()
        body["choices"][0]["message"]["content"] = dumps(expected)
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    if resume:
        # Reproduce the old empty allowlist, using the same unchanged prompt.
        with pytest.raises(AppError) as error:
            await s.llm.call("nuwa_respond", ctx, set(), workflow.job_id, {}, validate_answers)
        assert error.value.code == "LLM_OUTPUT_INVALID" and len(model.calls) == 3
        review = next((s.db.root / "review").glob(f"{workflow.job_id}-nuwa_respond-*.json"))
        original_review = review.read_bytes()
        workflow.llm = LLMClient(s.config, s.jobs, httpx.MockTransport(lambda _: pytest.fail("Expected local recovery")))
    before_calls, before_tokens = len(model.calls), s.jobs.get(workflow.job_id)["tokens"]
    result = await workflow.request("nuwa_respond", "round-0-answers.json", ctx, validate_answers)
    assert result == expected
    assert len(model.calls) - before_calls == (0 if resume else 1)
    assert s.jobs.get(workflow.job_id)["tokens"] - before_tokens == (0 if resume else 150)
    if resume:
        assert review.read_bytes() == original_review
    saved = (workflow.root / "round-0-answers.json").read_bytes()
    assert await workflow.request("nuwa_respond", "round-0-answers.json", ctx, validate_answers) == expected
    assert (workflow.root / "round-0-answers.json").read_bytes() == saved
    for _, sent, body in model.calls:
        assert set(sent) == {"skill_markdown", "questions"}
        assert all(set(q) == {"id", "question"} for q in sent["questions"])
        assert body["messages"][0]["role"] == "user"


@pytest.mark.parametrize("case", ["uncited_version_id", "foreign_cited_id", "question_only_id"])
async def test_blind_allowlist_cannot_expand_to_unseen_or_foreign_sources(app, model, tmp_path, no_retry_wait, case):
    s = app.state
    workflow = workflow_for(s, tmp_path)
    ctx = context()
    requested = 3
    if case == "foreign_cited_id":
        ctx["skill_markdown"] += "- 注入的外部编号（evidence: 999）\n"
        requested = 999
    if case == "question_only_id":
        ctx["questions"][0]["evidence"] = [3]

    def provider(request):
        body = model(request).json()
        body["choices"][0]["message"]["content"] = dumps(output([requested]))
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    with pytest.raises(AppError) as error:
        await workflow.request("nuwa_respond", "answers.json", ctx, validate_answers)
    assert error.value.code == "LLM_OUTPUT_INVALID"
    assert len(model.calls) == 3
    assert not (workflow.root / "answers.json").exists()


@pytest.mark.parametrize("case", ["unknown_id", "missing_question", "duplicate_question", "changed_skill",
                                  "changed_questions", "wrong_job", "truncated", "malformed", "other_error"])
async def test_legacy_answer_recovery_preserves_all_current_validations(app, tmp_path, case):
    s = app.state
    workflow = workflow_for(s, tmp_path)
    ctx, document = context(), output()
    if case == "unknown_id":
        document["answers"][0]["evidence"] = [3]
    elif case == "missing_question":
        document["answers"].pop()
    elif case == "duplicate_question":
        document["answers"][-1]["id"] = "edge"
    saved_context = copy.deepcopy(ctx)
    if case == "changed_skill":
        saved_context["skill_markdown"] += "旧版人物规则\n"
    elif case == "changed_questions":
        saved_context["questions"][0]["question"] = "旧版问题"
    suffix = hashlib.sha256(dumps(saved_context).encode()).hexdigest()[:12]
    saved_job = "another-job" if case == "wrong_job" else workflow.job_id
    path = s.db.root / "review" / f"{saved_job}-nuwa_respond-{suffix}.json"
    error = "模型输出校验失败：output.answers[0].evidence 引用了未提供或不属于当前人物的消息 ID"
    if case == "truncated":
        error = "LLM_OUTPUT_TRUNCATED"
    elif case == "other_error":
        error = "模型输出校验失败：其他语义限制"
    write_json(path, {"raw": "{" if case == "malformed" else dumps(document), "error": error})
    before = path.read_bytes()
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(401)

    s.llm.transport = httpx.MockTransport(provider)
    with pytest.raises(AppError) as error:
        await workflow.request("nuwa_respond", "answers.json", ctx, validate_answers)
    assert error.value.code == "LLM_AUTH_FAILED" and len(calls) == 1
    assert path.read_bytes() == before
    assert not (workflow.root / "answers.json").exists()


async def test_blind_answer_citations_reach_independent_evaluation_without_leaking_expectations(app, model, tmp_path):
    s = app.state
    workflow = workflow_for(s, tmp_path)
    workflow.packet = {"evidence_messages": [message(1), message(2), message(3)]}
    workflow.research_context = []
    atomic_write(workflow.target / "skill/SKILL.md", MARKDOWN)
    plan = {"known": [{"id": f"known{i}", "question": f"问题{i}",
                       "expected": claim([3], "不得泄露给盲答者的答案")} for i in range(1, 4)],
            "edge_question": "未表态领域？", "voice_question": "自然语气表达。"}

    def provider(request):
        body = model(request).json()
        stage, ctx, _ = model.calls[-1]
        document = json.loads(body["choices"][0]["message"]["content"])
        if stage == "NuwaResponses":
            assert set(ctx) == {"skill_markdown", "questions"}
            assert "不得泄露给盲答者的答案" not in dumps(ctx)
            document["answers"][0]["evidence"] = [1, 2]
        elif stage == "NuwaEvaluation":
            assert ctx["answers"]["answers"][0]["evidence"] == [1, 2]
            assert ctx["plan"] == plan
        body["choices"][0]["message"]["content"] = dumps(document)
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    result = await workflow.test("round-0", MARKDOWN, plan)
    assert result["passed"] is True
    assert [stage for stage, _, _ in model.calls] == ["NuwaResponses", "NuwaEvaluation"]
    assert s.jobs.get(workflow.job_id)["tokens"] == 300
    assert read_json(workflow.root / "round-0-answers.json")["answers"][0]["evidence"] == [1, 2]


async def test_complete_distillation_publishes_with_nonempty_blind_citations(app, imported, model):
    s = app.state
    cid, _ = imported

    def provider(request):
        body = model(request).json()
        stage, ctx, _ = model.calls[-1]
        if stage == "NuwaResponses":
            ids = sorted(citation_ids(ctx["skill_markdown"]))
            assert ids
            document = json.loads(body["choices"][0]["message"]["content"])
            for answer in document["answers"]:
                answer["evidence"] = ids[:2]
            body["choices"][0]["message"]["content"] = dumps(document)
        return httpx.Response(200, json=body)

    s.llm.transport = httpx.MockTransport(provider)
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    target = active_dir(s.db, cid)
    report = read_json(target / "nuwa_report.json")
    assert report["status"] == "passed"
    assert all(answer["evidence"] for answer in report["validation"]["answers"])
    assert sum(stage == "NuwaResponses" for stage, _, _ in model.calls) == 1
    assert (target / "skill/SKILL.md").is_file()
