from pathlib import Path

import httpx


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
