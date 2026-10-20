import hashlib
import json
import os
import re
import time
import tempfile
from pathlib import Path


class AppError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def now() -> int:
    return int(time.time())


def stable_id(value: str) -> int:
    return int(hashlib.sha256(value.encode()).hexdigest()[:13], 16)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path: Path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, default=str))


def atomic_write(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def slug_for(name: str, contact_id: int) -> str:
    return f"{re.sub(r'[^a-zA-Z0-9_-]', '-', name).strip('-') or 'person'}-{contact_id:x}"


def redact(text: str) -> str:
    text = re.sub(r"\b\d{7,19}[Xx]?\b", "[号码已隐藏]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已隐藏]", text)
    text = re.sub(r"(?:地址|住址|家庭住址)[:：]?[^\n，。;；]{2,80}", "[地址已隐藏]", text)
    return text
