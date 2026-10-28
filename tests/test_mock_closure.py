from pathlib import Path

import httpx
import pytest

from echotrace.distill.canonical_skill import REQUIRED_CANONICAL_FILES
from echotrace.distill.persona import active_dir
from echotrace.util import read_json


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


async def test_dry_run_closure_reads_no_secret_calls_no_provider_and_writes_no_skill(
    app, imported, monkeypatch
):
    """Catches dry-run regressions that leak content or cross an I/O boundary."""
    from echotrace import config as config_module

    contact_id, _ = imported
    first_message = app.state.db.one(
        "SELECT text FROM messages WHERE session_id IN "
        "(SELECT id FROM sessions WHERE contact_id=?) ORDER BY sequence LIMIT 1",
        (contact_id,),
    )["text"]
    original_read = config_module.read_json

    def reject_secret(path, default=None):
        if path == app.state.config.secrets_path:
            raise AssertionError("dry-run read the API key store")
        return original_read(path, default)

    def reject_provider(request):
        raise AssertionError(f"dry-run called provider: {request.url}")

    app.state.llm.transport = httpx.MockTransport(reject_provider)
    monkeypatch.setattr(config_module, "read_json", reject_secret)
    before = {path.relative_to(app.state.db.root) for path in app.state.db.root.rglob("*")}
    before_jobs = app.state.db.one("SELECT COUNT(*) AS n FROM jobs")["n"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/contacts/{contact_id}/distill/dry-run",
            json={
                "context_window": 16_384,
                "context_window_confirmed": True,
                "max_tokens": 4_096,
            },
        )

    assert response.status_code == 200, response.text
    diagnostic = response.json()["data"]
    assert diagnostic["mode"] == "Chunked"
    assert diagnostic["chunks"]
    assert "text" not in set(_keys(diagnostic))
    assert first_message not in response.text
    assert "fiction-secret" not in response.text
    assert app.state.db.one("SELECT COUNT(*) AS n FROM jobs")["n"] == before_jobs
    assert {path.relative_to(app.state.db.root) for path in app.state.db.root.rglob("*")} == before
    assert not list(Path(app.state.db.root).glob("people/*/versions/*/skill/SKILL.md"))


def assert_complete_full(root, expected_mode, expected_messages):
    manifest = read_json(root / "distill_manifest.json")
    metadata = read_json(root / "metadata.json")

    assert manifest["mode"] == "full"
    assert manifest["complete"] is True
    assert metadata["complete"] is True
    assert manifest["coverage"]["plan_mode"] == expected_mode
    assert manifest["coverage"]["total_messages"] == expected_messages
    assert manifest["coverage"]["first_covered_messages"] == expected_messages
    assert manifest["coverage"]["chunks_succeeded"] == manifest["coverage"]["chunks"]
    assert manifest["coverage"]["failed_chunks"] == []
    assert manifest["evidence"] == {"validated": True, "phase4_complete": True}
    assert manifest["nuwa"] == {
        "status": "passed",
        "format_passed": True,
        "semantic_passed": True,
    }
    assert all(manifest["gates"].values())
    for relative in REQUIRED_CANONICAL_FILES:
        path = root / relative
        assert path.is_file() and path.stat().st_size > 0, relative


@pytest.mark.parametrize(
    ("context_window", "expected_mode"),
    [(1_000_000, "Whole"), (32_768, "Chunked")],
)
async def test_fictional_full_closure_publishes_complete_whole_and_chunked(
    app, imported, context_window, expected_mode
):
    """Catches a planner path that stops before publishing canonical full output."""
    contact_id, _ = imported
    app.state.config.update(
        {"llm": {"context_window": context_window, "context_window_confirmed": True}}
    )

    job = app.state.jobs.create(
        "distill",
        {
            "contact_id": contact_id,
            "consent": True,
            "mode": "full",
            "send_raw": True,
            "token_limit": 2_000_000,
        },
    )
    await app.state.jobs.tasks[job["id"]]
    result = app.state.jobs.get(job["id"])

    assert result["status"] == "succeeded", result
    assert_complete_full(active_dir(app.state.db, contact_id), expected_mode, 600)
