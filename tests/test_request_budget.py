import pytest

from echotrace.util import AppError


def test_official_deepseek_capability_uses_verified_metadata():
    from echotrace.llm.capabilities import resolve_capability

    result = resolve_capability(
        "deepseek-flash", "https://api.deepseek.com/v1", 131072, 8192, False
    )

    assert result.context_window == 1048576
    assert result.max_output_tokens == 393216
    assert result.source == "deepseek-official-2026-10-25"
    assert result.verified is True


def test_official_model_name_on_third_party_host_requires_confirmation():
    from echotrace.llm.capabilities import resolve_capability

    with pytest.raises(AppError) as error:
        resolve_capability(
            "deepseek-flash", "https://gateway.example/v1", 262144, 65536, False
        )

    assert error.value.code == "MODEL_CAPACITY_REQUIRED"


def test_unknown_model_requires_explicit_capacity_confirmation():
    from echotrace.llm.capabilities import resolve_capability

    with pytest.raises(AppError) as error:
        resolve_capability("private-model", "http://127.0.0.1:9000/v1", 32768, 4096, False)

    assert error.value.code == "MODEL_CAPACITY_REQUIRED"


def test_confirmed_capacity_caps_output_to_context_and_configured_limit():
    from echotrace.llm.capabilities import resolve_capability

    result = resolve_capability(
        "private-model", "http://127.0.0.1:9000/v1", 32768, 65536, True
    )

    assert result.context_window == 32768
    assert result.max_output_tokens == 32768
    assert result.source == "user-confirmed"
    assert result.verified is False


def test_context_window_confirmation_defaults_false_and_persists(tmp_path):
    from echotrace.config import Config
    from echotrace.db import Database

    config = Config(Database(tmp_path / "data"))

    assert config.get()["llm"]["context_window_confirmed"] is False
    config.update({"llm": {"context_window_confirmed": True}})
    assert config.get()["llm"]["context_window_confirmed"] is True


def _settings(**changes):
    result = {
        "model": "private-model", "base_url": "http://127.0.0.1:9000/v1",
        "context_window": 32768, "context_window_confirmed": True,
        "max_tokens": 8192, "adapter": "none", "thinking": "low",
        "thinking_values": {"low": "low"}, "thinking_field": "reasoning_effort",
    }
    result.update(changes)
    return result


def _request(content="短消息", **changes):
    result = {
        "model": "private-model", "temperature": .2, "max_tokens": 8192,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": "系统规则"}, {"role": "user", "content": content}],
    }
    result.update(changes)
    return result


def test_fallback_token_count_is_deterministic_and_marked_approximate():
    from echotrace.llm.tokenizer import count_tokens

    first = count_tokens({"text": "中文 and ASCII"}, "private-model")
    second = count_tokens({"text": "中文 and ASCII"}, "private-model")

    assert first == second
    assert first.tokens > 0
    assert first.name == "conservative-character-estimator-v1"
    assert first.approximate is True


def test_request_budget_counts_complete_request_and_safety_margin():
    from echotrace.llm.budget import plan_request_budget

    result = plan_request_budget("chat_roleplay", _settings(), _request())

    assert result.context_capacity == 32768
    assert result.safety_margin == 1024
    assert result.request_tokens == result.static_request_tokens + result.content_tokens
    assert result.output_reserve == 2048
    assert result.remaining_input_tokens == result.available_content_tokens - result.content_tokens
    assert result.approximate is True


def test_prompt_schema_and_output_changes_affect_request_budget():
    from echotrace.llm.budget import plan_request_budget

    base = plan_request_budget("chat_roleplay", _settings(), _request())
    larger_prompt = plan_request_budget("chat_roleplay", _settings(), _request("系统 schema " * 300))
    larger_output = plan_request_budget("distill_persona", _settings(), _request())

    assert larger_prompt.request_tokens > base.request_tokens
    assert larger_prompt.remaining_input_tokens < base.remaining_input_tokens
    assert larger_output.output_reserve > base.output_reserve
    assert larger_output.available_content_tokens < base.available_content_tokens


def test_reasoning_budget_is_reserved_inside_output_capacity():
    from echotrace.llm.budget import plan_request_budget

    settings = _settings(adapter="budget", thinking="max", max_tokens=32768)
    request = _request(thinking={"type": "enabled", "budget_tokens": 16384})
    result = plan_request_budget("chat_roleplay", settings, request)

    assert result.reasoning_reserve == 16384
    assert result.output_reserve >= 17408


def test_static_request_overhead_fails_before_provider_call():
    from echotrace.llm.budget import plan_request_budget

    with pytest.raises(AppError) as error:
        plan_request_budget("chat_roleplay", _settings(context_window=16384), _request("规则" * 20000))

    assert error.value.code == "REQUEST_OVERHEAD_TOO_LARGE"


def test_request_budget_rejects_unconfirmed_unknown_capacity():
    from echotrace.llm.budget import plan_request_budget

    with pytest.raises(AppError) as error:
        plan_request_budget("chat_roleplay", _settings(context_window_confirmed=False), _request())

    assert error.value.code == "MODEL_CAPACITY_REQUIRED"
