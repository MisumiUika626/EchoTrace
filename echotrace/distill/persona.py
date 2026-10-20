import json
from pathlib import Path

from echotrace.util import AppError, read_json


def person(db, cid):
    row = db.one("SELECT * FROM contacts WHERE id=?", (cid,))
    if not row:
        raise AppError("NOT_FOUND", "人物不存在。", 404)
    return row


def active_dir(db, cid):
    row = db.one("SELECT dir FROM personas WHERE contact_id=? AND active=1", (cid,))
    if not row:
        raise AppError("PERSONA_REQUIRED", "请先完成一次蒸馏，再使用此功能。")
    return Path(row["dir"])


def artifact(db, cid, name, version=None):
    if version is not None:
        row = db.one("SELECT dir FROM personas WHERE contact_id=? AND version=?", (cid, version))
        if not row:
            raise AppError("NOT_FOUND", "此版本不存在。", 404)
        path = Path(row["dir"])
    else:
        path = active_dir(db, cid)
    result = read_json(path / name)
    if result is None:
        raise AppError("ARTIFACT_NOT_FOUND", "此版本缺少所需产物。", 404)
    return result


def read_quotes(path):
    if not (path / "quotes.jsonl").exists():
        return []
    return [json.loads(line) for line in (path / "quotes.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
