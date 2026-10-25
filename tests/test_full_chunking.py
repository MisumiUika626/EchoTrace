from dataclasses import asdict

from echotrace.distill.pipeline import (
    Pipeline,
    assert_distill_start_allowed,
    resolve_distill_mode,
    validate_full_coverage,
)
from echotrace.distill.planner import plan_full_chunks
from echotrace.ingest.canonical import canonical_messages
from echotrace.stats import calculate
from echotrace.util import AppError, read_json, write_json


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
    assert set(first.chunks[0].budgets) == {"distill_chunk", "extract_quotes"}


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
    assert all(set(descriptor["stage_budgets"]) == {"distill_chunk", "extract_quotes"}
               for descriptor in manifest["chunk_descriptors"])


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
