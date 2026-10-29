"""Offline model-capacity metadata with explicit fallback for unknown providers."""
from dataclasses import dataclass
from urllib.parse import urlparse

from echotrace.util import AppError


@dataclass(frozen=True)
class ModelCapability:
    model: str
    official_host: str
    context_window: int
    max_output_tokens: int
    source: str


@dataclass(frozen=True)
class ResolvedCapability:
    context_window: int
    max_output_tokens: int
    source: str
    verified: bool


CAPABILITIES = {
    model.model: model for model in (
        ModelCapability("deepseek-flash", "api.deepseek.com", 1048576, 393216,
                        "deepseek-official-2026-10-25"),
        ModelCapability("deepseek-v4-pro", "api.deepseek.com", 1048576, 393216,
                        "deepseek-official-2026-10-25"),
    )
}


def resolve_capability(model, base_url, configured_context, configured_output, confirmed):
    metadata = CAPABILITIES.get(model)
    if metadata and urlparse(base_url).hostname == metadata.official_host:
        return ResolvedCapability(metadata.context_window, metadata.max_output_tokens,
                                  metadata.source, True)
    if not confirmed:
        raise AppError(
            "MODEL_CAPACITY_REQUIRED",
            "无法确认当前服务商与模型的上下文容量，请核对后明确确认设置值。",
        )
    context = int(configured_context)
    return ResolvedCapability(context, min(context, int(configured_output)), "user-confirmed", False)
