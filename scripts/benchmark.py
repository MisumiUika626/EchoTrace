"""Measure the actual streaming import, including cleaning, FTS and health statistics."""
import argparse
import json
import os
import platform
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
from echotrace.config import Config
from echotrace.db import Database
from echotrace.ingest.cleaner import import_files
from generate_fixtures import generate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10000)
    parser.add_argument("--root", type=Path, default=Path("data/benchmark"))
    args = parser.parse_args()
    root = args.root / f"{args.count}-{time.time_ns()}"
    path = generate(root / "fictional.jsonl", args.count)
    db = Database(root / "data")
    process, stop = psutil.Process(os.getpid()), threading.Event()
    peak = [process.memory_info().rss]
    def sample():
        while not stop.wait(.02):
            peak[0] = max(peak[0], process.memory_info().rss)
    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    started = time.perf_counter()
    result = import_files(db, Config(db), [path], "完全虚构的性能样例", "fiction-benchmark", ["fiction-me"])
    elapsed = time.perf_counter() - started
    stop.set()
    monitor.join()
    report = {"records": args.count, "added": result["report"]["import"]["added"], "seconds": round(elapsed, 3),
              "peak_rss_mb": round(max(peak[0], process.memory_info().rss) / 1024**2, 2), "python": platform.python_version(),
              "platform": platform.platform(), "sqlite_vec": db.vector_available, "scope": "parse+clean+insert+FTS+stats"}
    (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report": str(root / "result.json")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
