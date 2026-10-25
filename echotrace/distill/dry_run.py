"""Deterministic, offline planning for full distillation requests."""
from dataclasses import dataclass

from echotrace.ingest.canonical import canonical_messages, message_fingerprint, model_message
from echotrace.llm.budget import RequestBudget, plan_request_budget
from echotrace.llm.client import adapter_parameters
from echotrace.llm.nuwa_skill import nuwa_instructions
from echotrace.util import AppError


@dataclass
class PackedChunk:
    views: list[dict]
    budget: RequestBudget
    new_units: int


def _request(llm, settings, views):
    context = {
        "target_name": "B",
        "messages": views,
        "summary_only": bool(settings.get("summary_only", False)),
    }
    instruction = nuwa_instructions("distill_chunk")
    messages = ([{"role": "system", "content": instruction}] if instruction else [])
    messages.append({"role": "user", "content": llm.render("distill_chunk", context)})
    return {
        "model": settings["model"],
        "temperature": settings["temperature"],
        "max_tokens": settings["max_tokens"],
        "response_format": {"type": "json_object"},
        "messages": messages,
        **adapter_parameters(settings),
    }


def _budget(llm, settings, views):
    return plan_request_budget(
        "distill_chunk", settings, _request(llm, settings, views), content_value=views
    )


def _fits(llm, settings, views):
    try:
        return _budget(llm, settings, views)
    except AppError as exc:
        if exc.code != "REQUEST_OVERHEAD_TOO_LARGE":
            raise
        return None


def _largest_prefix(llm, settings, base, text):
    low, high, best, best_budget = 1, len(text), 0, None
    while low <= high:
        middle = (low + high) // 2
        candidate = {**base, "text": text[:middle], "fragment_index": 999999,
                     "fragment_count": 999999}
        budget = _fits(llm, settings, [candidate])
        if budget:
            best, best_budget, low = middle, budget, middle + 1
        else:
            high = middle - 1
    if not best:
        raise AppError(
            "REQUEST_OVERHEAD_TOO_LARGE",
            "完整请求的固定开销已耗尽上下文，无法容纳任何非空消息片段。",
        )
    return best, best_budget


def pack_message_views(llm, rows, settings):
    """Greedily pack source-order views; fragment only an unsplittable message."""
    # Prove that the static envelope can hold at least some content before emitting chunks.
    _budget(llm, settings, [])
    units = [model_message(row) for row in rows]
    fragmented = {}
    chunks, position = [], 0
    while position < len(units):
        low, high = position + 1, len(units)
        best_end, best_budget = position, None
        while low <= high:
            middle = (low + high) // 2
            budget = _fits(llm, settings, units[position:middle])
            if budget:
                best_end, best_budget, low = middle, budget, middle + 1
            else:
                high = middle - 1
        if best_end == position:
            view = units[position]
            if "fragment_index" in view:
                raise AppError("REQUEST_OVERHEAD_TOO_LARGE", "消息片段无法放入空数据片。")
            text, fragments = view["text"], []
            while text:
                size, _ = _largest_prefix(llm, settings, view, text)
                fragments.append({**view, "text": text[:size]})
                text = text[size:]
            count = len(fragments)
            fragmented[view["id"]] = count
            units[position:position + 1] = [
                {**fragment, "fragment_index": index, "fragment_count": count}
                for index, fragment in enumerate(fragments)
            ]
            continue
        views = units[position:best_end]
        chunks.append(PackedChunk(views, best_budget, len(views)))
        position = best_end
    return chunks, fragmented


def _descriptor(index, chunk, fragmented):
    sequences = [view["sequence"] for view in chunk.views]
    first_units = [view for view in chunk.views if view.get("fragment_index", 0) == 0]
    fragment_units = [view for view in chunk.views if "fragment_index" in view]
    return {
        "index": index,
        "sequence_start": min(sequences),
        "sequence_end": max(sequences),
        "new_messages": len(first_units),
        "new_fragments": len(fragment_units),
        "overlap_messages": 0,
        "estimated_request_tokens": chunk.budget.request_tokens,
        "fragmented_messages": len({view["id"] for view in fragment_units}),
    }


def dry_run_distill(db, config, llm, contact_id, options):
    """Return non-sensitive diagnostics without secrets, network, or artifact writes."""
    canonical = canonical_messages(db, contact_id)
    scope = options.get("scope") or {}
    raw_messages = [row for row in canonical["messages"]
                    if (not scope.get("start") or row["ts"] >= int(scope["start"]))
                    and (not scope.get("end") or row["ts"] <= int(scope["end"]))]
    if not raw_messages:
        raise AppError("NOT_ENOUGH_DATA", "没有可用于蒸馏的消息。")
    settings = dict(options)
    messages = raw_messages
    if settings.get("summary_only"):
        messages = [
            {**row, "sender": "A" if row["sender"] == "me" else "B",
             "text": f"[匿名统计：{row['kind']}消息，{len(row['text'])}字]"}
            for row in raw_messages
        ]
    chunks, fragmented = pack_message_views(llm, messages, settings)
    first = chunks[0].budget
    return {
        "mode": "Whole" if len(chunks) == 1 else "Chunked",
        "source": {
            "messages": len(raw_messages),
            "messages_sha256": message_fingerprint(raw_messages),
            "sources": len(canonical["sources"]),
        },
        "capacity": {
            "context": first.context_capacity,
            "max_output": first.output_capacity,
            "source": first.capacity_source,
            "verified": first.capacity_verified,
        },
        "estimator": {"name": first.estimator, "approximate": first.approximate},
        "request": {
            "static_tokens": first.static_request_tokens,
            "output_reserve": first.output_reserve,
            "reasoning_reserve": first.reasoning_reserve,
            "safety_margin": first.safety_margin,
        },
        "fragmented_messages": len(fragmented),
        "chunks": [_descriptor(index, chunk, fragmented) for index, chunk in enumerate(chunks)],
    }
