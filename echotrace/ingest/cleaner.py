import hashlib
from collections import Counter
from pathlib import Path

from echotrace.ingest.parsers import parse_file
from echotrace.util import AppError, dumps, now, slug_for, stable_id, write_json


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def preview(paths, custom_pattern=None, encoding=None):
    rows, speakers = [], Counter()
    for path in paths:
        for index, message in enumerate(parse_file(Path(path), custom_pattern, encoding)):
            speakers[(message["identity"], message["sender_raw"])] += 1
            if len(rows) < 50:
                rows.append({k: v for k, v in message.items() if k != "raw"})
            if index >= 499:
                break
    return {"messages": rows, "speakers": [{"identity": k[0], "name": k[1], "count": v} for k, v in speakers.items()],
            "sampled": True, "questions": ["请确认谁是我。昵称变更可用多个身份值映射到同一人。"]}


def import_files(db, config, paths, name, uid, me, custom_pattern=None, progress=None, encoding=None, source_meta=None):
    if not name.strip() or not me:
        raise AppError("IDENTITY_REQUIRED", "请填写人物称呼，并确认至少一个「我」的身份。")
    cid = stable_id("qq:" + (uid.strip() or name.strip()))
    sid = stable_id(f"session:{cid}:1")
    paths = [Path(path) for path in paths]
    source_parts = [(str(path.resolve()), _sha256_file(path)) for path in paths]
    source_hashes = {path: sha256 for path, sha256 in source_parts}
    source_sha256 = source_parts[0][1] if len(source_parts) == 1 else hashlib.sha256(dumps(source_parts).encode()).hexdigest()
    contact = db.one("SELECT * FROM contacts WHERE id=?", (cid,))
    if db.one("SELECT id FROM jobs WHERE kind='distill' AND status IN ('queued','running','paused') AND json_extract(payload,'$.contact_id')=?", (cid,)):
        raise AppError("JOB_ALREADY_RUNNING", "请先完成或取消该人物的蒸馏，再追加记录。")
    imported_sources = {
        row["sha256"]: row for row in db.rows("SELECT * FROM import_sources WHERE session_id=?", (sid,))
    }
    provenance = source_meta or {"kind": "local_file"}
    if contact and source_parts and all(sha256 in imported_sources for _, sha256 in source_parts):
        if source_meta:
            with db.connect() as conn:
                conn.executemany(
                    "UPDATE import_sources SET provenance=? WHERE session_id=? AND sha256=?",
                    [(dumps(provenance), sid, sha256) for _, sha256 in source_parts],
                )
        duplicate_source = imported_sources[source_parts[-1][1]]
        from echotrace.stats import calculate
        report = calculate(db, cid)
        report["import"] = {"read": 0, "added": 0, "duplicates": report["total"], "filtered": 0,
                            "collapsed_repeats": 0, "blank": 0, "encoding_issues": 0}
        return {"contact_id": cid, "report": report, "duplicate": True,
                "source": {"sha256": source_sha256, "messages_sha256": duplicate_source["messages_sha256"],
                           "encoding": duplicate_source["encoding"]}}
    settings = config.get()["cleaning"]
    identities = {str(x).strip().lstrip("@") for x in me}
    observed, kept = {}, 0
    for row in db.rows("SELECT json_extract(flags,'$.identity') AS identity,sender_raw FROM messages WHERE session_id=?", (sid,)):
        if row["identity"]:
            observed.setdefault(row["identity"], set()).add(row["sender_raw"])
    detected_encodings = set()
    for path in paths:
        for message in parse_file(path, custom_pattern, encoding):
            if message.get("encoding"):
                detected_encodings.add(message["encoding"])
            if settings["filter_system"] and message["kind"] == "system":
                continue
            if not message["text"].replace("\r\n", "\n").strip():
                continue
            observed.setdefault(message["identity"], set()).add(message["sender_raw"])
            kept += 1
            if progress and kept % 1000 == 0:
                progress(kept, 0)
    if not kept:
        raise AppError("IMPORT_EMPTY", "清洗后没有可保留的消息，导入已中止。")
    self_seen = {identity for identity in observed if identity in identities}
    if not self_seen:
        raise AppError("IDENTITY_REQUIRED", "授权输入中没有精确匹配到本人身份。")
    others = {identity for identity in observed if identity not in identities}
    if len(others) != 1:
        raise AppError("IDENTITY_AMBIGUOUS", "清洗后必须且只能确认一位对方发送者；检测到第三位发送者或身份冲突。")

    slug = contact["slug"] if contact else slug_for(name, cid)
    seen, added, filtered, repeats, empty, corrupt = 0, 0, 0, 0, 0, 0
    batch, previous = [], None
    mappings = {r["identity"]: r["role"] for r in db.rows("SELECT identity,role FROM speaker_mappings WHERE session_id=?", (sid,))}
    existing_roles = {}
    for row in db.rows(
        "SELECT json_extract(flags,'$.identity') AS identity,sender "
        "FROM messages WHERE session_id=? AND sender IN ('me','them')",
        (sid,),
    ):
        if row["identity"]:
            existing_roles.setdefault(row["identity"], set()).add(row["sender"])
    for identity, roles in existing_roles.items():
        requested = mappings.get(identity, "me" if identity in identities else "them")
        if roles != {requested}:
            raise AppError(
                "IDENTITY_CONFLICT",
                "追加导入会改变既有发言人的本人/对方身份；请先使用身份修正功能明确调整。",
            )
    existing_hashes = {r["hash"] for r in db.rows("SELECT hash FROM messages WHERE session_id=?", (sid,))}
    existing_platform_ids = {
        r["msg_id"] for r in db.rows("SELECT msg_id FROM messages WHERE session_id=? AND msg_id<>''", (sid,))
    }
    next_sequence = db.one("SELECT COALESCE(MAX(sequence),-1)+1 AS n FROM messages WHERE session_id=?", (sid,))["n"]
    staged = 0

    def flush(conn):
        nonlocal staged
        if not batch:
            return
        conn.executemany("INSERT INTO staged_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        staged += len(batch)
        batch.clear()

    detected_encoding = next(iter(detected_encodings)) if len(detected_encodings) == 1 else ("mixed" if detected_encodings else None)
    with db.connect() as conn:
        conn.execute(
            "CREATE TEMP TABLE staged_messages(id,session_id,msg_id,sender,sender_raw,ts,kind,text,reply_to,raw,hash,flags,sequence,evidence_id,source_index)"
        )
        for path in paths:
            file_sha256 = source_hashes[str(path.resolve())]
            source_index = 0
            for message in parse_file(path, custom_pattern, encoding):
                seen += 1
                if settings["filter_system"] and message["kind"] == "system":
                    filtered += 1
                    continue
                sender = "system" if message["kind"] == "system" else ("me" if message["identity"] in identities else "them")
                sender = mappings.get(message["identity"], sender)
                original = message["text"].replace("\r\n", "\n")
                current_source_index = source_index
                source_index += 1
                if message["msg_id"] and message["msg_id"] in existing_platform_ids:
                    continue
                source_identity = ["platform", message["msg_id"]] if message["msg_id"] else [
                    "source", file_sha256, current_source_index
                ]
                digest = hashlib.sha1(dumps([sid, source_identity]).encode()).hexdigest()
                if digest in existing_hashes:
                    continue
                existing_hashes.add(digest)
                if message["msg_id"]:
                    existing_platform_ids.add(message["msg_id"])
                mid = stable_id(digest)
                flags = {"media_only": message["kind"] in ("image", "emoji", "voice", "video"), "identity": message["identity"]}
                empty += not bool(original.strip())
                corrupt += "\ufffd" in original
                flags["blank"], flags["encoding_issue"] = not bool(original.strip()), "\ufffd" in original
                if flags["blank"]:
                    continue
                if settings["merge_repeats"] and previous and previous[0:2] == (sender, original) and 0 <= message["ts"] - previous[2] <= 30:
                    flags["repeat_of"] = previous[3]
                    repeats += 1
                else:
                    previous = (sender, original, message["ts"], mid)
                sequence = next_sequence + staged + len(batch)
                batch.append((mid, sid, message["msg_id"], sender, message["sender_raw"], message["ts"], message["kind"], original,
                              message["reply_to"], dumps(message["raw"]), digest, dumps(flags), sequence,
                              f"m{sequence + 1:06d}", current_source_index))
                if len(batch) >= 1000:
                    flush(conn)
                if progress and seen % 1000 == 0:
                    progress(seen, staged + len(batch))
        flush(conn)
        if [(str(path.resolve()), _sha256_file(path)) for path in paths] != source_parts:
            raise AppError("IMPORT_SOURCE_CHANGED", "导入期间源文件发生变化，已回滚本次写入。")
        conn.execute("INSERT OR IGNORE INTO contacts(id,uid,display_name,slug,created_at) VALUES(?,?,?,?,?)", (cid, uid or name, name, slug, now()))
        conn.execute("INSERT OR IGNORE INTO sessions(id,contact_id,name) VALUES(?,?,?)", (sid, cid, name))
        cursor = conn.execute(
            "INSERT INTO messages(id,session_id,msg_id,sender,sender_raw,ts,kind,text,reply_to,raw,hash,flags,sequence,evidence_id,source_index) "
            "SELECT id,session_id,msg_id,sender,sender_raw,ts,kind,text,reply_to,raw,hash,flags,sequence,evidence_id,source_index "
            "FROM staged_messages ORDER BY sequence"
        )
        added = cursor.rowcount
        conn.execute("UPDATE sessions SET msg_count=(SELECT COUNT(*) FROM messages WHERE session_id=?),first_ts=(SELECT MIN(ts) FROM messages WHERE session_id=?),last_ts=(SELECT MAX(ts) FROM messages WHERE session_id=?) WHERE id=?", (sid, sid, sid, sid))
        if added:
            conn.execute("UPDATE contacts SET revision=revision+1 WHERE id=?", (cid,))
        from echotrace.ingest.canonical import message_fingerprint
        messages_sha256 = message_fingerprint(conn.execute(
            "SELECT sequence,evidence_id,msg_id,ts,sender,text,kind FROM messages WHERE session_id=? ORDER BY sequence", (sid,)
        ))
        conn.executemany(
            "INSERT OR REPLACE INTO import_sources(session_id,file,sha256,messages_sha256,encoding,imported_at,provenance) VALUES(?,?,?,?,?,?,?)",
            [(sid, str(path), source_hashes[str(path.resolve())], messages_sha256, detected_encoding, now(), dumps(provenance))
             for path in paths],
        )
    from echotrace.stats import calculate
    report = calculate(db, cid)
    report["import"] = {"read": seen, "added": added, "duplicates": seen - filtered - added, "filtered": filtered,
                        "collapsed_repeats": repeats, "blank": empty, "encoding_issues": corrupt}
    write_json(db.root / "people" / slug / "health.json", report)
    return {"contact_id": cid, "report": report, "duplicate": False,
            "source": {"sha256": source_sha256, "messages_sha256": messages_sha256, "encoding": detected_encoding}}
