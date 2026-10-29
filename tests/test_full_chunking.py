from dataclasses import asdict

import pytest

from echotrace.distill.pipeline import (
    Pipeline,
    assert_distill_start_allowed,
    resolve_distill_mode,
    validate_full_coverage,
)
from echotrace.distill.planner import plan_full_chunks
from echotrace.ingest.canonical import canonical_messages
from echotrace.stats import calculate
from echotrace.util import AppError, dumps, read_json, write_json


def _settings(context_window=16_384, **changes):
    result = {
        "model": "private-model", "base_url": "http://127.0.0.1:9000/v1",
        "context_window": context_window, "context_window_confirmed": True,
        "max_tokens": 4096, "adapter": "none", "thinking": "low",
        "thinking_field": "reasoning_effort", "thinking_values": {"low": "low"},
        "temperature": .2, "summary_only": False,
    }
    result.update(changes)
    return result


def test_full_plan_is_deterministic_and_whole_has_no_overlap(app, imported):
    contact_id, _ = imported
    rows = canonical_messages(app.state.db, contact_id)["messages"]

    first = plan_full_chunks(app.state.llm, rows, _settings(1_000_000), "B")
    second = plan_full_chunks(app.state.llm, rows, _settings(1_000_000), "B")

    assert asdict(first) == asdict(second)
    assert first.mode == "Whole"
    assert len(first.chunks) == 1
    assert first.chunks[0].new_indexes == tuple(range(len(first.chunks[0].views)))
    assert set(first.chunks[0].budgets) == {"full_map", "extract_quotes"}


def test_full_plan_obeys_both_stages_and_bounds_overlap(app, imported):
    contact_id, _ = imported
    rows = canonical_messages(app.state.db, contact_id)["messages"]
    plan = plan_full_chunks(app.state.llm, rows, _settings(), "B")

    assert plan.mode == "Chunked"
    covered = []
    for chunk in plan.chunks:
        assert chunk.new_indexes
        overlap = [view for index, view in enumerate(chunk.views) if index not in chunk.new_indexes]
        assert len(overlap) <= 3
        assert all("fragment_index" not in view for view in overlap)
        for budget in chunk.budgets.values():
            assert budget.remaining_input_tokens >= 0
        covered.extend((view["id"], view.get("fragment_index"))
                       for index, view in enumerate(chunk.views) if index in chunk.new_indexes)
    assert len(covered) == len(set(covered))
    assert {item[0] for item in covered} == {row["evidence_id"] for row in rows}


def test_full_plan_fragments_long_message_with_exact_offsets(app, imported):
    contact_id, _ = imported
    long_text = "甲乙ABC\n" * 12_000
    rows = canonical_messages(app.state.db, contact_id)["messages"][:1]
    rows[0] = {**rows[0], "text": long_text}

    plan = plan_full_chunks(app.state.llm, rows, _settings(), "B")
    fragments = [view for chunk in plan.chunks for index, view in enumerate(chunk.views)
                 if index in chunk.new_indexes]

    assert len(fragments) > 1
    assert "m000001" in plan.fragmented
    assert "".join(view["text"] for view in fragments) == long_text
    assert [view["fragment_index"] for view in fragments] == list(range(len(fragments)))
    assert [view["fragment_offset"] for view in fragments] == [
        sum(len(previous["text"]) for previous in fragments[:index])
        for index in range(len(fragments))
    ]
    assert all(view["fragment_count"] == len(fragments) for view in fragments)
    assert all(len(chunk.views) == len(chunk.new_indexes) for chunk in plan.chunks)


def test_single_message_fit_uses_same_first_coverage_envelope_as_final_chunk(app, imported):
    contact_id, _ = imported
    row = canonical_messages(app.state.db, contact_id)["messages"][0]
    text = "甲" * 11_388
    row = {**row, "text": text}

    plan = plan_full_chunks(app.state.llm, [row], _settings(), "B")
    fragments = [view for chunk in plan.chunks for index, view in enumerate(chunk.views)
                 if index in chunk.new_indexes]

    assert fragments
    assert "".join(view["text"] for view in fragments) == text


