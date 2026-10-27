import hashlib
import json
import shutil
from pathlib import Path

import httpx

from conftest import claim
from echotrace.distill.canonical_skill import REQUIRED_CANONICAL_FILES, validate_canonical_skill
from echotrace.distill.nuwa import DIMENSIONS, select_framework
from echotrace.distill.persona import active_dir
from echotrace.distill.skill_writer import citation_ids
from echotrace.llm.nuwa_skill import DOCUMENTS, load_nuwa_skill
from echotrace.util import AppError, read_json, write_json
from test_workflow import finish


def candidate(**changes):
    return {**claim([1, 2, 3]), "name": "可核查候选", "application": "相似场景", "limitation": "无证据时不适用",
            "scenarios": [{"domain": "工作", "evidence": [1]}, {"domain": "生活", "evidence": [2]}],
            "generative": True, "generative_reason": "预测新问题时优先核查条件", "distinctive": True,
            "distinctive_reason": "有明确的个人优先次序", **changes}


def test_three_tests_downgrade_repeated_scene_and_discard_unfounded_candidate():
    good = candidate()
    repeated = candidate(name="同例换名字", scenarios=[{"domain": "工作", "evidence": [1]}, {"domain": "生活", "evidence": [1]}])
    empty = candidate(name="无依据", scenarios=[], generative=False, distinctive=False)
    synthesis = {"candidates": [good, repeated, empty], "heuristics": [], "expression": [], "values": [], "tensions": [], "lineage": [], "unknowns": []}
    selected = select_framework(synthesis)
    assert [s["action"] for s in selected["selection"]] == ["model", "heuristic", "discard"]
    assert len(selected["models"]) == 1 and len(selected["heuristics"]) == 1
    anonymous = select_framework(synthesis, summary_only=True)
    assert anonymous["models"] == anonymous["heuristics"] == []


def test_distributed_app_uses_bundled_nuwa_without_local_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    skill = load_nuwa_skill()
    assert skill["source"] == "bundled"
    assert "name: huashu-nuwa" in skill["documents"]["SKILL.md"]
    assert "三重验证" in skill["documents"]["references/extraction-framework.md"]
    assert "角色扮演规则" in skill["documents"]["references/skill-template.md"]


async def test_full_nuwa_skill_resolves_every_markdown_citation_from_portable_evidence(app, imported, model):
    cid, _ = imported
    state = app.state
    first_target = state.db.one(
        "SELECT id FROM messages WHERE sender='them' AND session_id IN "
        "(SELECT id FROM sessions WHERE contact_id=?) ORDER BY sequence LIMIT 1",
        (cid,),
    )
    state.db.execute(
        "UPDATE messages SET text=text || ? WHERE id=?",
        ("甲" * 2_000, first_target["id"]),
    )
    def bounded_provider(request):
        response = model(request)
        body = response.json()
        stage = model.calls[-1][0]
        document = json.loads(body["choices"][0]["message"]["content"])
        if stage == "FullMap":
            for finding in document["findings"]:
                finding["value"] = finding["value"][:600]
                finding["quote"] = finding["quote"][:800]
        elif stage == "Quotes":
            for quote in document["quotes"]:
                quote["text"] = quote["text"][:800]
        body["choices"][0]["message"]["content"] = json.dumps(document, ensure_ascii=False)
        return httpx.Response(200, json=body)
    state.llm.transport = httpx.MockTransport(bounded_provider)
    await finish(state, state.jobs.create("distill", {
        "contact_id": cid,
        "consent": True,
        "token_limit": 2_000_000,
        "mode": "full",
        "send_raw": True,
    }))
    root = active_dir(state.db, cid)
    citations = set()
    for relative in REQUIRED_CANONICAL_FILES:
        citations.update(citation_ids((root / relative).read_text(encoding="utf-8")))
    packet = read_json(root / "skill/references/evidence.json")
    messages = {message["id"]: message for message in packet["evidence_messages"]}

    assert citations
    assert citations <= messages.keys()
    for evidence_id in citations:
        message = messages[evidence_id]
        assert {"id", "sequence", "sender", "ts", "text"} <= message.keys()
        assert "fragment_offset" not in message and "source_characters" not in message


async def test_copied_canonical_skill_tree_needs_no_private_database(app, imported, model, tmp_path):
    cid, _ = imported
    state = app.state
    await finish(state, state.jobs.create("distill", {
        "contact_id": cid,
        "consent": True,
        "token_limit": 2_000_000,
        "mode": "full",
        "send_raw": True,
    }))
    source = active_dir(state.db, cid)
    copied = tmp_path / "portable"
    shutil.copytree(source / "skill", copied / "skill")
    packet = read_json(copied / "skill/references/evidence.json")
    quality = read_json(copied / "skill/references/skill-quality.json")
    report = read_json(copied / "skill/references/nuwa-report.json")

    assert quality is not None and report is not None
    result = validate_canonical_skill(
        copied,
        packet["evidence_messages"],
        quality,
        report,
    )
    assert result["gates"]["canonical_files"] is True
    assert result["gates"]["evidence_valid"] is True
    assert result["gates"]["nuwa_quality"] is True
    assert report["provenance"]["prompts"]
    assert report["validation"]["skill_sha256"] == hashlib.sha256(
        (copied / "skill/SKILL.md").read_bytes()
    ).hexdigest()


