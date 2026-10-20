"""Abrupt-process recovery acceptance, using only fictional input and a mock provider."""
import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import httpx

from conftest import FIXTURE, FakeModel
from echotrace.app import create_app
from echotrace.ingest.cleaner import import_files
from echotrace.util import read_json, write_json


async def worker(output, resume):
    model = FakeModel()

    async def provider(request):
        body = json.loads(request.content)
        schema_line = next(line for line in body["messages"][0]["content"].splitlines() if line.startswith("{"))
        is_chunk = json.loads(schema_line)["title"] == "Chunk"
        # Let one whole map checkpoint persist, then stop inside the next request.
        if not resume and is_chunk and list((output / "app-data" / "people").glob("*/versions/v*/chunks/*.json")):
            write_json(output / "ready-to-kill.json", {"calls": len(model.calls)})
            await asyncio.Event().wait()
        return model(request)

    app = create_app(output / "app-data", llm_transport=httpx.MockTransport(provider))
    state = app.state
    state.config.update({"llm": {"base_url": "http://fiction.invalid/v1", "api_key": "fiction-only", "concurrency": 1},
                         "privacy": {"acknowledged": True}})
    await state.jobs.startup()
    if resume:
        row = state.db.one("SELECT id,status FROM jobs WHERE kind='distill'")
        assert row["status"] == "paused" and not model.calls
        write_json(output / "after-restart.json", {"status": row["status"], "model_calls_before_resume": len(model.calls)})
        job = state.jobs.resume(row["id"])
    else:
        cid = import_files(state.db, state.config, [FIXTURE], "林间（虚构）", "fiction-them", ["fiction-me"])["contact_id"]
        job = state.jobs.create("distill", {"contact_id": cid, "consent": True, "tier": "standard", "token_limit": 2000000})
    await state.jobs.tasks[job["id"]]
    result = state.jobs.get(job["id"])
    assert resume, "Initial worker completed without pausing at the checkpoint boundary"
    assert result["status"] == "succeeded", result
    chunks = list((output / "app-data" / "people").glob("*/versions/v*/chunks/*.json"))
    assert sum(stage == "Chunk" for stage, _, _ in model.calls) == len(chunks) - 1
    write_json(output / "completed.json", {"status": "succeeded", "chunks": len(chunks),
                                           "map_calls_after_resume": sum(stage == "Chunk" for stage, _, _ in model.calls)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.worker:
        asyncio.run(worker(args.worker, args.resume))
        return
    output = ROOT / "data" / "recovery" / str(time.time_ns())
    output.mkdir(parents=True)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(output)]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with (output / "worker.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while not (output / "ready-to-kill.json").exists():
                assert process.poll() is None, "Worker failed; inspect worker.log"
                if time.monotonic() >= deadline:
                    raise TimeoutError("Worker did not persist a checkpoint")
                time.sleep(.05)
            checkpoint = next((output / "app-data" / "people").glob("*/versions/v*/chunks/*.json"))
            digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
            process.kill()
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        resumed = subprocess.run(command + ["--resume"], cwd=ROOT, env=env, stdout=log, stderr=log, timeout=40)
    assert resumed.returncode == 0, "Resume failed; inspect worker.log"
    assert hashlib.sha256(checkpoint.read_bytes()).hexdigest() == digest
    report = {"status": "passed", "method": "kill child process after persisted map checkpoint; restart; explicit resume",
              "fictional_messages": 5000, "checkpoint_unchanged": True,
              **read_json(output / "after-restart.json"), **read_json(output / "completed.json")}
    report["status"] = "passed"
    write_json(output / "result.json", report)
    print(json.dumps({**report, "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
