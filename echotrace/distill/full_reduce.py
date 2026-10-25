"""Deterministic Evidence checks and aggregation for full distillation."""
from copy import deepcopy

from echotrace.util import AppError, read_json, write_json


def _speaker(message):
    return message.get("sender")


def validate_findings(document, messages, allowed_ids, owned_ids=None):
    """Validate mechanical Evidence properties without claiming semantic support."""
    result = deepcopy(document)
    lookup = {message["id"]: message for message in messages}
    allowed = set(allowed_ids)
    owned = set(owned_ids) if owned_ids is not None else None
    for field in ("findings", "contradictions"):
        for item in result.get(field, []):
            evidence = item.get("evidence", [])
            if len(evidence) != len(set(evidence)):
                raise ValueError("Evidence 不能包含重复 ID")
            if not evidence or not set(evidence).issubset(allowed) or not set(evidence).issubset(lookup):
                raise ValueError("Evidence ID 不在本次授权范围")
            if owned is not None and not set(evidence).intersection(owned):
                raise ValueError("分片观察必须至少引用一条本片首次覆盖消息")
            sources = [lookup[evidence_id] for evidence_id in evidence]
            targets = [message for message in sources if _speaker(message) in ("them", "B")]
            source_kind = item.get("source_kind")
            if source_kind == "direct" and not targets:
                raise ValueError("direct 结论必须有目标本人发言")
            if source_kind == "reported" and not str(item.get("note", "")).strip():
                raise ValueError("转述必须注明发言者和未核实限制")
            if source_kind == "inference" and (not item.get("inferred") or not str(item.get("note", "")).strip()):
                raise ValueError("推断必须明确标记并注明依据")
            quote = item.get("quote", "")
            if quote and not any(quote in message.get("text", "") for message in targets):
                raise ValueError("quote 必须逐字出自目标本人消息")
    return result


def deduplicate_findings(items):
    """Merge overlap duplicates by normalized conclusion, never by vote count."""
    result = []
    positions = {}
    for source in items:
        item = deepcopy(source)
        key = (" ".join(item.get("topic", "").split()).casefold(),
               " ".join(item.get("value", "").split()).casefold())
        if key not in positions:
            positions[key] = len(result)
            result.append(item)
            continue
        current = result[positions[key]]
        current["evidence"] = list(dict.fromkeys(current.get("evidence", []) + item.get("evidence", [])))
        if item.get("confidence", 0) > current.get("confidence", 0):
            current["confidence"] = item["confidence"]
        if not current.get("quote") and item.get("quote"):
            current["quote"] = item["quote"]
    return result


async def map_chunk(llm, jobs, job_id, payload, messages, owned_ids, path, fingerprint):
    """Run a full Map request with one repair opportunity and durable state."""
    saved = read_json(path)
    if saved and saved.get("fingerprint") == fingerprint and saved.get("status") == "succeeded":
        return saved
    allowed_ids = {message["id"] for message in messages}
    try:
        output = await llm.call(
            "full_map",
            {"messages": messages},
            allowed_ids,
            job_id,
            payload,
            lambda document: validate_findings(document, messages, allowed_ids, owned_ids),
            max_attempts=2,
        )
        result = {"fingerprint": fingerprint, "status": "succeeded", "output": output}
    except AppError as exc:
        result = {"fingerprint": fingerprint, "status": "failed",
                  "error": {"code": exc.code, "message": exc.message}}
    write_json(path, result)
    return result