async def test_research_artifacts_and_blind_independent_validation(app, imported, model):
    cid, _ = imported
    s = app.state
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    root = active_dir(s.db, cid)
    report = read_json(root / "nuwa_report.json")
    assert report["status"] == "passed" and report["repair_rounds"] == 0
    calls = [(stage, context) for stage, context, _ in model.calls]
    research = [ctx for stage, ctx in calls if stage == "NuwaResearch"]
    assert {ctx["file"] for ctx in research} == {d[0] for d in DIMENSIONS}
    assert len(research) == 6
    for ctx in research:
        targets = {m["id"] for m in ctx["evidence_messages"] if m["sender"] == "them"}
        assert targets and any(set(ob["evidence"]) <= targets for ob in ctx["observations"])
    for stage, ctx in calls:
        if stage == "NuwaResponses":
            assert set(ctx) == {"skill_markdown", "questions"}
            assert all(set(q) == {"id", "question"} for q in ctx["questions"])
        if stage == "NuwaEvaluation":
            assert ctx["evidence_messages"] and len(ctx["plan"]["known"]) == 3
    for stage, _, body in model.calls:
        if stage.startswith("Nuwa") and stage != "NuwaResponses":
            instruction = body["messages"][0]
            assert instruction["role"] == "system"
            assert all(text in instruction["content"] for text in load_nuwa_skill()["documents"].values())
            assert all(name in instruction["content"] for name in DOCUMENTS)
        elif stage == "NuwaResponses":
            assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
    assert report["provenance"]["sources"]["delivery"] == "system_message"
    assert set(report["checkpoints"]) == {"research", "synthesis", "validation"}
    assert {r["perspective"] for r in report["refinement"]["reviews"]} == {"optimizer", "creator"}
    assert report["validation"]["skill_sha256"] == hashlib.sha256((root / "skill/SKILL.md").read_bytes()).hexdigest()
    for filename, _ in DIMENSIONS:
        text = (root / "skill/references/research" / filename).read_text(encoding="utf-8")
        assert "信息缺口" in text and "evidence:" in text
    assert (root / "skill/references/evidence.json").is_file()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        result = (await client.post(f"/api/contacts/{cid}/skill/quality-check")).json()["data"]
        assert result["semantic_passed"]
        with (root / "skill/SKILL.md").open("a", encoding="utf-8") as file:
            file.write("\n手动改动后的版本\n")
        result = (await client.post(f"/api/contacts/{cid}/skill/quality-check")).json()["data"]
        assert result["nuwa_status"] == "stale" and not result["semantic_passed"]


async def test_optional_human_checkpoints_resume_without_repeating_completed_roles(app, imported, model):
    cid, _ = imported
    s = app.state
    job = s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000, "nuwa_review": True})
    await s.jobs.tasks[job["id"]]
    for checkpoint in ("research", "synthesis", "validation"):
        paused = s.jobs.get(job["id"])
        assert paused["status"] == "paused" and paused["payload"]["nuwa_pending"] == checkpoint
        assert checkpoint in paused["result"]["nuwa"]["checkpoints"]
        assert not s.db.one("SELECT version FROM personas WHERE contact_id=?", (cid,))
        before = len(model.calls)
        await s.jobs.startup()
        assert len(model.calls) == before and s.jobs.get(job["id"])["status"] == "paused"
        resumed = s.jobs.resume(job["id"])
        await s.jobs.tasks[resumed["id"]]
    assert s.jobs.get(job["id"])["status"] == "succeeded"
    assert sum(stage == "NuwaResearch" for stage, _, _ in model.calls) == 6
    assert sum(stage == "NuwaSynthesis" for stage, _, _ in model.calls) == 1
    assert sum(stage == "NuwaRefinement" for stage, _, _ in model.calls) == 2


async def test_local_rebuild_cannot_inherit_validation_for_changed_character(app, imported, model):
    cid, _ = imported
    s = app.state
    await finish(s, s.jobs.create("distill", {
        "contact_id": cid,
        "consent": True,
        "token_limit": 2000000,
        "mode": "full",
        "send_raw": True,
    }))
    source = active_dir(s.db, cid)
    original_report = (source / "nuwa_report.json").read_bytes()
    skill = read_json(source / "skill_model.json")
    skill["heuristics"][0]["value"] = "本地编辑的观点尚未经过人物测试。"
    write_json(source / "skill_model.json", skill)
    calls = len(model.calls)
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "skill_only": True, "local_only": True, "base_version": 1}))
    rebuilt = active_dir(s.db, cid)
    assert len(model.calls) == calls
    assert "本地编辑的观点" in (rebuilt / "skill/SKILL.md").read_text(encoding="utf-8")
    assert read_json(rebuilt / "skill_quality.json")["semantic_passed"] is False
    assert read_json(rebuilt / "nuwa_report.json")["status"] == "stale"
    assert read_json(rebuilt / "skill/references/nuwa-report.json")["status"] == "stale"
    assert read_json(rebuilt / "skill/references/skill-quality.json")["semantic_passed"] is False
    assert read_json(rebuilt / "metadata.json")["complete"] is False
    assert read_json(rebuilt / "metadata.json")["complete_eligible"] is False
    assert read_json(rebuilt / "distill_manifest.json")["complete"] is False
    assert read_json(rebuilt / "distill_manifest.json")["gates"]["skill_only_revalidated"] is False
    assert read_json(rebuilt / "distill_manifest.json")["nuwa"]["status"] == "stale"
    assert (source / "nuwa_report.json").read_bytes() == original_report