def test_chunk_planner_preserves_full_map_output_target(app, imported):
    contact_id, _ = imported
    rows = canonical_messages(app.state.db, contact_id)["messages"]

    plan = plan_full_chunks(app.state.llm, rows, _settings(32_768), "B")

    assert plan.mode == "Chunked"
    assert all(chunk.budgets["full_map"].output_reserve == 4096 for chunk in plan.chunks)


def test_prepare_full_persists_body_free_descriptors_and_validates(app, imported, tmp_path):
    contact_id, _ = imported
    target = tmp_path / "v1"
    stats = calculate(app.state.db, contact_id)
    pipeline = Pipeline(app.state.db, app.state.config, app.state.jobs, app.state.llm)

    manifest = pipeline.prepare_full(contact_id, target, {"scope": {}}, stats,
                                     _settings(), "B")
    canonical = validate_full_coverage(app.state.db, contact_id, target, manifest)

    assert len(canonical) == 600
    assert manifest["sampled"] == manifest["total"] == 600
    assert all("text" not in descriptor for descriptor in manifest["chunk_descriptors"])
    assert all(set(descriptor["stage_budgets"]) == {"full_map", "extract_quotes"}
               for descriptor in manifest["chunk_descriptors"])


async def test_full_pipeline_uses_phase4_and_persists_complete_phase4_manifest(app, imported, model, monkeypatch):
    import echotrace.distill.pipeline as pipeline_module
    from echotrace.distill.persona import active_dir
    from test_large_distill import extraction_job

    async def direct_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(pipeline_module.asyncio, "to_thread", direct_thread)

    contact_id, _ = imported
    job_id = extraction_job(app.state)
    await app.state.pipeline._run(job_id, {
        "contact_id": contact_id, "consent": True, "token_limit": 2_000_000,
        "mode": "full", "send_raw": True,
    })
    target = active_dir(app.state.db, contact_id)
    manifest = read_json(target / "manifest.json")
    stages = [stage for stage, _, _ in model.calls]

    assert "FullMap" in stages and "FullReduce" in stages
    assert "Chunk" not in stages
    assert manifest["map_total"] == manifest["map_succeeded"] == len(manifest["chunks"])
    assert manifest["failed_chunks"] == []
    assert manifest["reduce_layers"] in (0, 1)
    assert manifest["evidence_validated"] is True
    assert manifest["phase4_complete"] is True


async def test_valid_full_pipeline_writes_complete_formal_manifest(app, imported, model, monkeypatch):
    import echotrace.distill.pipeline as pipeline_module
    from echotrace.distill.persona import active_dir
    from test_large_distill import extraction_job

    async def direct_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(pipeline_module.asyncio, "to_thread", direct_thread)
    contact_id, _ = imported
    job_id = extraction_job(app.state)
    await app.state.pipeline._run(job_id, {
        "contact_id": contact_id,
        "consent": True,
        "token_limit": 2_000_000,
        "mode": "full",
        "send_raw": True,
    })
    target = active_dir(app.state.db, contact_id)
    metadata = read_json(target / "metadata.json")
    formal = read_json(target / "distill_manifest.json")

    assert metadata["complete"] is True
    assert metadata["complete_eligible"] is True
    assert formal["complete"] is True
    assert all(formal["gates"].values())
    assert formal["skill_path"] == "skill/SKILL.md"
    assert read_json(target.parent.parent / "distill_manifest.json")["complete"] is True


