from pathlib import Path

import pytest

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
