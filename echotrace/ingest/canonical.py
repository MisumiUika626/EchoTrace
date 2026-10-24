"""Source-order message view shared by distillation and fingerprint checks."""
import hashlib

from echotrace.util import AppError, dumps


FINGERPRINT_FIELDS = ("sequence", "evidence_id", "msg_id", "ts", "sender", "text", "kind")


def message_fingerprint(rows):
    digest = hashlib.sha256()
    digest.update(b'{"messages":[')
    count = 0
    for row in rows:
        if count:
            digest.update(b",")
        digest.update(dumps({key: row[key] for key in FINGERPRINT_FIELDS}).encode("utf-8"))
        count += 1
    digest.update(b"]}")
    return digest.hexdigest()


def canonical_messages(db, contact_id):
    rows = db.rows(
        "SELECT m.id AS db_id,m.sequence,m.evidence_id,m.msg_id,m.ts,m.sender,m.text,m.kind "
        "FROM messages m JOIN sessions s ON s.id=m.session_id "
        "WHERE s.contact_id=? ORDER BY m.sequence", (contact_id,)
    )
    for sequence, row in enumerate(rows):
        if row["sequence"] != sequence or row["evidence_id"] != f"m{sequence + 1:06d}":
            raise AppError("INVALID_EVIDENCE", "消息顺序或 Evidence ID 不连续，请先修复数据。")
    sources = db.rows(
        "SELECT i.file,i.sha256,i.provenance FROM import_sources i "
        "JOIN sessions s ON s.id=i.session_id WHERE s.contact_id=? ORDER BY i.id", (contact_id,)
    )
    return {"messages": rows, "messages_sha256": message_fingerprint(rows), "sources": sources}
