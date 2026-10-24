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
