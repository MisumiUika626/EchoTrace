"""Pure deterministic Whole/Chunked planning for full distillation."""
from dataclasses import dataclass

from echotrace.ingest.canonical import message_fingerprint, model_message
from echotrace.llm.budget import RequestBudget, plan_request_budget
from echotrace.llm.client import build_request
from echotrace.llm.tokenizer import count_tokens
from echotrace.util import AppError

PLANNER_VERSION = "full-open-map-v3"
STAGES = ("full_map", "extract_quotes")


@dataclass(frozen=True)
class PlannedChunk:
    views: tuple[dict, ...]
    new_indexes: tuple[int, ...]
    budgets: dict[str, RequestBudget]


@dataclass(frozen=True)
class FullPlan:
    mode: str
    chunks: tuple[PlannedChunk, ...]
    fragmented: dict[str, int]
    source_fingerprint: str
    planner_version: str = PLANNER_VERSION


def stage_context(stage, views, target_name, settings, owned_ids=None):
    if stage == "full_map":
        return {"messages": views, "first_coverage_ids": list(owned_ids or [])}
    if stage == "extract_quotes":
        return {"messages": views, "target": "them"}
    raise AppError("INVALID_PROMPT", "完整分片规划只支持观察与语录阶段。")


def budgets_for_views(llm, views, settings, target_name, owned_ids=None):
    budgets = {}
    content = list(views)
    for stage in STAGES:
        context = stage_context(stage, content, target_name, settings, owned_ids)
        request = build_request(llm, stage, settings, context)
        budgets[stage] = plan_request_budget(stage, settings, request, content_value=content)
    return budgets


def _fits(llm, views, settings, target_name, owned_ids=None):
    try:
        return budgets_for_views(llm, views, settings, target_name, owned_ids)
    except AppError as exc:
        if exc.code != "REQUEST_OVERHEAD_TOO_LARGE":
            raise
        return None


def _largest_prefix(llm, base, text, settings, target_name):
    low, high, best = 1, len(text), 0
    while low <= high:
        middle = (low + high) // 2
        candidate = {**base, "text": text[:middle], "fragment_index": 999999,
                     "fragment_count": 999999, "fragment_offset": 999999999}
        if _fits(llm, [candidate], settings, target_name):
            best, low = middle, middle + 1
        else:
            high = middle - 1
    if not best:
        raise AppError("REQUEST_OVERHEAD_TOO_LARGE",
                       "完整请求的固定开销已耗尽上下文，无法容纳任何非空消息片段。")
    return best


def _fragment_units(llm, units, settings, target_name):
    result, fragmented = [], {}
    for view in units:
        if _fits(llm, [view], settings, target_name):
            result.append(view)
            continue
        text, pieces, offset = view["text"], [], 0
        while offset < len(text):
            size = _largest_prefix(llm, view, text[offset:], settings, target_name)
            pieces.append((offset, text[offset:offset + size]))
            offset += size
        count = len(pieces)
        fragmented[view["id"]] = count
        result.extend({**view, "text": piece, "fragment_index": index,
                       "fragment_count": count, "fragment_offset": start}
                      for index, (start, piece) in enumerate(pieces))
    return result, fragmented


def _largest_new_prefix(llm, overlap, units, start, settings, target_name):
    low, high, best_end, best_budgets = start + 1, len(units), start, None
    while low <= high:
        middle = (low + high) // 2
        candidate_new = units[start:middle]
        budgets = _fits(llm, [*overlap, *candidate_new], settings, target_name,
                        [view["id"] for view in candidate_new])
        if budgets:
            best_end, best_budgets, low = middle, budgets, middle + 1
        else:
            high = middle - 1
    return best_end, best_budgets


def _bounded_overlap(previous, settings, ratio, maximum):
    complete = [view for view in previous if "fragment_index" not in view]
    candidates = complete[-maximum:]
    if not candidates:
        return []
    # The ratio is measured against the receiving stage's content allowance.
    limit = min(maximum, len(candidates))
    candidates = candidates[-limit:]
    return candidates


def plan_full_chunks(llm, canonical_rows, settings, target_name, *,
                     overlap_messages=3, overlap_ratio=.05):
    """Plan source-order chunks admitted by both extraction request envelopes."""
    if not canonical_rows:
        raise AppError("NOT_ENOUGH_DATA", "没有可用于蒸馏的消息。")
    budgets_for_views(llm, [], settings, target_name)
    original = [model_message(row) for row in canonical_rows]
    units, fragmented = _fragment_units(llm, original, settings, target_name)
    chunks, position = [], 0
    previous_new = []
    while position < len(units):
        overlap = _bounded_overlap(previous_new, settings, overlap_ratio, overlap_messages)
        # Enforce the 5% cap using the receiving envelope, shrinking oldest first.
        while overlap:
            empty_budgets = budgets_for_views(llm, [], settings, target_name)
            cap = min(b.available_content_tokens for b in empty_budgets.values()) * overlap_ratio
            if count_tokens(overlap, settings["model"]).tokens <= cap:
                break
            overlap.pop(0)
        end, budgets = _largest_new_prefix(llm, overlap, units, position,
                                            settings, target_name)
        while end == position and overlap:
            overlap.pop(0)
            end, budgets = _largest_new_prefix(llm, overlap, units, position,
                                                settings, target_name)
        if end == position:
            raise AppError("REQUEST_OVERHEAD_TOO_LARGE", "消息片段无法放入空数据片。")
        views = (*overlap, *units[position:end])
        new_indexes = tuple(range(len(overlap), len(views)))
        chunks.append(PlannedChunk(views, new_indexes, budgets))
        previous_new = units[position:end]
        position = end
    return FullPlan("Whole" if len(chunks) == 1 else "Chunked", tuple(chunks),
                    fragmented, message_fingerprint(canonical_rows))