async def test_source_change_before_publish_records_incomplete_and_stops_formal_publish(app, imported, model, monkeypatch):
    import echotrace.distill.pipeline as pipeline_module
    from echotrace.distill.persona import person
    from test_large_distill import extraction_job

    async def direct_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(pipeline_module.asyncio, "to_thread", direct_thread)
    original_validate = pipeline_module.validate_full_coverage
    validation_calls = 0

    def mutate_on_final(db, contact_id, target, manifest):
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls == 3:
            db.execute(
                "UPDATE messages SET text=text || '变' WHERE sequence=0 AND session_id IN "
                "(SELECT id FROM sessions WHERE contact_id=?)",
                (contact_id,),
            )
        return original_validate(db, contact_id, target, manifest)

    monkeypatch.setattr(pipeline_module, "validate_full_coverage", mutate_on_final)
    contact_id, _ = imported
    job_id = extraction_job(app.state)
    with pytest.raises(AppError) as error:
        await app.state.pipeline._run(job_id, {
            "contact_id": contact_id,
            "consent": True,
            "token_limit": 2_000_000,
            "mode": "full",
            "send_raw": True,
        })
    target = (app.state.db.root / "people" / person(app.state.db, contact_id)["slug"]
              / "versions/v1")
    formal = read_json(target / "distill_manifest.json")
    metadata = read_json(target / "metadata.json")

    assert error.value.code == "STALE_DISTILLATION"
    assert formal["complete"] is False
    assert formal["gates"]["source_current"] is False
    assert metadata["complete"] is False
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM personas WHERE contact_id=?", (contact_id,)
    )["n"] == 0


async def test_degraded_nuwa_records_incomplete_and_stops_formal_publish(app, imported, model, monkeypatch):
    import json
    import httpx
    import echotrace.distill.pipeline as pipeline_module
    from echotrace.distill.persona import person
    from test_large_distill import extraction_job

    async def direct_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(pipeline_module.asyncio, "to_thread", direct_thread)

    def degraded_provider(request):
        response = model(request)
        body = response.json()
        if model.calls[-1][0] == "NuwaEvaluation":
            document = json.loads(body["choices"][0]["message"]["content"])
            document["checks"][0].update(passed=False, reason="模拟语义检查失败")
            document["improvements"] = ["需要人工复核 Evidence 语义"]
            body["choices"][0]["message"]["content"] = json.dumps(document, ensure_ascii=False)
        return httpx.Response(200, json=body)

    app.state.llm.transport = httpx.MockTransport(degraded_provider)
    contact_id, _ = imported
    job_id = extraction_job(app.state)
    with pytest.raises(AppError) as error:
        await app.state.pipeline._run(job_id, {
            "contact_id": contact_id,
            "consent": True,
            "token_limit": 2_000_000,
            "mode": "full",
            "send_raw": True,
        })
    target = (app.state.db.root / "people" / person(app.state.db, contact_id)["slug"]
              / "versions/v1")
    formal = read_json(target / "distill_manifest.json")

    assert error.value.code == "NUWA_QUALITY_INCOMPLETE"
    assert formal["complete"] is False
    assert formal["gates"]["nuwa_quality"] is False
    assert read_json(target / "metadata.json")["complete"] is False
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM personas WHERE contact_id=?", (contact_id,)
    )["n"] == 0


async def test_sampled_writes_incomplete_formal_manifest_and_cannot_replace_complete(app, imported, model, monkeypatch):
    import echotrace.distill.pipeline as pipeline_module
    from echotrace.distill.persona import active_dir
    from test_large_distill import extraction_job

    async def direct_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(pipeline_module.asyncio, "to_thread", direct_thread)
    contact_id, _ = imported
    first_job = extraction_job(app.state)
    await app.state.pipeline._run(first_job, {
        "contact_id": contact_id,
        "consent": True,
        "token_limit": 2_000_000,
        "mode": "sampled",
        "tier": "quick",
        "send_raw": True,
    })
    target = active_dir(app.state.db, contact_id)
    formal = read_json(target / "distill_manifest.json")

    assert formal["mode"] == "sampled"
    assert formal["complete"] is False
    assert formal["gates"]["mode_full"] is False
    assert read_json(target / "metadata.json")["complete"] is False

    metadata = read_json(target / "metadata.json")
    metadata["complete"] = True
    write_json(target / "metadata.json", metadata)
    second_job = "extract-sampled-2"
    app.state.db.execute(
        "INSERT INTO jobs(id,kind,status,payload) VALUES(?, 'distill', 'running', ?)",
        (second_job, dumps({"token_limit": 2_000_000})),
    )
    with pytest.raises(AppError) as error:
        await app.state.pipeline._run(second_job, {
            "contact_id": contact_id,
            "consent": True,
            "token_limit": 2_000_000,
            "mode": "sampled",
            "tier": "quick",
            "send_raw": True,
        })
    assert error.value.code == "SAMPLED_CANNOT_REPLACE_COMPLETE"
    assert active_dir(app.state.db, contact_id) == target


