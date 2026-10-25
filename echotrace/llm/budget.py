"""Provider-independent complete-request accounting and admission."""
import math
from dataclasses import dataclass

from echotrace.llm.capabilities import resolve_capability
from echotrace.llm.tokenizer import count_tokens
from echotrace.util import AppError


OUTPUT_TARGETS = {
    "distill_chunk": 4096, "full_map": 4096, "full_reduce": 8192,
    "extract_quotes": 4096, "distill_persona": 8192,
    "distill_radar": 4096, "self_style": 4096, "distill_skill": 8192,
    "nuwa_research": 8192, "nuwa_synthesize": 12288, "nuwa_test_plan": 4096,
    "nuwa_respond": 4096, "nuwa_evaluate": 4096, "nuwa_refine": 4096,
    "nuwa_integrate": 2048, "advise_top3": 4096, "chat_roleplay": 2048,
}


@dataclass(frozen=True)
class RequestBudget:
    stage: str
    model: str
    context_capacity: int
    output_capacity: int
    capacity_source: str
    capacity_verified: bool
    estimator: str
    approximate: bool
    static_request_tokens: int
    content_tokens: int
    request_tokens: int
    output_reserve: int
    reasoning_reserve: int
    safety_margin: int
    available_content_tokens: int
    planned_input_tokens: int
    remaining_input_tokens: int


def plan_request_budget(stage, settings, request, attempt=0, content_value=None):
    capability = resolve_capability(
        settings["model"], settings.get("base_url", ""), settings["context_window"],
        settings["max_tokens"], settings.get("context_window_confirmed", False),
    )
    counted = count_tokens(request, settings["model"])
    if content_value is None:
        content, static_tokens = counted, 0
    else:
        content = count_tokens(content_value, settings["model"])
        static_tokens = max(0, counted.tokens - content.tokens)
    output_capacity = min(int(settings["max_tokens"]), capability.max_output_tokens)
    desired_output = min(output_capacity, OUTPUT_TARGETS[stage] * 2 ** attempt)
    reasoning_reserve = 0
    minimum_output = 256
    if settings.get("adapter") == "budget":
        reasoning_reserve = int(request.get("thinking", {}).get("budget_tokens", 0))
        minimum_output = reasoning_reserve + 1024
        if minimum_output > output_capacity:
            raise AppError("LLM_OUTPUT_BUDGET_TOO_SMALL",
                           "单次输出上限不足以容纳当前思考预算，请提高上限或降低思考等级。")
        desired_output = max(desired_output, minimum_output)
    safety_margin = max(1024, math.ceil(capability.context_window * .01))
    remaining_for_output = capability.context_window - counted.tokens - safety_margin
    if remaining_for_output < minimum_output:
        raise AppError("REQUEST_OVERHEAD_TOO_LARGE",
                       "完整请求（含提示词、schema、证据、输出预留和安全余量）超过上下文窗口。")
    output_reserve = min(desired_output, remaining_for_output)
    available = capability.context_window - static_tokens - output_reserve - safety_margin
    if available <= 0 or content.tokens > available:
        raise AppError("REQUEST_OVERHEAD_TOO_LARGE",
                       "完整请求（含提示词、schema、证据、输出预留和安全余量）超过上下文窗口。")
    return RequestBudget(
        stage=stage, model=settings["model"], context_capacity=capability.context_window,
        output_capacity=output_capacity, capacity_source=capability.source,
        capacity_verified=capability.verified, estimator=counted.name,
        approximate=counted.approximate or content.approximate,
        static_request_tokens=static_tokens, content_tokens=content.tokens,
        request_tokens=counted.tokens, output_reserve=output_reserve,
        reasoning_reserve=reasoning_reserve, safety_margin=safety_margin,
        available_content_tokens=available, planned_input_tokens=counted.tokens,
        remaining_input_tokens=available - content.tokens,
    )


def allowance(stage, settings, estimate, request, attempt=0):
    try:
        result = plan_request_budget(stage, settings, request, attempt)
    except AppError as exc:
        if exc.code == "REQUEST_OVERHEAD_TOO_LARGE":
            raise AppError(
                "LLM_CONTEXT_TOO_LARGE",
                "完整请求（含提示词、schema、证据和修复材料）超过上下文窗口。分片将自动拆分；"
                "汇总阶段请提高上下文窗口或缩短自定义提示词。",
            ) from exc
        raise
    return result.output_reserve, result.request_tokens
