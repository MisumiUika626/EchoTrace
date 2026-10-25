import pytest

from echotrace.distill.dry_run import dry_run_distill, pack_message_views
from echotrace.util import AppError, dumps


def _options(context_window, **changes):
    result = {
        "model": "private-model",
        "base_url": "http://127.0.0.1:9000/v1",
        "context_window": context_window,
        "context_window_confirmed": True,
        "max_tokens": 4096,
        "adapter": "none",
        "thinking": "low",
        "thinking_field": "reasoning_effort",
        "thinking_values": {"low": "low"},
        "temperature": .2,
        "summary_only": False,
    }
    result.update(changes)
    return result


def test_dry_run_selects_whole_or_chunked_from_complete_request(app, imported):
    contact_id, _ = imported

    whole = dry_run_distill(app.state.db, app.state.config, app.state.llm, contact_id,
                            _options(1_000_000))
    chunked = dry_run_distill(app.state.db, app.state.config, app.state.llm, contact_id,
                              _options(16_384))

    assert whole["mode"] == "Whole"
    assert len(whole["chunks"]) == 1
    assert chunked["mode"] == "Chunked"
    assert len(chunked["chunks"]) > 1
    assert chunked["source"]["messages"] == 600
    assert not any(key == "text" for key in _keys(chunked))


def test_dry_run_is_deterministic_and_prompt_changes_boundaries(app, imported, tmp_path):
    contact_id, _ = imported
    options = _options(16_384)

    first = dry_run_distill(app.state.db, app.state.config, app.state.llm, contact_id, options)
    second = dry_run_distill(app.state.db, app.state.config, app.state.llm, contact_id, options)
    assert dumps(first) == dumps(second)

    prompt_dir = app.state.config.prompt_dir()
    original = app.state.llm.template("distill_chunk")
    (prompt_dir / "distill_chunk.md").write_text(original + "\n额外静态规则" * 1200, encoding="utf-8")
    changed = dry_run_distill(app.state.db, app.state.config, app.state.llm, contact_id, options)

    assert [c["sequence_end"] for c in changed["chunks"]] != [c["sequence_end"] for c in first["chunks"]]


def test_packer_fragments_one_long_message_without_losing_text(app, imported):
    contact_id, _ = imported
    long_text = "甲乙ABC\n" * 12_000
    app.state.db.execute(
        "UPDATE messages SET text=? WHERE sequence=0 AND session_id IN (SELECT id FROM sessions WHERE contact_id=?)",
        (long_text, contact_id),
    )
    app.state.db.execute(
        "DELETE FROM messages WHERE sequence<>0 AND session_id IN (SELECT id FROM sessions WHERE contact_id=?)",
        (contact_id,),
    )
    canonical = __import__("echotrace.ingest.canonical", fromlist=["canonical_messages"]).canonical_messages(
        app.state.db, contact_id
    )
    chunks, _ = pack_message_views(app.state.llm, canonical["messages"], _options(16_384))
    fragments = [view for chunk in chunks for view in chunk.views]

    assert len(chunks) > 1
    assert all(view["text"] for view in fragments)
    assert "".join(view["text"] for view in fragments) == long_text
    assert {view["id"] for view in fragments} == {"m000001"}
    assert [view["fragment_index"] for view in fragments] == list(range(len(fragments)))
    assert all(view["fragment_count"] == len(fragments) for view in fragments)
    assert all(chunk.new_units > 0 for chunk in chunks)


def test_impossible_static_overhead_emits_no_chunks(app, imported):
    contact_id, _ = imported
    prompt_dir = app.state.config.prompt_dir()
    (prompt_dir / "distill_chunk.md").write_text("规则" * 30_000 + "\n{{schema}}\n{{context}}", encoding="utf-8")

    with pytest.raises(AppError) as error:
        dry_run_distill(app.state.db, app.state.config, app.state.llm, contact_id, _options(16_384))

    assert error.value.code == "REQUEST_OVERHEAD_TOO_LARGE"


def test_no_fixed_message_count_cap(app, imported):
    contact_id, _ = imported
    result = dry_run_distill(app.state.db, app.state.config, app.state.llm, contact_id,
                             _options(1_000_000))

    assert result["chunks"][0]["new_messages"] == 600
    assert result["chunks"][0]["sequence_end"] == 599


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)