async def test_research_interruption_reuses_each_saved_dimension(app, imported, model, monkeypatch):
    cid, _ = imported
    s = app.state
    original = s.llm.call
    async def stop(stage, context, *args, **kwargs):
        if stage == "nuwa_research" and context["file"].startswith("03-"):
            raise AppError("TOKEN_BUDGET_EXCEEDED", "模拟阶段预算耗尽")
        return await original(stage, context, *args, **kwargs)
    monkeypatch.setattr(s.llm, "call", stop)
    job = s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000})
    await s.jobs.tasks[job["id"]]
    assert s.jobs.get(job["id"])["status"] == "failed"
    root = next((s.db.root / "people").glob("*/versions/v1"))
    saved = {p.name: p.read_bytes() for p in (root / "nuwa").glob("0*.json")}
    assert saved
    seen = len(model.calls)
    monkeypatch.setattr(s.llm, "call", original)
    await finish(s, s.jobs.resume(job["id"]))
    assert all((root / "nuwa" / name).read_bytes() == data for name, data in saved.items())
    new_files = {ctx["file"].replace(".md", ".json") for stage, ctx, _ in model.calls[seen:] if stage == "NuwaResearch"}
    assert not new_files & saved.keys()
    assert not any(stage in ("Chunk", "Persona", "Radar") for stage, _, _ in model.calls[seen:])


async def test_semantic_failures_stop_after_two_repairs_and_format_recheck_preserves_them(app, imported, model):
    cid, _ = imported
    s = app.state
    def failing(request):
        response = model(request)
        body = response.json()
        output = json.loads(body["choices"][0]["message"]["content"])
        if "checks" in output:
            output["checks"][0].update(passed=False, reason="回答与实际立场相反，必须保留失败")
            output["improvements"] = ["重审证据而非改写真实立场"]
        body["choices"][0]["message"]["content"] = json.dumps(output, ensure_ascii=False)
        return httpx.Response(200, json=body)
    s.llm.transport = httpx.MockTransport(failing)
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    root = active_dir(s.db, cid)
    report = read_json(root / "nuwa_report.json")
    assert report["repair_rounds"] == 2 and report["status"] == "degraded"
    assert sum(stage == "NuwaSynthesis" for stage, _, _ in model.calls) == 3
    assert read_json(root / "skill_quality.json")["passed"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        quality = (await client.post(f"/api/contacts/{cid}/skill/quality-check")).json()["data"]
        assert quality["passed"] and not quality["semantic_passed"]
        assert any(not c["passed"] for c in quality["validation"])


async def test_summary_mode_delivers_honest_degraded_framework(app, imported, model):
    cid, _ = imported
    s = app.state
    s.config.update({"privacy": {"summary_only": True}})
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    root = active_dir(s.db, cid)
    report = read_json(root / "nuwa_report.json")
    assert report["status"] == "degraded" and report["models"] == report["heuristics"] == 0
    assert report["validation"]["plan"]["known"] == [] and report["repair_rounds"] == 0
    sent = json.dumps([context for _, context, _ in model.calls], ensure_ascii=False)
    assert "林间" not in sent and "窗边的橘子" not in sent


async def test_refinement_is_integrated_retested_and_reverted_on_regression(app, imported, model):
    cid, _ = imported
    s = app.state
    def refine(request):
        response = model(request)
        body = response.json()
        output = json.loads(body["choices"][0]["message"]["content"])
        stage, context, _ = model.calls[-1]
        if stage == "NuwaRefinement":
            output["suggestions"] = [{"section": "protocol", "text": "先复核日期再解释变化。", "reason": "避免混淆时序"}]
        if stage == "NuwaEvaluation" and "先复核日期再解释变化" in next(c for st, c, _ in reversed(model.calls) if st == "NuwaResponses")["skill_markdown"]:
            output["checks"][0].update(passed=False, reason="模拟精炼后回归")
        body["choices"][0]["message"]["content"] = json.dumps(output, ensure_ascii=False)
        return httpx.Response(200, json=body)
    s.llm.transport = httpx.MockTransport(refine)
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    root = active_dir(s.db, cid)
    report = read_json(root / "nuwa_report.json")
    assert sum(stage == "NuwaIntegration" for stage, _, _ in model.calls) == 1
    assert sum(stage == "NuwaEvaluation" for stage, _, _ in model.calls) == 2
    assert report["refinement"]["rejected"] and not report["refinement"]["applied"]
    assert "先复核日期再解释变化" not in (root / "skill/SKILL.md").read_text(encoding="utf-8")
