import hashlib
import json
import logging
import math
import re
import struct
from contextlib import nullcontext

import jieba

from echotrace.util import AppError, dumps, stable_id

jieba.setLogLevel(logging.ERROR)


def tokenize(text):
    return [t.strip().lower() for t in jieba.cut(text[:20000]) if re.search(r"\w", t)]


def embed(text, config=None):
    settings = config.get()["embedding"] if config else {"provider": "local_hash"}
    if settings["provider"] == "local_model":
        from pathlib import Path
        path = Path(settings["local_model_path"])
        if not path.is_dir():
            raise AppError("EMBEDDING_NOT_AVAILABLE", "本地嵌入模型目录不存在；可改用本地词项向量。")
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise AppError("EMBEDDING_NOT_AVAILABLE", "请安装可选 semantic 依赖后使用本地语义模型。") from exc
        cache = getattr(embed, "_models", {})
        key = str(path.resolve())
        if key not in cache:
            cache[key] = SentenceTransformer(key, local_files_only=True, truncate_dim=256)
            embed._models = cache
        values = list(cache[key].encode(text, normalize_embeddings=True))
        values = (values + [0.] * 256)[:256]
    else:
        values = [0.] * 256
        terms = tokenize(text)
        for term in terms:
            h = hashlib.blake2b(term.encode(), digest_size=4).digest()
            values[int.from_bytes(h[:2], "little") % 256] += 1 if h[2] % 2 else -1
    norm = math.sqrt(sum(x * x for x in values)) or 1
    return struct.pack("256f", *(x / norm for x in values))


def window(db, contact_id, message_id):
    center = db.one("SELECT m.* FROM messages m JOIN sessions s ON s.id=m.session_id "
                    "WHERE s.contact_id=? AND (m.evidence_id=? OR m.id=?)", (contact_id, message_id, message_id))
    if not center:
        raise AppError("INVALID_EVIDENCE", "消息不属于此人物。")
    sid, sequence = center["session_id"], center["sequence"]
    before = db.rows("SELECT evidence_id AS id,ts,sender,text FROM messages WHERE session_id=? AND sequence<? ORDER BY sequence DESC LIMIT 3", (sid, sequence))
    after = db.rows("SELECT evidence_id AS id,ts,sender,text FROM messages WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT 3", (sid, sequence))
    current = {"id": center["evidence_id"], "ts": center["ts"], "sender": center["sender"], "text": center["text"]}
    return list(reversed(before)) + [current] + after


def rebuild(db, config, contact_id, quotes, connection=None):
    # User picks survive every distillation/rollback.
    picks = db.rows("SELECT * FROM quotes WHERE contact_id=? AND source='user_pick'", (contact_id,))
    all_quotes = {q["id"]: q for q in quotes}
    for pick in picks:
        for key in ("dims", "evidence"):
            pick[key] = json.loads(pick[key])
        all_quotes[pick["id"]] = pick
    prepared = []
    for q in all_quotes.values():
        context = window(db, contact_id, q["evidence"][0])
        text = "\n".join(f"{m['sender']}: {m['text']}" for m in context)
        prepared.append((q, " ".join(tokenize(text)), embed(text, config)))
    with (nullcontext(connection) if connection is not None else db.connect()) as conn:
        old_ids = [r[0] for r in conn.execute("SELECT id FROM quotes WHERE contact_id=?", (contact_id,))]
        conn.executemany("DELETE FROM quotes_fts WHERE rowid=?", [(x,) for x in old_ids])
        if db.vector_available:
            db.load_vec(conn)
            conn.executemany("DELETE FROM quote_vectors WHERE id=?", [(x,) for x in old_ids])
        conn.execute("DELETE FROM quotes WHERE contact_id=?", (contact_id,))
        for q, tokens, vector in prepared:
            conn.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                q["id"], contact_id, q["start_msg"], q["end_msg"], q["text"], q["score"], dumps(q["dims"]),
                q.get("source", "llm"), vector, dumps(q["evidence"]), q["ts"], q.get("speaker", "them"), q["topic"], int(q.get("sensitive", False))))
            conn.execute("INSERT INTO quotes_fts(rowid,tokens,contact_id) VALUES(?,?,?)", (q["id"], tokens, contact_id))
            if db.vector_available:
                conn.execute("INSERT INTO quote_vectors(id,embedding) VALUES(?,?)", (q["id"], vector))


def from_model(db, contact_id, items, allow_sensitive=False):
    output = []
    for q in items:
        if q.get("sensitive") and not allow_sensitive:
            continue
        evidence = set(q["evidence"])
        sources = db.rows("SELECT m.evidence_id AS id,m.sender,m.text FROM messages m JOIN sessions s ON s.id=m.session_id "
                          "WHERE s.contact_id=? AND m.evidence_id IN (" + ",".join("?" for _ in evidence) + ")",
                          (contact_id, *evidence))
        if (not evidence or len(sources) != len(evidence) or not q["text"] or
                any(m["sender"] != "them" or q["text"] not in m["text"] for m in sources)):
            raise AppError("INVALID_EVIDENCE", "引用检查失败：原话必须逐字来自此人物的每条引用消息；旧检查点需复核后重新抽取。")
        context = window(db, contact_id, q["evidence"][0])
        source = next(m for m in context if m["id"] == q["evidence"][0])
        dims = {k: q[k] for k in ("reuse", "emotion", "relation", "info")}
        score = round(.35 * q["reuse"] + .3 * q["emotion"] + .2 * q["relation"] + .15 * q["info"], 1)
        output.append({"id": stable_id(f"quote:{contact_id}:{q['evidence']}:{q['text']}"), "contact_id": contact_id, "people_id": contact_id,
                       "start_msg": context[0]["id"], "end_msg": context[-1]["id"], "text": q["text"], "score": score, "dims": dims,
                       "source": "llm", "evidence": q["evidence"], "ts": source["ts"], "speaker": source["sender"], "topic": q["topic"], "sensitive": False})
    return output
