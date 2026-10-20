from datetime import datetime

from echotrace.ingest.parsers import TZ
from echotrace.stats import LABELS


def timeline_events(db, cid, observations):
    """Date events from their source messages, keeping evidence inside this person."""
    events, seen = [], set()
    for item in observations:
        if item.get("dimension") != "events" or not item.get("evidence"):
            continue
        ids = list(dict.fromkeys(item["evidence"]))
        rows = db.rows("SELECT m.id,m.ts FROM messages m JOIN sessions s ON s.id=m.session_id "
                       f"WHERE s.contact_id=? AND m.id IN ({','.join('?' for _ in ids)})", (cid, *ids))
        if {row["id"] for row in rows} != set(ids):
            continue
        ts = min(row["ts"] for row in rows)
        key = (item["value"], tuple(sorted(ids)))
        if key in seen:
            continue
        seen.add(key)
        events.append({"date": datetime.fromtimestamp(ts, TZ).date().isoformat(), "ts": ts,
                       "event": item["value"], "evidence": ids, "inferred": item["inferred"],
                       "confidence": item["confidence"], "note": item.get("note", "")})
    return sorted(events, key=lambda event: event["ts"])


def score(model, stats, recent):
    result = []
    for key, label in LABELS.items():
        dim = next(d for d in model["dimensions"] if d["key"] == key).copy()
        anchor = stats["anchors"][key]
        dim.update(label=label, anchor_score=anchor["score"], score=round(.6 * anchor["score"] + .4 * dim["model_score"]), anchor=anchor)
        if abs(dim["model_score"] - dim["anchor_score"]) > 20 and not dim["conflict_note"].strip():
            raise ValueError(f"{key} 模型与统计相差超过20分，必须解释")
        if anchor["hits"] < 3 or stats["counts"].get("them", 0) < 100:
            dim["confidence"] = min(.49, dim["confidence"])
        if dim["score"] < 40 and "样本不足或信号偏弱" not in dim["interpretation"]:
            dim["interpretation"] += "；样本不足或信号偏弱。"
        delta = recent["anchors"][key]["score"] - anchor["score"]
        dim["anchor_trend"] = "rising" if delta > 5 else "falling" if delta < -5 else "flat"
        result.append(dim)
    return {"dimensions": result, "sample": stats["total"], "first_ts": stats["first_ts"], "last_ts": stats["last_ts"],
            "disclaimer": "这些指标是对文本的统计与模型推断，不是心理测评结论，不构成对真实人物的准确描述。"}
