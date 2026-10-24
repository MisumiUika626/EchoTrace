from pathlib import Path

import pytest

from echotrace.db import Database
from echotrace.ingest.parsers import parse_file
from echotrace.util import AppError


CHAT = "2024-03-01 21:03:12 小明(12345678)\n第一行\n第二行\n\n2024-03-01 21:04:05 小红(87654321)\n怎么了\n"


@pytest.mark.parametrize(
    ("encoding", "prefix", "expected"),
    [
        ("utf-8", b"\xef\xbb\xbf", "utf-8-sig"),
        ("gbk", b"", "gbk"),
        ("utf-16", b"\xff\xfe", "utf-16"),
    ],
)
def test_qq_txt_strictly_decodes_supported_encodings(tmp_path: Path, encoding: str, prefix: bytes, expected: str):
    path = tmp_path / "chat.txt"
    payload = CHAT.encode("utf-16-le" if encoding == "utf-16" else encoding)
    path.write_bytes(prefix + payload)

    rows = list(parse_file(path))

    assert [row["text"] for row in rows] == ["第一行\n第二行", "怎么了"]
    assert all(row["encoding"] == expected for row in rows)


def test_qq_txt_rejects_invalid_bytes_without_replacement(tmp_path: Path):
    path = tmp_path / "chat.txt"
    path.write_bytes(b"\x81\x00\xff\x00")

    with pytest.raises(AppError, match="编码"):
        list(parse_file(path))


def test_qq_txt_explicit_wrong_encoding_is_an_error(tmp_path: Path):
    path = tmp_path / "chat.txt"
    path.write_bytes(CHAT.encode("gbk"))

    with pytest.raises(AppError, match="gbk|编码"):
        list(parse_file(path, encoding="utf-8"))


def test_qq_txt_preserves_source_order_and_multiline_body(tmp_path: Path):
    path = tmp_path / "chat.txt"
    path.write_text(
        "2024-03-02 12:00:00 后发者(2)\n后时间先出现\n仍是正文\n\n"
        "2024-03-01 12:00:00 先发者(1)\n先时间后出现\n",
        encoding="utf-8",
    )

    rows = list(parse_file(path))

    assert [row["sender_raw"] for row in rows] == ["后发者(2)", "先发者(1)"]
    assert rows[0]["text"] == "后时间先出现\n仍是正文"


def test_qq_txt_reports_unknown_record_line_number(tmp_path: Path):
    path = tmp_path / "chat.txt"
    path.write_text("无法归属的记录\n2024-03-01 12:00:00 小明(1)\n你好\n", encoding="utf-8")

    with pytest.raises(AppError, match="第 1 行"):
        list(parse_file(path))


def test_inline_txt_preserves_body_leading_spaces(tmp_path: Path):
    path = tmp_path / "spaces.txt"
    path.write_text(
        "2024-03-01 12:00:00 me:   leading\n2024-03-01 12:00:01 them: reply\n",
        encoding="utf-8",
    )

    rows = list(parse_file(path, encoding="utf-8"))

    assert rows[0]["text"] == "  leading"


def test_header_txt_preserves_body_boundary_blank_lines(tmp_path: Path):
    path = tmp_path / "blank-lines.txt"
    path.write_text(
        "2024-03-01 12:00:00 me(1)\n\nleading blank\n\n\n"
        "2024-03-01 12:00:01 them(2)\nreply\n",
        encoding="utf-8",
    )

    rows = list(parse_file(path, encoding="utf-8"))

    assert rows[0]["text"] == "\nleading blank\n"


def test_qq_txt_preserves_media_placeholder_as_message_body(tmp_path: Path):
    path = tmp_path / "media.txt"
    path.write_text(
        "2024-03-01 12:00:00 me(1)\n[图片]\n\n"
        "2024-03-01 12:00:01 them(2)\n收到\n",
        encoding="utf-8",
    )

    rows = list(parse_file(path, encoding="utf-8"))

    assert rows[0]["text"] == "[图片]"


def test_fresh_schema_exposes_canonical_message_and_source_fields(tmp_path: Path):
    db = Database(tmp_path / "fresh")

    message_columns = {row["name"] for row in db.rows("PRAGMA table_info(messages)")}
    source_columns = {row["name"] for row in db.rows("PRAGMA table_info(import_sources)")}
    indexes = {row["name"] for row in db.rows("PRAGMA index_list(messages)")}

    assert {"sequence", "evidence_id", "source_index"} <= message_columns
    assert {"sha256", "messages_sha256", "encoding", "imported_at", "provenance"} <= source_columns
    assert {"messages_session_sequence", "messages_session_evidence"} <= indexes


