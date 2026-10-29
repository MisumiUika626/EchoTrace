"""Deterministic Evidence checks and aggregation for full distillation."""
import hashlib
from copy import deepcopy

from echotrace.llm.budget import plan_request_budget
from echotrace.llm.client import build_request
from echotrace.util import AppError, dumps, read_json, write_json


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
            {"messages": messages, "first_coverage_ids": sorted(owned_ids)},
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


def _reduce_context(findings, intermediate):
    return {"findings": findings, "intermediate": intermediate}


def _fits_reduce(llm, settings, findings, intermediate):
    context = _reduce_context(findings, intermediate)
    request = build_request(llm, "full_reduce", settings, context)
    try:
        plan_request_budget("full_reduce", settings, request, content_value=context)
        return True
    except AppError as exc:
        if exc.code == "REQUEST_OVERHEAD_TOO_LARGE":
            return False
        raise


def plan_reduce_groups(llm, settings, findings):
    """Greedily produce reproducible maximal groups under the complete request budget."""
    groups = []
    current = []
    for finding in findings:
        candidate = current + [finding]
        if _fits_reduce(llm, settings, candidate, True):
            current = candidate
            continue
        if not current:
            raise AppError("REDUCE_CONTEXT_TOO_LARGE", "单条 Map 观察无法放入 Reduce 完整请求。")
        groups.append(current)
        current = [finding]
        if not _fits_reduce(llm, settings, current, True):
            raise AppError("REDUCE_CONTEXT_TOO_LARGE", "单条 Map 观察无法放入 Reduce 完整请求。")
    if current:
        groups.append(current)
    return groups


async def _call_reduce(llm, job_id, payload, findings, messages, intermediate):
    allowed_ids = {message["id"] for message in messages}
    return await llm.call(
        "full_reduce",
        _reduce_context(findings, intermediate),
        allowed_ids,
        job_id,
        payload,
        lambda document: validate_findings(document, messages, allowed_ids),
        max_attempts=2,
    )


async def reduce_findings(llm, jobs, job_id, payload, findings, messages, path, fingerprint):
    """Reduce directly or through exactly one intermediate layer."""
    saved = read_json(path)
    if saved and saved.get("fingerprint") == fingerprint and saved.get("status") == "succeeded":
        return saved
    jobs.check(job_id)
    unique = deduplicate_findings(findings)
    settings = llm.settings(payload)
    if _fits_reduce(llm, settings, unique, False):
        output = await _call_reduce(llm, job_id, payload, unique, messages, False)
        result = {"fingerprint": fingerprint, "status": "succeeded",
                  "reduce_layers": 0, "output": output}
        write_json(path, result)
        return result

    groups = plan_reduce_groups(llm, settings, unique)
    intermediate_findings = []
    for index, group in enumerate(groups):
        jobs.check(job_id)
        checkpoint = path.with_name(path.name + f".group-{index}.json")
        group_fingerprint = hashlib.sha256(dumps([
            fingerprint,
            index,
            group,
            llm.template("full_reduce"),
        ]).encode()).hexdigest()
        saved_group = read_json(checkpoint)
        if saved_group and saved_group.get("fingerprint") == group_fingerprint:
            group_output = saved_group.get("output")
        else:
            group_output = await _call_reduce(llm, job_id, payload, group, messages, True)
            write_json(checkpoint, {"fingerprint": group_fingerprint, "output": group_output})
        intermediate_findings.extend(group_output.get("findings", []))
        intermediate_findings.extend(group_output.get("contradictions", []))
    intermediate_findings = deduplicate_findings(intermediate_findings)
    if not _fits_reduce(llm, settings, intermediate_findings, False):
        raise AppError("REDUCE_CONTEXT_TOO_LARGE",
                       "一层中间归并后最终 Reduce 仍超过上下文容量；不会继续递归归并。")
    output = await _call_reduce(llm, job_id, payload, intermediate_findings, messages, False)
    result = {"fingerprint": fingerprint, "status": "succeeded",
              "reduce_layers": 1, "output": output}
    write_json(path, result)
    return result
