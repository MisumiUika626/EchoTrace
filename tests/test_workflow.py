import asyncio
import json
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

from conftest import FIXTURE
from echotrace.distill.persona import active_dir
from echotrace.distill.radar import timeline_events
from echotrace.ingest.cleaner import import_files
from echotrace.rag.retriever import search


async def finish(state, job):
    task = state.jobs.tasks[job["id"]]
    await task
    result = state.jobs.get(job["id"])
    assert result["status"] == "succeeded", result
    return result


async def test_full_fictional_workflow_versions_cache_and_evidence(app, model):
    s = app.state
    imported = import_files(s.db, s.config, [FIXTURE], "林间（虚构）", "fiction-them", ["fiction-me"])
    cid = imported["contact_id"]
    assert imported["report"]["total"] == 5000
    payload = {"contact_id": cid, "consent": True, "token_limit": 2000000, "tier": "quick"}
    first = await finish(s, s.jobs.create("distill", payload.copy()))
    root = active_dir(s.db, cid)
    for name in ("persona.json", "radar.json", "quotes.jsonl", "skill/SKILL.md"):
        assert (root / name).is_file()
    quality = json.loads((root / "skill_quality.json").read_text(encoding="utf-8"))
    assert len(quality["checks"]) == 6 and quality["passed"], quality
    metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
    assert {item["period"] for item in metrics["ranges"]} == {"last_365", "last_90", "last_30"}
    assert metrics["timeline"]
    for event in metrics["timeline"]:
        assert event["ts"] == min(s.db.one("SELECT ts FROM messages WHERE evidence_id=?", (mid,))["ts"] for mid in event["evidence"])
    observations = json.loads((root / "observations.json").read_text(encoding="utf-8"))
    invalid = [{**item, "evidence": ["m999999"]} for item in observations]
    assert timeline_events(s.db, cid, invalid) == []
    quotes = search(s.db, s.config, cid, "周末一起去", 8)
    assert quotes and all(q["contact_id"] == cid for q in quotes)
    assert all(len(q["context"]) <= 7 for q in quotes)
    own_ids = {r["evidence_id"] for r in s.db.rows("SELECT evidence_id FROM messages")}
    assert all(set(q["evidence"]) <= own_ids for q in quotes)
    advice = await s.advisor.advise(cid, {"consent": True, "draft": "周末怎么约看展"})
    assert {x["strategy"] for x in advice["options"]} == {"safe", "warm", "honest"}
    assert advice["context_used"]["messages"] == 100
    chat = await s.advisor.chat(cid, {"consent": True, "draft": "周末一起去吗"})
    assert chat["inferred"] and s.db.one("SELECT count(*) AS n FROM conversations")["n"] == 2
    map_calls = sum(stage == "Chunk" for stage, _, _ in model.calls)
    second = await finish(s, s.jobs.create("distill", payload.copy()))
    assert second["result"]["version"] == 2
    assert sum(stage == "Chunk" for stage, _, _ in model.calls) == map_calls
    v1 = Path(s.db.one("SELECT dir FROM personas WHERE contact_id=? AND version=1", (cid,))["dir"])
    s.pipeline.publish(cid, v1, json.loads((v1 / "metadata.json").read_text(encoding="utf-8")))
    assert active_dir(s.db, cid) == v1
    assert search(s.db, s.config, cid, "周末")
    assert first["tokens"] > 0


def test_duplicate_import_and_role_correction_keep_ids(app, imported):
    cid, path = imported
    s = app.state
    before = s.db.rows("SELECT id,sender FROM messages ORDER BY id")
    with TestClient(app) as client:
        response = client.post(f"/api/contacts/{cid}/speakers", json={"mapping": {"fiction-me": "them", "fiction-them": "me"}})
        assert response.status_code == 200
    result = import_files(s.db, s.config, [path], "林间（虚构）", "fiction-them", ["fiction-me"])
    assert result["report"]["import"]["added"] == 0
    after = s.db.rows("SELECT id,sender FROM messages ORDER BY id")
    assert [m["id"] for m in before] == [m["id"] for m in after]
    assert all(a["sender"] != b["sender"] for a, b in zip(before, after))


def test_message_pagination_no_same_timestamp_loss(app, imported):
    cid, _ = imported
    s = app.state
    s.db.execute("UPDATE messages SET ts=1767229200")
    collected, cursor = set(), {}
    with TestClient(app) as client:
        while True:
            data = client.get(f"/api/contacts/{cid}/messages", params={"limit": 37, **cursor}).json()["data"]
            ids = {m["id"] for m in data["messages"]}
            assert not ids & collected
            collected |= ids
            cursor = data["next"]
            if not cursor:
                break
    assert len(collected) == 600


async def test_summary_privacy_excludes_raw_chat_and_old_simulation(app, imported, model):
    cid, _ = imported
    s = app.state
    s.config.update({"privacy": {"summary_only": True}})
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    s.db.execute("INSERT INTO conversations(contact_id,role,content,created_at) VALUES(?,'user','不可外发的旧私密原文',1)", (cid,))
    await s.advisor.chat(cid, {"consent": True})
    sent = json.dumps([context for _, context, _ in model.calls], ensure_ascii=False)
    assert "不可外发的旧私密原文" not in sent
    assert "窗边的橘子" not in sent
    assert "林间" not in sent
    assert "Quotes" not in [stage for stage, _, _ in model.calls]
    assert model.calls[-1][1]["simulation_history"] == []


