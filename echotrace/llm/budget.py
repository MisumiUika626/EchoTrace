"""Provider-independent, conservative request admission (not an exact tokenizer)."""
import math

from echotrace.util import AppError


OUTPUT_TARGETS = {
    "distill_chunk": 4096, "extract_quotes": 4096, "distill_persona": 8192,
    "distill_radar": 4096, "self_style": 4096, "distill_skill": 8192,
    "nuwa_research": 8192, "nuwa_synthesize": 12288, "nuwa_test_plan": 4096,
    "nuwa_respond": 4096, "nuwa_evaluate": 4096, "nuwa_refine": 4096,
    "nuwa_integrate": 2048, "advise_top3": 4096, "chat_roleplay": 2048,
}


def allowance(stage, settings, estimate, request, attempt=0):
    ceiling = settings["max_tokens"]
    target = min(ceiling, OUTPUT_TARGETS[stage] * 2 ** attempt)
    # Reasoning budgets are part of the output allowance, not extra free tokens.
    minimum = 256
    if settings["adapter"] == "budget":
        minimum = request["thinking"]["budget_tokens"] + 1024
        if minimum > ceiling:
            raise AppError("LLM_OUTPUT_BUDGET_TOO_SMALL", "单次输出上限不足以容纳当前思考预算，请提高上限或降低思考等级。")
        target = max(target, minimum)
    input_tokens = math.ceil(estimate(request) * 1.25) + 256
    remaining = settings.get("context_window", 131072) - input_tokens - 1024
    if remaining < minimum:
        raise AppError("LLM_CONTEXT_TOO_LARGE", "完整请求（含提示词、schema、证据和修复材料）超过上下文窗口。分片将自动拆分；汇总阶段请提高上下文窗口或缩短自定义提示词。")
    return min(target, remaining), input_tokens
