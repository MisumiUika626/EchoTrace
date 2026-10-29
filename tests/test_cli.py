import json
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import uvicorn


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "echotrace.cli", *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8", timeout=40,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})


def test_cli_requires_explicit_distill_consent():
    result = run_cli("distill", "--contact", 1)
    assert result.returncode == 2 and "--consent" in result.stderr


def test_cli_against_live_local_service(app, imported, model, monkeypatch):
    cid, path = imported
    sent = []

    def qce(request):
        if request.method == "POST":
            sent.append(json.loads(request.content))
            result = {"taskId": "fiction-cli"}
        else:
            result = {"status": "completed", "result": {"filePath": str(path)}}
        return httpx.Response(200, json={"success": True, "data": result})

    async def no_socket(*_):
        return

    app.state.qce.transport = httpx.MockTransport(qce)
    monkeypatch.setattr(app.state.qce, "watch", no_socket)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        assert server.started
        before_calls = len(model.calls)
        private_text = app.state.db.one(
            "SELECT text FROM messages WHERE session_id IN "
            "(SELECT id FROM sessions WHERE contact_id=?) ORDER BY sequence LIMIT 1",
            (cid,),
        )["text"]
        result = run_cli("distill", "--port", port, "--contact", cid, "--dry-run")
        assert result.returncode == 0, result.stderr + result.stdout
        assert "Traceback" not in result.stderr
        assert private_text not in result.stdout and "fiction-secret" not in result.stdout
        diagnostic = json.loads(result.stdout)
        assert diagnostic["mode"] in ("Whole", "Chunked") and diagnostic["chunks"]
        assert len(model.calls) == before_calls
        result = run_cli("distill", "--port", port, "--contact", cid, "--consent", "--tier", "quick")
        assert result.returncode == 0, result.stderr + result.stdout
        assert "Traceback" not in result.stderr
        assert private_text not in result.stdout and "fiction-secret" not in result.stdout
        job = json.loads(result.stdout.splitlines()[-1])
        assert job["status"] == "succeeded" and model.calls
        result = run_cli("export", "--port", port, "--name", "CLI虚构", "--me", "fiction-me",
                         "--peer-uid", "u_fiction", "--start", 1767220000, "--end", 1768220000,
                         "--format", "STREAMING_JSONL", "--no-auto-import")
        assert result.returncode == 0, result.stderr + result.stdout
        job = json.loads(result.stdout.splitlines()[-1])
        assert job["status"] == "succeeded" and not job["result"].get("import")
        assert sent[0]["format"] == "STREAMING_JSONL"
        result = run_cli("distill", "--port", port, "--contact", 999999, "--consent")
        assert result.returncode != 0 and "人物不存在" in result.stderr and "Traceback" not in result.stderr
    finally:
        server.should_exit = True
        thread.join(timeout=15)