def test_legacy_database_backfills_evidence_without_changing_primary_keys(tmp_path: Path):
    import sqlite3

    root = tmp_path / "legacy"
    root.mkdir()
    database = root / "echotrace.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE contacts (id INTEGER PRIMARY KEY, uid TEXT, display_name TEXT, slug TEXT, created_at INTEGER, revision INTEGER DEFAULT 0);
            CREATE TABLE sessions (id INTEGER PRIMARY KEY, contact_id INTEGER, name TEXT, msg_count INTEGER, first_ts INTEGER, last_ts INTEGER);
            CREATE TABLE messages (
              id INTEGER PRIMARY KEY, session_id INTEGER, msg_id TEXT, sender TEXT, sender_raw TEXT,
              ts INTEGER, kind TEXT, text TEXT, reply_to TEXT, raw JSON, hash TEXT UNIQUE, flags JSON);
            CREATE TABLE import_sources (
              id INTEGER PRIMARY KEY, session_id INTEGER, file TEXT, sha256 TEXT,
              messages_sha256 TEXT, encoding TEXT, imported_at INTEGER);
            INSERT INTO import_sources VALUES(1,2,'legacy.jsonl','source-hash','message-hash','utf-8',0);
            INSERT INTO contacts VALUES(1,'them','对方','legacy',0,0);
            INSERT INTO sessions VALUES(2,1,'对方',2,10,20);
            INSERT INTO messages VALUES(91,2,'late','me','我',20,'text','后时间先写入','','{}','h1','{}');
            INSERT INTO messages VALUES(37,2,'early','them','对方',10,'text','先时间后写入','','{}','h2','{}');
            """
        )

    db = Database(root)
    rows = db.rows("SELECT id,sequence,evidence_id,source_index,flags FROM messages ORDER BY sequence")

    assert [row["id"] for row in rows] == [37, 91]
    assert [row["sequence"] for row in rows] == [0, 1]
    assert [row["evidence_id"] for row in rows] == ["m000001", "m000002"]
    assert [row["source_index"] for row in rows] == [0, 1]
    assert all('"legacy_order":1' in row["flags"] for row in rows)
    assert db.one("SELECT provenance FROM import_sources WHERE id=1")["provenance"] == "{}"


def test_repeated_migration_preserves_populated_canonical_values(tmp_path: Path):
    root = tmp_path / "partial"
    db = Database(root)
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO contacts(id,uid,display_name,slug,created_at) VALUES(1,'them','对方','partial',0)"
        )
        connection.execute("INSERT INTO sessions(id,contact_id,name) VALUES(2,1,'对方')")
        connection.execute(
            "INSERT INTO messages(id,session_id,msg_id,sender,sender_raw,ts,kind,text,raw,hash,flags,sequence,evidence_id,source_index) "
            "VALUES(9,2,'kept','them','对方',10,'text','保留','{}','hash-kept','{}',7,'custom-evidence',4)"
        )

    Database(root)
    row = Database(root).one(
        "SELECT id,sequence,evidence_id,source_index FROM messages WHERE id=9"
    )

    assert row == {"id": 9, "sequence": 7, "evidence_id": "custom-evidence", "source_index": 4}


def _store(tmp_path: Path):
    from echotrace.config import Config

    db = Database(tmp_path / "data")
    return db, Config(db)


def _jsonl(path: Path, rows):
    import json

    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _two_party_rows(first="一", second="二"):
    return [
        {"msgId": "1", "timestamp": 1, "sender": {"uid": "me", "name": "我"}, "content": first},
        {"msgId": "2", "timestamp": 2, "sender": {"uid": "them", "name": "对方"}, "content": second},
    ]


def test_import_preserves_long_text_verbatim(tmp_path: Path):
    from echotrace.ingest.cleaner import import_files

    path = tmp_path / "long.jsonl"
    long_text = "开头\n" + "甲" * 2500 + "\n结尾"
    _jsonl(path, _two_party_rows(long_text, "收到"))
    db, config = _store(tmp_path)

    result = import_files(db, config, [path], "对方", "them", ["me"])
    stored = db.one(
        "SELECT m.text FROM messages m JOIN sessions s ON s.id=m.session_id "
        "WHERE s.contact_id=? ORDER BY m.id LIMIT 1",
        (result["contact_id"],),
    )

    assert stored["text"] == long_text


def test_import_rejects_third_sender_before_writing(tmp_path: Path):
    from echotrace.ingest.cleaner import import_files

    path = tmp_path / "three.jsonl"
    _jsonl(path, [
        *_two_party_rows(),
        {"msgId": "3", "timestamp": 3, "sender": {"uid": "third", "name": "第三人"}, "content": "三"},
    ])
    db, config = _store(tmp_path)

    with pytest.raises(AppError, match="第三|发送者|身份"):
        import_files(db, config, [path], "对方", "them", ["me"])

    assert db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 0


def test_import_rejects_missing_self_identity(tmp_path: Path):
    from echotrace.ingest.cleaner import import_files

    path = tmp_path / "missing-self.jsonl"
    _jsonl(path, [
        {"msgId": "1", "timestamp": 1, "sender": {"uid": "them", "name": "对方"}, "content": "一"},
        {"msgId": "2", "timestamp": 2, "sender": {"uid": "other", "name": "另一身份"}, "content": "二"},
    ])
    db, config = _store(tmp_path)

    with pytest.raises(AppError, match="本人|身份"):
        import_files(db, config, [path], "对方", "them", ["me"])


def test_import_accepts_partner_nickname_changes_with_same_identity(tmp_path: Path):
    from echotrace.ingest.cleaner import import_files

    path = tmp_path / "aliases.jsonl"
    _jsonl(path, [
        _two_party_rows()[0],
        {"msgId": "2", "timestamp": 2, "sender": {"uid": "them", "name": "旧昵称"}, "content": "二"},
        {"msgId": "3", "timestamp": 3, "sender": {"uid": "them", "name": "新昵称"}, "content": "三"},
    ])
    db, config = _store(tmp_path)

    result = import_files(db, config, [path], "对方", "them", ["me"])

    assert result["report"]["total"] == 3


def test_import_rejects_empty_session(tmp_path: Path):
    from echotrace.ingest.cleaner import import_files

    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    db, config = _store(tmp_path)

    with pytest.raises(AppError, match="消息|空"):
        import_files(db, config, [path], "对方", "them", ["me"])


def test_import_assigns_sequence_and_evidence_ids_in_source_order(tmp_path: Path):
    from echotrace.ingest.cleaner import import_files

    path = tmp_path / "order.jsonl"
    _jsonl(path, [
        {"msgId": "late", "timestamp": 20, "sender": {"uid": "me", "name": "我"}, "content": "先出现"},
        {"msgId": "early", "timestamp": 10, "sender": {"uid": "them", "name": "对方"}, "content": "后出现"},
    ])
    db, config = _store(tmp_path)

    result = import_files(db, config, [path], "对方", "them", ["me"])
    rows = db.rows(
        "SELECT m.sequence,m.evidence_id,m.source_index,m.text FROM messages m "
        "JOIN sessions s ON s.id=m.session_id WHERE s.contact_id=? ORDER BY m.sequence",
        (result["contact_id"],),
    )

    assert rows == [
        {"sequence": 0, "evidence_id": "m000001", "source_index": 0, "text": "先出现"},
        {"sequence": 1, "evidence_id": "m000002", "source_index": 1, "text": "后出现"},
    ]


def test_duplicate_import_keeps_ids_and_fingerprints_stable(tmp_path: Path):
    from echotrace.ingest.cleaner import import_files

    path = tmp_path / "same.jsonl"
    _jsonl(path, _two_party_rows())
    db, config = _store(tmp_path)

    first = import_files(db, config, [path], "对方", "them", ["me"])
    before = db.rows("SELECT id,sequence,evidence_id,text FROM messages ORDER BY sequence")
    second = import_files(db, config, [path], "对方", "them", ["me"])
    after = db.rows("SELECT id,sequence,evidence_id,text FROM messages ORDER BY sequence")

    assert first["source"]["sha256"] == second["source"]["sha256"]
    assert first["source"]["messages_sha256"] == second["source"]["messages_sha256"]
    assert second["duplicate"] is True
    assert before == after


def test_canonical_messages_recompute_stable_fingerprint_after_append(tmp_path: Path):
    from echotrace.ingest.canonical import canonical_messages
    from echotrace.ingest.cleaner import import_files

    first_path, second_path = tmp_path / "first.jsonl", tmp_path / "second.jsonl"
    _jsonl(first_path, [
        {"msgId": "late", "timestamp": 20, "sender": {"uid": "me", "name": "我"}, "content": "先出现\n全文"},
        {"msgId": "early", "timestamp": 10, "sender": {"uid": "them", "name": "对方"}, "content": "后出现"},
    ])
    _jsonl(second_path, [
        {"msgId": "last", "timestamp": 5, "sender": {"uid": "them", "name": "对方"}, "content": "最后追加"},
    ])
    db, config = _store(tmp_path)
    first = import_files(db, config, [first_path], "对方", "them", ["me"])
    snapshot = canonical_messages(db, first["contact_id"])
    appended = import_files(db, config, [second_path], "对方", "them", ["me"])
    current = canonical_messages(db, first["contact_id"])

    assert [(m["sequence"], m["evidence_id"], m["text"]) for m in snapshot["messages"]] == [
        (0, "m000001", "先出现\n全文"), (1, "m000002", "后出现")
    ]
    assert snapshot["messages_sha256"] == first["source"]["messages_sha256"]
    assert [m["evidence_id"] for m in current["messages"]] == ["m000001", "m000002", "m000003"]
    assert current["messages_sha256"] == appended["source"]["messages_sha256"]
    assert current["messages_sha256"] != snapshot["messages_sha256"]


def test_platform_ids_and_source_positions_define_idempotency(tmp_path: Path):
    from echotrace.ingest.cleaner import import_files

    first_path, second_path = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    _jsonl(first_path, [
        {"timestamp": 1, "sender": {"uid": "me", "name": "我"}, "content": "一"},
        {"timestamp": 2, "sender": {"uid": "them", "name": "对方"}, "content": "相同"},
        {"timestamp": 2, "sender": {"uid": "them", "name": "对方"}, "content": "相同"},
    ])
    _jsonl(second_path, [
        {"msgId": "3", "timestamp": 3, "sender": {"uid": "me", "name": "我"}, "content": "三"},
        {"msgId": "4", "timestamp": 4, "sender": {"uid": "them", "name": "对方"}, "content": "四"},
    ])
    db, config = _store(tmp_path)

    import_files(db, config, [first_path], "对方", "them", ["me"])
    import_files(db, config, [first_path, second_path], "对方", "them", ["me"])

    assert db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 5
    assert db.one("SELECT COUNT(*) AS n FROM import_sources")["n"] == 2


def test_append_rejects_third_identity_without_changing_existing_data(tmp_path: Path):
    from echotrace.ingest.cleaner import import_files

    first_path, second_path = tmp_path / "first.jsonl", tmp_path / "second.jsonl"
    _jsonl(first_path, _two_party_rows())
    _jsonl(second_path, [
        {"msgId": "3", "timestamp": 3, "sender": {"uid": "me", "name": "我"}, "content": "三"},
        {"msgId": "4", "timestamp": 4, "sender": {"uid": "third", "name": "第三人"}, "content": "四"},
    ])
    db, config = _store(tmp_path)
    import_files(db, config, [first_path], "对方", "them", ["me"])

    with pytest.raises(AppError, match="第三|发送者|身份"):
        import_files(db, config, [second_path], "对方", "them", ["me"])

    assert db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 2
    assert db.one("SELECT COUNT(*) AS n FROM import_sources")["n"] == 1


def test_second_pass_failure_rolls_back_all_database_writes(tmp_path: Path, monkeypatch):
    from echotrace.ingest import cleaner
    from echotrace.ingest.cleaner import import_files

    path = tmp_path / "unstable.jsonl"
    path.write_text("placeholder", encoding="utf-8")
    messages = [
        {"msg_id": "1", "identity": "me", "sender_raw": "我", "text": "一", "kind": "text", "ts": 1,
         "reply_to": "", "raw": {}, "encoding": "utf-8"},
        {"msg_id": "2", "identity": "them", "sender_raw": "对方", "text": "二", "kind": "text", "ts": 2,
         "reply_to": "", "raw": {}, "encoding": "utf-8"},
    ]
    calls = 0

    def unstable_parser(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        yield messages[0]
        if calls == 2:
            raise AppError("IMPORT_PARSE_FAILED", "第二遍解析失败")
        yield messages[1]

    monkeypatch.setattr(cleaner, "parse_file", unstable_parser)
    db, config = _store(tmp_path)

    with pytest.raises(AppError, match="第二遍"):
        import_files(db, config, [path], "对方", "them", ["me"])

    assert db.one("SELECT COUNT(*) AS n FROM contacts")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM sessions")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM messages")["n"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM import_sources")["n"] == 0


def test_import_source_provenance_is_persisted_without_affecting_fingerprint(tmp_path: Path):
    import json

    from echotrace.ingest.cleaner import import_files

    path = tmp_path / "qce.jsonl"
    _jsonl(path, _two_party_rows())
    db, config = _store(tmp_path)
    source_meta = {"kind": "qce", "task_id": "task-7", "partial": True, "server_completeness_proven": False}

    result = import_files(db, config, [path], "对方", "them", ["me"], source_meta=source_meta)
    source = db.one("SELECT provenance,messages_sha256 FROM import_sources")

    assert json.loads(source["provenance"]) == source_meta
    assert source["messages_sha256"] == result["source"]["messages_sha256"]


@pytest.mark.asyncio
async def test_import_job_forwards_encoding_and_source_provenance(tmp_path: Path, monkeypatch):
    import echotrace.app as app_module

    captured = {}

    def fake_import(*args):
        captured["args"] = args
        return {"ok": True}

    monkeypatch.setattr(app_module, "import_files", fake_import)
    application = app_module.create_app(tmp_path / "job-data")
    source_meta = {"kind": "qce", "task_id": "task-9", "partial": False}

    result = await application.state.jobs.handlers["import"]("job-1", {
        "paths": ["fixture.txt"], "name": "对方", "uid": "them", "me": ["me"],
        "pattern": None, "encoding": "gbk", "source_meta": source_meta,
    })

    assert result == {"ok": True}
    assert captured["args"][8] == "gbk"
    assert captured["args"][9] == source_meta


def test_pipeline_prepare_uses_source_order_and_stable_evidence_ids(tmp_path: Path):
    from echotrace.distill.pipeline import Pipeline
    from echotrace.ingest.cleaner import import_files
    from echotrace.stats import calculate
    from echotrace.util import read_json

    path = tmp_path / "source-order.jsonl"
    rows = [
        {"msgId": str(index), "timestamp": 1000 - index,
         "sender": {"uid": "me" if index % 2 else "them", "name": "我" if index % 2 else "对方"},
         "content": f"源顺序 {index}"}
        for index in range(1, 201)
    ]
    _jsonl(path, rows)
    db, config = _store(tmp_path)
    result = import_files(db, config, [path], "对方", "them", ["me"])
    target = tmp_path / "version"

    manifest = Pipeline(db, config, None, None).prepare(
        result["contact_id"], target, {"tier": "deep"}, calculate(db, result["contact_id"])
    )
    messages = [message for filename in manifest["chunks"] for message in read_json(target / "inputs" / filename)]

    assert [message["id"] for message in messages[:3]] == ["m000001", "m000002", "m000003"]
    assert [message["text"] for message in messages[:3]] == ["源顺序 1", "源顺序 2", "源顺序 3"]


def test_evidence_index_accepts_string_ids_and_preserves_excerpt_identity(tmp_path: Path):
    from echotrace.distill.evidence import EvidenceIndex
    from echotrace.util import write_json

    target = tmp_path / "evidence-version"
    message = {"id": "m000001", "sender": "them", "ts": 1, "text": "甲" * 1400, "kind": "text"}
    write_json(target / "manifest.json", {"chunks": ["00000.json"], "total": 1, "sampled": 1})
    write_json(target / "inputs/00000.json", [message])
    write_json(target / "chunks/00000.json", {"observations": [{
        "value": "长消息", "dimension": "expression_dna", "evidence": ["m000001"],
        "quote": "甲" * 10, "inferred": False, "confidence": .8, "note": "",
    }]})

    index = EvidenceIndex(target)
    index.build()
    packet = index.packet(False)

    assert index.messages({"m000001"})[0]["id"] == "m000001"
    assert packet["evidence_messages"][0]["id"] == "m000001"
    assert packet["evidence_messages"][0]["source_characters"] == 1400


def test_claim_schema_accepts_stable_string_evidence_ids():
    from echotrace.llm.schemas import Claim

    claim = Claim(value="可回查观察", evidence=["m000001"], inferred=False, confidence=.8)

    assert claim.evidence == ["m000001"]
