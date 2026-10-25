"""Sanitized, offline diagnostics for the shared full planner."""
from dataclasses import dataclass

from echotrace.distill.planner import plan_full_chunks
from echotrace.ingest.canonical import canonical_messages, message_fingerprint


@dataclass
class PackedChunk:
    """Compatibility view for callers written before the dual-stage planner."""
    views: list[dict]
    budget: object
    new_units: int


def pack_message_views(llm, rows, settings):
    plan = plan_full_chunks(llm, rows, settings, "B", overlap_messages=0)
    chunks = [PackedChunk(list(chunk.views), chunk.budgets["distill_chunk"],
                          len(chunk.new_indexes)) for chunk in plan.chunks]
    return chunks, plan.fragmented


def _budget_descriptor(budget):
    return {
        "static_tokens": budget.static_request_tokens,
        "content_tokens": budget.content_tokens,
        "estimated_request_tokens": budget.request_tokens,
        "output_reserve": budget.output_reserve,
        "reasoning_reserve": budget.reasoning_reserve,
        "safety_margin": budget.safety_margin,
        "available_content_tokens": budget.available_content_tokens,
        "remaining_input_tokens": budget.remaining_input_tokens,
    }


def _descriptor(index, chunk):
    new_views = [chunk.views[position] for position in chunk.new_indexes]
    sequences = [view["sequence"] for view in new_views]
    fragments = [view for view in new_views if "fragment_index" in view]
    return {
        "index": index,
        "sequence_start": min(sequences),
        "sequence_end": max(sequences),
        "new_messages": len({view["id"] for view in new_views
                             if view.get("fragment_index", 0) == 0}),
        "new_fragments": len(fragments),
        "overlap_messages": len(chunk.views) - len(chunk.new_indexes),
        "fragmented_messages": len({view["id"] for view in fragments}),
        "stages": {stage: _budget_descriptor(budget)
                   for stage, budget in chunk.budgets.items()},
        "estimated_request_tokens": chunk.budgets["distill_chunk"].request_tokens,
    }


def dry_run_distill(db, config, llm, contact_id, options):
    """Return body-free diagnostics without secrets, network, or artifact writes."""
    canonical = canonical_messages(db, contact_id)
    scope = options.get("scope") or {}
    raw_messages = [row for row in canonical["messages"]
                    if (not scope.get("start") or row["ts"] >= int(scope["start"]))
                    and (not scope.get("end") or row["ts"] <= int(scope["end"]))]
    settings = dict(options)
    messages = raw_messages
    if settings.get("summary_only"):
        messages = [
            {**row, "sender": "A" if row["sender"] == "me" else "B",
             "text": f"[匿名统计：{row['kind']}消息，{len(row['text'])}字]"}
            for row in raw_messages
        ]
    plan = plan_full_chunks(llm, messages, settings, options.get("target_name", "B"))
    first = plan.chunks[0].budgets["distill_chunk"]
    return {
        "mode": plan.mode,
        "planner_version": plan.planner_version,
        "source": {"messages": len(raw_messages),
                   "messages_sha256": message_fingerprint(raw_messages),
                   "sources": len(canonical["sources"])},
        "capacity": {"context": first.context_capacity,
                     "max_output": first.output_capacity,
                     "source": first.capacity_source,
                     "verified": first.capacity_verified},
        "estimator": {"name": first.estimator, "approximate": first.approximate},
        "request": {"static_tokens": first.static_request_tokens,
                    "output_reserve": first.output_reserve,
                    "reasoning_reserve": first.reasoning_reserve,
                    "safety_margin": first.safety_margin},
        "fragmented_messages": len(plan.fragmented),
        "chunks": [_descriptor(index, chunk) for index, chunk in enumerate(plan.chunks)],
    }
