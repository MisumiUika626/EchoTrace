"""Bounded extraction with durable split trees and source-based attribution."""
import hashlib
from datetime import datetime

from echotrace.distill.quotes import validate_quotes
from echotrace.ingest.parsers import TZ
from echotrace.util import AppError, dumps, read_json, write_json


RECOVERABLE = {"LLM_OUTPUT_TRUNCATED", "LLM_CONTEXT_TOO_LARGE", "LLM_OUTPUT_INVALID"}


def normalize_reported_observations(output, messages):
    """Separate a verified other-speaker quote from the subject's own words.

    Only already-labelled inferences supported exclusively by other-speaker
    messages qualify. Never invent a quote, change evidence IDs or promote a
    direct observation to an inference to bypass validation.
    """
    lookup = {m["id"]: m for m in messages}
    corrections = []
    for index, item in enumerate(output["observations"]):
        sources = [lookup[mid] for mid in item["evidence"] if mid in lookup]
        quote = item["quote"]
        if (item["inferred"] and quote.strip() and sources
                and len(sources) == len(item["evidence"])
                and all(m["sender"] in ("me", "A") for m in sources)
                and any(quote in m["text"] for m in sources)):
            corrections.append({"index": index, "original_quote": quote, "original_note": item["note"], "evidence": item["evidence"],
                                "reason": "另一位发言人的引文不能作为目标本人原话；保留为有来源的推断。"})
            item["quote"] = ""
            prefix = "仅依据另一位发言人的话推断，非目标本人原话；"
            item["note"] = prefix + item["note"][:400 - len(prefix)]
    return corrections


def validate_observations(output, messages):
    lookup = {m["id"]: m for m in messages}
    for item in output["observations"]:
        sources = [lookup[mid] for mid in item["evidence"] if mid in lookup]
        targets = [m for m in sources if m["sender"] in ("them", "B")]
        if not targets and not item["inferred"]:
            raise ValueError("人物直接观察必须有目标本人发言；他人转述只能标为推断并注明限制")
        if item["quote"] and not any(item["quote"] in m["text"] for m in targets):
            raise ValueError("观察引文必须逐字出自目标本人，不能引用另一位发言人的话；无本人原话时 quote 应为空字符串，转述只能标为推断并注明依据")


def split_messages(messages):
    if len(messages) > 1:
        middle = len(messages) // 2
        return messages[:middle], messages[middle:]
    if messages and len(messages[0]["text"]) > 256:
        message = messages[0]
        middle = len(message["text"]) // 2
        offset = message.get("fragment_offset", 0)
        return ([{**message, "text": message["text"][:middle], "fragment_offset": offset}],
                [{**message, "text": message["text"][middle:], "fragment_offset": offset + middle}])
    return None


async def extract(llm, jobs, job_id, payload, stage, context, path, fingerprint, depth=0):
    """Never repeat a failed parent after splitting; completed children survive restarts."""
    jobs.check(job_id)
    saved = read_json(path)
    if saved and saved.get("fingerprint") == fingerprint:
        return saved["output"]
    messages = context["messages"]
    if stage == "extract_quotes" and not any(m["sender"] == "them" for m in messages):
        # A reply's embedded original text/QQ ID is not a source message in
        # this chunk. An empty quote set is definitive, even for an old split
        # leaf with a failed repair checkpoint. Keep review files for audit.
        output = {"quotes": []}
        write_json(path, {"fingerprint": fingerprint, "output": output,
                          "reason": "no_target_messages"})
        return output
    ids = {m["id"] for m in messages}
    field = "observations" if stage == "distill_chunk" else "quotes"
    validator = (lambda output: validate_observations(output, messages)) if stage == "distill_chunk" else (lambda output: validate_quotes(output, messages))
    split_path = path.with_name(path.name + ".split.json")
    plan = read_json(split_path)
    if not plan or plan.get("fingerprint") != fingerprint:
        try:
            output = await llm.call(stage, context, ids, job_id, payload, validator,
                                    output_growth=depth >= 6 or split_messages(messages) is None,
                                    normalizer=(lambda output: normalize_reported_observations(output, messages)) if stage == "distill_chunk" else None)
            if stage == "distill_chunk":
                lookup = {m["id"]: m for m in messages}
                for item in output[field]:
                    dates = sorted({lookup[mid].get("date") or datetime.fromtimestamp(lookup[mid]["ts"], TZ).date().isoformat()
                                    for mid in item["evidence"]})
                    item["date"], item["source_dates"] = dates[0], dates
            write_json(path, {"fingerprint": fingerprint, "output": output})
            return output
        except AppError as exc:
            if exc.code not in RECOVERABLE:
                raise
            parts = split_messages(messages) if depth < 6 else None
            if parts is None:
                raise AppError(exc.code, exc.message + " 已到自动拆分边界，检查点及待复核输出已保留。", exc.status) from exc
            # Only the decision is persisted: children are deterministic views of
            # the existing input, with original IDs and character offsets intact.
            write_json(split_path, {"fingerprint": fingerprint, "reason": exc.code})
    parts = split_messages(messages)
    outputs = []
    for index, part in enumerate(parts):
        child_hash = hashlib.sha256(dumps([fingerprint, stage, part]).encode()).hexdigest()
        outputs.append(await extract(llm, jobs, job_id, payload, stage, {**context, "messages": part},
                                     path.with_name(path.name + f".{index}.json"), child_hash, depth + 1))
    output = {field: [item for result in outputs for item in result[field]]}
    if stage == "distill_chunk":
        output["open_questions"] = list(dict.fromkeys(q for result in outputs for q in result.get("open_questions", [])))
    write_json(path, {"fingerprint": fingerprint, "output": output})
    return output
