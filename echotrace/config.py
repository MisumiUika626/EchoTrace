import copy
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .util import AppError, dumps, read_json, write_json

DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULTS = {
    "llm": {"base_url": "", "model": DEFAULT_MODEL, "thinking": "max", "temperature": 0.2,
            "adapter": "reasoning_effort", "thinking_field": "reasoning_effort",
            "thinking_values": {"low": "low", "medium": "medium", "high": "high", "max": "max"},
            "timeout": 120, "concurrency": 2, "max_tokens": 8192},
    "qce": {"base_url": "http://127.0.0.1:40653"},
    "privacy": {"allow_raw": True, "summary_only": False, "acknowledged": False},
    "cleaning": {"filter_system": True, "merge_repeats": True, "truncate_at": 2000},
    "advisor": {"recent_n": 100, "idle_hint": True, "token_budget": 16000},
    "embedding": {"provider": "local_hash", "local_model_path": ""},
    "radar": {"weights": [0.6, 0.4], "scales": {"affinity": 3, "intimacy": 4, "trust": 8,
              "dependence": 8, "initiative": 1, "stability": 1, "openness": 1, "progression": 8}},
}


def loopback_url(url: str) -> str:
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or p.hostname not in {"127.0.0.1", "localhost", "::1"} or p.username:
        raise AppError("LOCAL_ONLY", "QCE 地址必须是本机回环地址。")
    return url.rstrip("/")


class ModelSettings(BaseModel):
    base_url: str
    model: str = Field(min_length=1, max_length=200)
    thinking: str = Field(pattern="^(low|medium|high|max)$")
    temperature: float = Field(ge=0, le=2)
    adapter: str = Field(pattern="^(reasoning_effort|budget|custom|none)$")
    thinking_field: str = Field(pattern=r"^[a-zA-Z_][\w.]*$")
    thinking_values: dict
    timeout: int = Field(ge=5, le=1800)
    concurrency: int = Field(ge=1, le=4)
    max_tokens: int = Field(ge=256, le=393216)


class PrivacySettings(BaseModel):
    allow_raw: bool
    summary_only: bool
    acknowledged: bool


class CleaningSettings(BaseModel):
    filter_system: bool
    merge_repeats: bool
    truncate_at: int = Field(ge=100, le=100000)


class AdvisorSettings(BaseModel):
    recent_n: int = Field(ge=20, le=200)
    idle_hint: bool
    token_budget: int = Field(ge=6000, le=100000)


class EmbeddingSettings(BaseModel):
    provider: str = Field(pattern="^(local_hash|local_model)$")
    local_model_path: str = Field(max_length=2000)


class RadarSettings(BaseModel):
    weights: tuple[float, float]
    scales: dict[str, float]


class Config:
    def __init__(self, db):
        self.db, self.root = db, db.root
        self.lock = threading.RLock()
        self.secrets_path = self.root / ".secrets.json"

    def get(self, secrets=False):
        result = copy.deepcopy(DEFAULTS)
        import json
        for row in self.db.rows("SELECT * FROM settings"):
            if row["key"] in result:
                result[row["key"]].update(json.loads(row["value"]))
        keys = read_json(self.secrets_path, {})
        for group, key in (("llm", "api_key"), ("qce", "token")):
            result[group][key] = keys.get(group, "") if secrets else ("****" if keys.get(group) else "")
        result["data_dir"] = str(self.root)
        return result

    def update(self, patch):
        with self.lock:
            current = self.get(True)
            keys = read_json(self.secrets_path, {})
            for group in DEFAULTS:
                if group in patch:
                    if not isinstance(patch[group], dict):
                        raise AppError("INVALID_SETTINGS", "设置格式错误。")
                    current[group].update(patch[group])
            ModelSettings.model_validate(current["llm"])
            for group, schema in (("privacy", PrivacySettings), ("cleaning", CleaningSettings),
                                  ("advisor", AdvisorSettings), ("embedding", EmbeddingSettings)):
                schema.model_validate(current[group])
            radar = RadarSettings.model_validate(current["radar"])
            if radar.weights != (.6, .4) or set(radar.scales) != set(DEFAULTS["radar"]["scales"]) or any(not .1 <= v <= 100 for v in radar.scales.values()):
                raise AppError("INVALID_SETTINGS", "雷达权重固定为0.6/0.4，各维映射系数应在0.1到100之间。")
            if not isinstance(current["qce"]["base_url"], str):
                raise AppError("INVALID_URL", "QCE 地址需要字符串。")
            url = current["llm"]["base_url"]
            if url and (urlparse(url).scheme not in {"https", "http"} or not urlparse(url).hostname or urlparse(url).username):
                raise AppError("INVALID_URL", "模型地址需要完整的 http(s) URL。")
            loopback_url(current["qce"]["base_url"])
            for group, key in (("llm", "api_key"), ("qce", "token")):
                value = current[group].pop(key, "")
                if not isinstance(value, str):
                    raise AppError("INVALID_SETTINGS", "密钥需要字符串。")
                if value != "****":
                    keys[group] = value
            write_json(self.secrets_path, keys)
            os.chmod(self.secrets_path, 0o600)
            with self.db.connect() as conn:
                for group in DEFAULTS:
                    conn.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (group, dumps(current[group])))
        return self.get()

    def prompt_dir(self) -> Path:
        path = self.root / "prompts"
        path.mkdir(exist_ok=True)
        return path
