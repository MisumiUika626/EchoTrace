"""Disk-backed evidence hierarchy. Full observations remain available for retrieval."""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime

from echotrace.ingest.parsers import TZ
from echotrace.llm.client import estimate_tokens
from echotrace.util import dumps, read_json, write_json

DIMENSION_FILTERS = {
    "01-persona.md": ("personality", "interests"),
    "02-conversations.md": ("relationship", "emotion"),
    "03-expression-dna.md": ("expression_dna",),
    "04-external-views.md": ("relationship", "personality"),
    "05-decisions.md": ("events", "interests"),
    "06-timeline.md": ("events", "emotion", "personality"),
}


class EvidenceIndex:
    def __init__(self, target, canonical_messages=None):
        self.target, self.path = target, target / "evidence.sqlite"
        self.canonical_messages = canonical_messages

    def build(self, check=lambda: None):
        manifest = read_json(self.target / "manifest.json")
        identity = hashlib.sha256(dumps(manifest).encode()).hexdigest()
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as conn:
                if conn.execute("SELECT value FROM metadata WHERE key='identity'").fetchone() == (identity,):
                    return
        staging = self.path.with_suffix(".building.sqlite")
        staging.unlink(missing_ok=True)
        with closing(sqlite3.connect(staging)) as conn, conn:
            conn.executescript("""
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE messages(id TEXT PRIMARY KEY, sequence INTEGER, ts INTEGER, payload TEXT);
                CREATE INDEX message_sequence ON messages(sequence,id);
                CREATE TABLE observations(id TEXT PRIMARY KEY, dimension TEXT, period TEXT, ts INTEGER, payload TEXT);
                CREATE INDEX observation_group ON observations(dimension,period,ts,id);
            """)
            if self.canonical_messages is not None:
                conn.executemany("INSERT INTO messages VALUES(?,?,?,?)",
                                 ((m["id"], m["sequence"], m["ts"], dumps(m))
                                  for m in self.canonical_messages))
            for filename in manifest["chunks"]:
                check()
                messages = read_json(self.target / "inputs" / filename)
                lookup = {m["id"]: m for m in messages}
                if self.canonical_messages is None:
                    conn.executemany("INSERT OR IGNORE INTO messages VALUES(?,?,?,?)",
                                     ((m["id"], m.get("sequence", index), m["ts"], dumps(m))
                                      for index, m in enumerate(messages)))
                chunk = read_json(self.target / "chunks" / filename)
                for item in chunk["observations"]:
                    ts = min(lookup[mid]["ts"] for mid in item["evidence"])
                    date = datetime.fromtimestamp(ts, TZ).date().isoformat()
                    observation = {**item, "date": date}
                    dimension = item.get("dimension", item.get("topic", "open"))
                    # Identical wording at another date/source is a distinct record.
                    key = hashlib.sha256(dumps([observation["value"], dimension,
                                              sorted(observation["evidence"]), date, observation.get("quote")]).encode()).hexdigest()
                    conn.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)",
                                 (key, dimension, date[:7], ts, dumps(observation)))
            conn.execute("INSERT INTO metadata VALUES('identity',?)", (identity,))
        staging.replace(self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            groups = [{"dimension": d, "period": p, "observations": n} for d, p, n in
                      conn.execute("SELECT dimension,period,COUNT(*) FROM observations GROUP BY dimension,period ORDER BY period,dimension")]
            count = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        write_json(self.target / "coverage.json", {"total_messages": manifest["total"], "sampled_messages": manifest["sampled"],
            "sampling_ratio": manifest["sampled"] / max(1, manifest["total"]), "observations": count, "groups": groups,
            "note": "完整观察及出处保存在 evidence.sqlite 和 chunks；汇总按月份及主题轮选，不代表逐条语义覆盖，采样外内容未作模型分析。"})

    def select(self, dimensions=(), budget=12000, limit=240):
        result, used = [], 0
        where = "WHERE dimension IN (" + ",".join("?" for _ in dimensions) + ")" if dimensions else ""
        # SQLite sorts on disk; Python retains only the bounded selected packet.
        sql = f"""SELECT payload FROM (
            SELECT payload,period,dimension,ROW_NUMBER() OVER(
                PARTITION BY dimension,period ORDER BY ts,id) AS rank FROM observations {where}
            ) ORDER BY rank,period,dimension LIMIT 4000"""
        with closing(sqlite3.connect(self.path)) as conn:
            for (payload,) in conn.execute(sql, dimensions):
                item = json.loads(payload)
                cost = estimate_tokens(item)
                if used + cost > budget:
                    continue
                result.append(item)
                used += cost
                if len(result) >= limit:
                    break
        return result

    def messages(self, ids):
        result = []
        with closing(sqlite3.connect(self.path)) as conn:
            ids = sorted(ids)
            for offset in range(0, len(ids), 400):
                batch = ids[offset:offset + 400]
                result.extend(json.loads(payload) for (payload,) in conn.execute(
                    "SELECT payload FROM messages WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch))
        return sorted(result, key=lambda m: (m.get("sequence", 0), m["id"]))

    def packet(self, summary_only, dimensions=(), observation_budget=4000, message_budget=6500):
        observations = self.select(dimensions, observation_budget, 70)
        if not observations and dimensions:
            observations = self.select((), observation_budget, 70)
        ids = {mid for item in observations for mid in item["evidence"]}
        messages, used = [], 0
        for message in self.messages(ids):
            # Excerpts retain source offsets; do not let one giant message evict a month.
            text = message["text"]
            needle = next((item.get("quote") for item in observations if message["id"] in item["evidence"]
                           and item.get("quote") and item["quote"] in text), "")
            offset = max(0, text.find(needle) - 100) if needle else 0
            excerpt = {**message, "text": text[offset:offset + 1200]}
            if len(text) > 1200:
                excerpt.update(fragment_offset=offset, source_characters=len(text))
            cost = estimate_tokens(excerpt)
            if used + cost <= message_budget:
                messages.append(excerpt)
                used += cost
        allowed = {m["id"] for m in messages}
        observations = [item for item in observations if set(item["evidence"]) <= allowed]
        sizes = [len(m["text"]) for m in messages if m["sender"] == "them"]
        text = "" if summary_only else "\n".join(m["text"] for m in messages if m["sender"] == "them")
        if summary_only:
            from echotrace.distill.pipeline import summary_messages
            messages = summary_messages(messages)
            # An index may come from a raw-text run. Never send its quotations
            # or free-text conclusions when asked for anonymous statistics.
            observations = []
        return {"observations": observations, "evidence_messages": messages, "summary_only": summary_only,
                "expression_statistics": {"sample": len(sizes), "mean_characters": round(sum(sizes) / max(1, len(sizes)), 1),
                    "question_marks": text.count("？") + text.count("?"), "exclamation_marks": text.count("！") + text.count("!"),
                    "note": "仅为所选原文片段的统计，不能证明人物心理或全量语料偏好。"}}