async def test_pause_resume_checkpoint_and_restart(app, imported, model):
    cid, _ = imported
    s = app.state
    # A budget stop preserves prepared inputs. Resuming raises the budget explicitly.
    job = s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 1})
    await s.jobs.tasks[job["id"]]
    assert s.jobs.get(job["id"])["status"] == "failed"
    resumed = s.jobs.resume(job["id"], 2000000)
    await finish(s, resumed)
    s.db.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    count = len(model.calls)
    await s.jobs.startup()
    assert s.jobs.get(job["id"])["status"] == "paused"
    assert len(model.calls) == count


async def test_import_pause_waits_for_worker_before_resume(app, tmp_path):
    s = app.state
    started, stopped = asyncio.Event(), asyncio.Event()
    async def worker(job_id, payload):
        started.set()
        while True:
            await asyncio.sleep(.01)
            try:
                s.jobs.check(job_id)
            finally:
                if s.jobs.get(job_id)["status"] == "paused":
                    stopped.set()
    s.jobs.handlers["import"] = worker
    job = s.jobs.create("import", {})
    await started.wait()
    await s.jobs.cancel(job["id"], pause=True)
    assert stopped.is_set() and job["id"] not in s.jobs.tasks


async def test_stale_source_checkpoint_refuses_model_calls(app, imported, model):
    cid, _ = imported
    s = app.state
    job = s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 1})
    await s.jobs.tasks[job["id"]]
    assert s.jobs.get(job["id"])["status"] == "failed"
    s.db.execute("UPDATE contacts SET revision=revision+1 WHERE id=?", (cid,))
    calls = len(model.calls)
    resumed = s.jobs.resume(job["id"], 2000000)
    await s.jobs.tasks[resumed["id"]]
    result = s.jobs.get(job["id"])
    assert result["status"] == "failed" and "检查点" in result["error"]
    assert len(model.calls) == calls


async def test_skill_only_new_version_preserves_other_artifacts(app, imported, model, tmp_path, monkeypatch):
    from echotrace.distill.skill_writer import install_skill
    from echotrace.util import AppError
    cid, _ = imported
    s = app.state
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    old = active_dir(s.db, cid)
    before = {name: (old / name).read_bytes() for name in ("persona.json", "radar.json", "quotes.jsonl", "self_style.json")}
    calls = len(model.calls)
    result = await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 500000, "skill_only": True, "base_version": 1}))
    assert result["result"]["version"] == 2
    stages = [stage for stage, _, _ in model.calls[calls:]]
    assert stages.count("NuwaResearch") == 6
    assert "NuwaSynthesis" in stages and "NuwaEvaluation" in stages
    assert stages.count("NuwaRefinement") == 2
    assert "Chunk" not in stages and "Persona" not in stages
    root = active_dir(s.db, cid)
    assert root != old
    assert all((root / name).read_bytes() == value for name, value in before.items())
    assert json.loads((root / "skill_quality.json").read_text(encoding="utf-8"))["passed"]
    with TestClient(app) as client:
        response = client.post(f"/api/contacts/{cid}/skill/rerun", json={"consent": False})
        assert response.json()["error"]["code"] == "CONSENT_REQUIRED"
    old_snapshot = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    calls = len(model.calls)
    s.config.update({"llm": {"base_url": ""}, "privacy": {"summary_only": True}})
    rename = Path.rename
    locked_attempts = []
    def rename_with_transient_lock(path, target):
        if path.name.startswith(".skill-copy-") and len(locked_attempts) < 2:
            locked_attempts.append(path)
            raise PermissionError(13, "Transient directory lock")
        return rename(path, target)
    monkeypatch.setattr(Path, "rename", rename_with_transient_lock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/api/contacts/{cid}/skill/regenerate")
        assert response.status_code == 200
        job = response.json()["data"]
        local = await finish(s, job)
    assert local["result"]["version"] == 3 and len(model.calls) == calls
    assert len(locked_attempts) == 2
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} == old_snapshot
    rebuilt = active_dir(s.db, cid)
    assert json.loads((rebuilt / "metadata.json").read_text(encoding="utf-8"))["tokens"] == 0
    assert all((rebuilt / name).read_bytes() == value for name, value in before.items())
    installed = Path(install_skill(rebuilt / "skill", "fictional-acceptance", "project", tmp_path)["path"])
    assert installed.joinpath("SKILL.md").is_file()
    assert len(list(installed.joinpath("references", "research").glob("*.md"))) == 6
    with pytest.raises(AppError) as error:
        install_skill(rebuilt / "skill", "fictional-acceptance", "project", tmp_path)
    assert error.value.code == "SKILL_EXISTS"


