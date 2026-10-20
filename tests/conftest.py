import json
from pathlib import Path

import httpx
import pytest

from echotrace.app import create_app
from echotrace.ingest.cleaner import import_files
from echotrace.llm.schemas import RADAR_KEYS, SECTIONS

FIXTURE = Path(__file__).parent / "fixtures" / "fictional.jsonl"


def claim(ids, value="从虚构消息中观察到表达习惯"):
    return {"value": value, "evidence": ids[:3], "inferred": False, "confidence": .7, "note": ""}


class FakeModel:
    """Deterministic provider exercising the real HTTP/schema/evidence boundary."""
    def __init__(self):
        self.calls = []
        self.invalid = False

    def __call__(self, request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "fiction-model"}]})
        body = json.loads(request.content)
        lines = [line for line in body["messages"][0]["content"].splitlines() if line.startswith("{")]
        schema, context = json.loads(lines[0]), json.loads(lines[-1])
        stage = schema["title"]
        self.calls.append((stage, context, body))
        messages = context.get("messages", [])
        ids = [m["id"] for m in messages]
        if not ids:
            def evidence(value):
                if isinstance(value, dict):
                    return value.get("evidence", []) + [x for k, v in value.items() if k != "evidence" for x in evidence(v)]
                return [x for v in value for x in evidence(v)] if isinstance(value, list) else []
            ids = list(dict.fromkeys(evidence(context)))
        if stage == "Chunk":
            selected = [m for m in messages if m["sender"] in ("them", "B")][:6]
            output = {"observations": [{**claim([m["id"]], m["text"]), "dimension": "events" if "周末" in m["text"] else "expression_dna", "quote": m["text"], "date": "2026-01-01"} for m in selected], "open_questions": []}
        elif stage == "Quotes":
            output = {"quotes": [{"evidence": [m["id"]], "text": m["text"], "topic": "虚构约定", "reuse": 8, "emotion": 7, "relation": 8, "info": 8, "reason": "保留虚构原话", "sensitive": False} for m in messages if m["sender"] == "them" and m["kind"] == "text"][:5]}
        elif stage == "Persona":
            output = {"sections": [{"key": key, "title": key, "items": [claim(ids)]} for key in SECTIONS], "tensions": [], "unknowns": ["虚构记录之外的信息未知"]}
        elif stage == "Radar":
            output = {"dimensions": [{"key": key, "model_score": context["stats"]["anchors"][key]["score"], "confidence": .7, "trend": "flat", "evidence": ids[:3], "interpretation": "虚构记录中的信号", "conflict_note": ""} for key in RADAR_KEYS]}
        elif stage == "Skill":
            output = {"models": [{**claim(ids), "name": f"观察模型{i}", "application": "只在有相似记录时使用", "limitation": "场景外证据不足"} for i in range(3)], "heuristics": [claim(ids, f"证据范围内的规则{i}") for i in range(5)]}
        elif stage == "SelfStyle":
            output = {k: claim(ids) for k in ("sentence_length", "punctuation", "emoji", "rhythm")}
            output["catchphrases"] = []
        elif stage == "Advice":
            output = {"situation_read": "从虚构文本观察到邀请。", "options": [{"strategy": strategy, "text": text, "why": "依据过往虚构约定", "evidence": ids[:1], "predicted_reaction": "可能继续交流", "risk": {"level": "low", "detail": "无法确定真人反应"}, "follow_up": "等对方确认"} for strategy, text in zip(("safe", "warm", "honest"), ("你方便的时候再说呀", "周末一起去看展吗", "我今天想先休息一下"))], "avoid": ["不代对方做承诺"], "confidence_note": "仅用于虚构流程测试"}
        elif stage == "Chat":
            output = {"text": "好呀，周末再一起看看", "evidence": ids[:1], "inferred": True}
        else:
            raise AssertionError(stage)
        if self.invalid:
            output = {"text": "错误证据", "evidence": [9999999999999999], "inferred": False}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output, ensure_ascii=False)}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})


@pytest.fixture
def model():
    return FakeModel()


@pytest.fixture
def app(tmp_path, model):
    app = create_app(tmp_path / "data", llm_transport=httpx.MockTransport(model))
    app.state.config.update({"llm": {"base_url": "http://fiction.invalid/v1", "api_key": "fiction-secret"}, "privacy": {"acknowledged": True}})
    return app


@pytest.fixture
def imported(app, tmp_path):
    small = tmp_path / "sample.jsonl"
    with FIXTURE.open(encoding="utf-8") as source, small.open("w", encoding="utf-8") as target:
        for i, line in enumerate(source):
            if i >= 600:
                break
            target.write(line)
    result = import_files(app.state.db, app.state.config, [small], "林间（虚构）", "fiction-them", ["fiction-me"])
    return result["contact_id"], small