async def test_failed_full_map_records_incomplete_manifest_and_stops_publish(app, imported, model, monkeypatch):
    import httpx
    import echotrace.llm.client as client_module
    import echotrace.distill.pipeline as pipeline_module
    from echotrace.distill.persona import person
    from test_large_distill import extraction_job

    async def no_wait(_):
        pass

    async def direct_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(client_module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(pipeline_module.asyncio, "to_thread", direct_thread)
    original = model

    def invalid_map(request):
        response = original(request)
        if original.calls[-1][0] == "FullMap":
            body = response.json()
            body["choices"][0]["message"]["content"] = '{"findings":[{"bad":true}],"open_questions":[]}'
            return httpx.Response(200, json=body)
        return response

    app.state.llm.transport = httpx.MockTransport(invalid_map)
    contact_id, _ = imported
    job_id = extraction_job(app.state)
    payload = {
        "contact_id": contact_id, "consent": True, "token_limit": 2_000_000,
        "mode": "full", "send_raw": True,
    }
    with pytest.raises(AppError) as error:
        await app.state.pipeline._run(job_id, payload)
    target = app.state.db.root / "people" / person(app.state.db, contact_id)["slug"] / "versions" / "v1"
    manifest = read_json(target / "manifest.json")

    assert error.value.code == "PHASE4_INCOMPLETE"
    assert manifest["map_succeeded"] < manifest["map_total"]
    assert manifest["failed_chunks"]
    assert manifest["evidence_validated"] is False
    assert manifest["phase4_complete"] is False
    assert app.state.db.one("SELECT COUNT(*) AS n FROM personas WHERE contact_id=?", (contact_id,))["n"] == 0


async def test_sampled_pipeline_keeps_legacy_map_route(app, imported, model, monkeypatch):
    import echotrace.distill.pipeline as pipeline_module
    from test_large_distill import extraction_job

    async def direct_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(pipeline_module.asyncio, "to_thread", direct_thread)
    contact_id, _ = imported
    job_id = extraction_job(app.state)
    await app.state.pipeline._run(job_id, {
        "contact_id": contact_id, "consent": True, "token_limit": 2_000_000,
        "mode": "sampled", "send_raw": True,
    })

    stages = [stage for stage, _, _ in model.calls]
    assert "Chunk" in stages
    assert "FullMap" not in stages and "FullReduce" not in stages


def test_validate_full_coverage_rejects_mutated_fragment(app, imported, tmp_path):
    contact_id, _ = imported
    long_text = "长消息XYZ\n" * 15_000
    app.state.db.execute(
        "UPDATE messages SET text=? WHERE sequence=0 AND session_id IN "
        "(SELECT id FROM sessions WHERE contact_id=?)", (long_text, contact_id))
    app.state.db.execute(
        "DELETE FROM messages WHERE sequence<>0 AND session_id IN "
        "(SELECT id FROM sessions WHERE contact_id=?)", (contact_id,))
    app.state.db.execute("UPDATE sessions SET msg_count=1 WHERE contact_id=?", (contact_id,))
    target = tmp_path / "v1"
    pipeline = Pipeline(app.state.db, app.state.config, app.state.jobs, app.state.llm)
    manifest = pipeline.prepare_full(contact_id, target, {"scope": {}},
                                     {"total": 1}, _settings(), "B")
    path = target / "inputs" / manifest["chunks"][0]
    views = read_json(path)
    position = manifest["chunk_descriptors"][0]["new_indexes"][0]
    views[position]["text"] += "篡改"
    write_json(path, views)

    try:
        validate_full_coverage(app.state.db, contact_id, target, manifest)
    except AppError as error:
        assert error.code == "INVALID_FULL_COVERAGE"
    else:
        raise AssertionError("mutated full input must be rejected")


def test_new_mode_is_full_but_missing_legacy_payload_stays_sampled():
    assert resolve_distill_mode({"mode": "full"}) == "full"
    assert resolve_distill_mode({"mode": "sampled", "tier": "quick"}) == "sampled"
    assert resolve_distill_mode({"tier": "deep"}) == "sampled"


def test_full_raw_gate_and_sampled_complete_guard_precede_writes(app, imported, tmp_path):
    contact_id, _ = imported
    before = app.state.db.one("SELECT COUNT(*) AS n FROM jobs")["n"]
    try:
        assert_distill_start_allowed(app.state.db, app.state.config, contact_id,
                                     {"mode": "full", "send_raw": False})
    except AppError as error:
        assert error.code == "FULL_REQUIRES_RAW_TEXT"
    else:
        raise AssertionError("full without raw text must be rejected")
    target = tmp_path / "complete"
    write_json(target / "metadata.json", {"complete": True})
    app.state.db.execute(
        "INSERT INTO personas(contact_id,version,created_at,model,thinking,token_cost,dir,summary,active) "
        "VALUES(?,?,?,?,?,?,?,?,1)",
        (contact_id, 99, 1, "mock", "low", 0, str(target), "future complete"),
    )
    try:
        assert_distill_start_allowed(app.state.db, app.state.config, contact_id,
                                     {"mode": "sampled", "send_raw": True})
    except AppError as error:
        assert error.code == "SAMPLED_CANNOT_REPLACE_COMPLETE"
    else:
        raise AssertionError("sampled must not replace complete")
    assert app.state.db.one("SELECT COUNT(*) AS n FROM jobs")["n"] == before


async def test_versions_view_exposes_full_and_legacy_sampled_labels(app, imported, tmp_path):
    from types import SimpleNamespace

    from echotrace.api.routes import versions

    contact_id, _ = imported
    full = tmp_path / "full"
    legacy = tmp_path / "legacy"
    write_json(full / "metadata.json", {"distill_mode": "full", "complete": False})
    write_json(full / "manifest.json", {"plan_mode": "Chunked"})
    write_json(full / "parameters.json", {"tier": "standard"})
    write_json(legacy / "metadata.json", {"sampled": 50})
    write_json(legacy / "parameters.json", {"tier": "quick"})
    for version, path, active in ((1, legacy, 0), (2, full, 1)):
        app.state.db.execute(
            "INSERT INTO personas(contact_id,version,created_at,model,thinking,token_cost,dir,summary,active) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (contact_id, version, version, "mock", "low", 0, str(path), "fixture", active),
        )

    response = await versions(SimpleNamespace(app=app), contact_id)
    current, old = response["data"]

    assert (current["distill_mode"], current["plan_mode"], current["complete"]) == (
        "full", "Chunked", False)
    assert (old["distill_mode"], old["tier"], old["complete"]) == (
        "sampled", "quick", False)


async def test_dry_run_uses_same_private_display_name_as_production(app, imported, monkeypatch):
    from types import SimpleNamespace

    from echotrace.api import routes

    contact_id, _ = imported
    expected = app.state.db.one("SELECT display_name FROM contacts WHERE id=?", (contact_id,))["display_name"]
    captured = {}

    def fake_dry_run(_db, _config, _llm, _contact_id, options):
        captured.update(options)
        return {"mode": "Whole", "chunks": [], "estimator": {"approximate": True}}

    monkeypatch.setattr(routes, "dry_run_distill", fake_dry_run)
    await routes.distill_dry_run(SimpleNamespace(app=app), contact_id, routes.DistillDryRun())

    assert captured["target_name"] == expected
