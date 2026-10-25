from dataclasses import asdict

from echotrace.distill.planner import plan_full_chunks
from echotrace.ingest.canonical import canonical_messages


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
