import hashlib
from collections import Counter
from pathlib import Path

from echotrace.ingest.parsers import parse_file
from echotrace.util import AppError, dumps, now, slug_for, stable_id, write_json


def preview(paths, custom_pattern=None):
    rows, speakers = [], Counter()
    for path in paths:
        for index, message in enumerate(parse_file(Path(path), custom_pattern)):
            speakers[(message["identity"], message["sender_raw"])] += 1
            if len(rows) < 50:
                rows.append({k: v for k, v in message.items() if k != "raw"})
            if index >= 499:
                break
    return {"messages": rows, "speakers": [{"identity": k[0], "name": k[1], "count": v} for k, v in speakers.items()],
            "sampled": True, "questions": ["请确认谁是我。昵称变更可用多个身份值映射到同一人。"]}


def import_files(db, config, paths, name, uid, me, custom_pattern=None, progress=None):
    if not name.strip() or not me:
        raise AppError("IDENTITY_REQUIRED", "请填写人物称呼，并确认至少一个「我」的身份。")
    cid = stable_id("qq:" + (uid.strip() or name.strip()))
    sid = stable_id(f"session:{cid}:1")
    contact = db.one("SELECT * FROM contacts WHERE id=?", (cid,))
    if db.one("SELECT id FROM jobs WHERE kind='distill' AND status IN ('queued','running','paused') AND json_extract(payload,'$.contact_id')=?", (cid,)):
        raise AppError("JOB_ALREADY_RUNNING", "请先完成或取消该人物的蒸馏，再追加记录。")
    slug = contact["slug"] if contact else slug_for(name, cid)
    with db.connect() as conn:
        conn.execute("INSERT OR IGNORE INTO contacts(id,uid,display_name,slug,created_at) VALUES(?,?,?,?,?)", (cid, uid or name, name, slug, now()))
        conn.execute("INSERT OR IGNORE INTO sessions(id,contact_id,name) VALUES(?,?,?)", (sid, cid, name))
    settings = config.get()["cleaning"]
    seen, added, filtered, repeats, empty, corrupt = 0, 0, 0, 0, 0, 0
    batch, previous = [], None
    identities = {str(x).strip().lstrip("@") for x in me}
    mappings = {r["identity"]: r["role"] for r in db.rows("SELECT identity,role FROM speaker_mappings WHERE session_id=?", (sid,))}

    def flush():
        nonlocal added
        if not batch:
            return
        with db.connect() as conn:
            # changes() after executemany is not cumulative; cursor.rowcount excludes ignored rows.
            cursor = conn.executemany("INSERT OR IGNORE INTO messages(id,session_id,msg_id,sender,sender_raw,ts,kind,text,reply_to,raw,hash,flags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            added += cursor.rowcount
        batch.clear()

    try:
        for path in paths:
            for message in parse_file(Path(path), custom_pattern):
                seen += 1
                if settings["filter_system"] and message["kind"] == "system":
                    filtered += 1
                    continue
                sender = "system" if message["kind"] == "system" else ("me" if message["identity"] in identities or message["sender_raw"] in identities else "them")
                sender = mappings.get(message["identity"], mappings.get(message["sender_raw"], sender))
                original = message["text"].replace("\r\n", "\n").strip()
                # Immutable source identity keeps evidence IDs stable after role corrections.
                digest = hashlib.sha1(dumps([sid, message["identity"], message["ts"], message["kind"], original]).encode()).hexdigest()
                mid = stable_id(digest)
                flags = {"media_only": message["kind"] in ("image", "emoji", "voice", "video"), "identity": message["identity"]}
                empty += not bool(original)
                corrupt += "\ufffd" in original
                flags["blank"], flags["encoding_issue"] = not bool(original), "\ufffd" in original
                text = original
                truncate_at = max(100, int(settings["truncate_at"]))
                if len(text) > truncate_at:
                    archive = db.root / "people" / slug / "long_text" / f"{mid}.txt"
                    archive.parent.mkdir(parents=True, exist_ok=True)
                    archive.write_text(text, encoding="utf-8")
                    text = text[:truncate_at] + "…[长文本已归档]"
                    flags["full_text"] = f"long_text/{mid}.txt"
                if settings["merge_repeats"] and previous and previous[0:2] == (sender, original) and 0 <= message["ts"] - previous[2] <= 30:
                    flags["repeat_of"] = previous[3]
                    repeats += 1
                else:
                    previous = (sender, original, message["ts"], mid)
                batch.append((mid, sid, message["msg_id"], sender, message["sender_raw"], message["ts"], message["kind"], text, message["reply_to"], dumps(message["raw"]), digest, dumps(flags)))
                if len(batch) >= 1000:
                    flush()
                    if progress:
                        progress(seen, added)
        flush()
    finally:
        with db.connect() as conn:
            conn.execute("UPDATE sessions SET msg_count=(SELECT COUNT(*) FROM messages WHERE session_id=?),first_ts=(SELECT MIN(ts) FROM messages WHERE session_id=?),last_ts=(SELECT MAX(ts) FROM messages WHERE session_id=?) WHERE id=?", (sid, sid, sid, sid))
            if added:
                conn.execute("UPDATE contacts SET revision=revision+1 WHERE id=?", (cid,))
    from echotrace.stats import calculate
    report = calculate(db, cid)
    report["import"] = {"read": seen, "added": added, "duplicates": seen - filtered - added, "filtered": filtered,
                        "collapsed_repeats": repeats, "blank": empty, "encoding_issues": corrupt}
    write_json(db.root / "people" / slug / "health.json", report)
    return {"contact_id": cid, "report": report}