async def test_scoped_distillation_months_keep_the_selected_range(app, imported):
    cid, _ = imported
    s = app.state
    messages = s.db.rows("SELECT evidence_id,ts FROM messages ORDER BY ts,id")
    scope = {"start": messages[100]["ts"], "end": messages[499]["ts"]}
    selected = {m["evidence_id"] for m in messages if scope["start"] <= m["ts"] <= scope["end"]}
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000, "scope": scope}))
    metrics = json.loads((active_dir(s.db, cid) / "metrics.json").read_text(encoding="utf-8"))
    assert sum(month["sample"] for month in metrics["monthly"]) == len(selected)
    for month in metrics["monthly"]:
        assert all(set(anchor["evidence"]) <= selected for anchor in month["anchors"].values())


async def test_append_creates_new_version_and_reuses_unchanged_chunks(app, imported, model, tmp_path):
    cid, _ = imported
    s = app.state
    payload = {"contact_id": cid, "consent": True, "token_limit": 2000000, "tier": "deep"}
    await finish(s, s.jobs.create("distill", payload.copy()))
    first = active_dir(s.db, cid)
    previous = (first / "metrics.json").read_bytes()
    end = s.db.one("SELECT MAX(ts) AS ts FROM messages")["ts"]
    rows = [{"msgId": f"fiction-append-{i}", "timestamp": end + 86400 + i * 60,
             "sender": {"uid": "fiction-me" if i % 2 else "fiction-them", "name": "追加虚构"},
             "content": "周末一起去散步吧，下午三点见" if i % 2 else "好的，我喜欢这个安排"} for i in range(300)]
    path = tmp_path / "appended.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    result = import_files(s.db, s.config, [path], "林间（虚构）", "fiction-them", ["fiction-me"])
    assert result["contact_id"] == cid and result["report"]["import"]["added"] == 300
    calls = sum(stage == "Chunk" for stage, _, _ in model.calls)
    second = await finish(s, s.jobs.create("distill", payload.copy()))
    root = active_dir(s.db, cid)
    count = len(json.loads((root / "manifest.json").read_text(encoding="utf-8"))["chunks"])
    new_calls = sum(stage == "Chunk" for stage, _, _ in model.calls) - calls
    assert 0 < new_calls < count
    assert second["result"]["version"] == 2 and first != root
    assert (first / "metrics.json").read_bytes() == previous
    assert json.loads((root / "metadata.json").read_text(encoding="utf-8"))["source_count"] == 900
    assert json.loads((root / "radar.json").read_text(encoding="utf-8"))["last_ts"] > end


async def test_cross_person_evidence_and_manual_quote_survives_rollback(app, imported, tmp_path):
    cid, _ = imported
    s = app.state
    await finish(s, s.jobs.create("distill", {"contact_id": cid, "consent": True, "token_limit": 2000000}))
    other = tmp_path / "other.jsonl"
    rows = [
        {"msgId": "other-1", "timestamp": 1767229200,
         "sender": {"uid": "other", "name": "另一个虚构人"}, "content": "只能属于另一个人的测试语料"},
        {"msgId": "other-2", "timestamp": 1767229260,
         "sender": {"uid": "me", "name": "我"}, "content": "只用于确认双人身份的虚构回复"},
    ]
    other.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    imported_other = import_files(s.db, s.config, [other], "另一个虚构人", "other", ["me"])
    other_cid = imported_other["contact_id"]
    mid = s.db.one("SELECT m.id FROM messages m JOIN sessions s ON m.session_id=s.id WHERE s.contact_id=?", (other_cid,))["id"]
    own_mid = s.db.one("SELECT m.id FROM messages m JOIN sessions s ON m.session_id=s.id WHERE s.contact_id=? ORDER BY m.id LIMIT 1", (cid,))["id"]
    with TestClient(app) as client:
        assert client.get(f"/api/contacts/{cid}/messages/{mid}").json()["error"]["code"] == "INVALID_EVIDENCE"
        assert client.post(f"/api/contacts/{cid}/quotes", json={"message_id": mid}).json()["error"]["code"] == "INVALID_EVIDENCE"
        pick = client.post(f"/api/contacts/{cid}/quotes", json={"message_id": own_mid}).json()["data"]
        assert pick["source"] == "user_pick"
        assert client.post(f"/api/contacts/{other_cid}/distill", json={"consent": True}).json()["error"]["code"] == "NOT_ENOUGH_DATA"
        assert client.post(f"/api/contacts/{cid}/versions/rollback", json={"version": 1}).status_code == 200
    assert s.db.one("SELECT source FROM quotes WHERE id=?", (pick["id"],))["source"] == "user_pick"
    assert all(q["contact_id"] == cid for q in search(s.db, s.config, cid, "测试语料"))


def test_radar_scale_setting_changes_local_anchor(app, imported):
    from echotrace.stats import calculate
    cid, _ = imported
    s = app.state
    before = calculate(s.db, cid)["anchors"]["affinity"]
    scales = s.config.get()["radar"]["scales"]
    s.config.update({"radar": {"scales": {**scales, "affinity": .1}}})
    after = calculate(s.db, cid)["anchors"]["affinity"]
    assert after["scale"] == .1 and before["score"] != after["score"]
