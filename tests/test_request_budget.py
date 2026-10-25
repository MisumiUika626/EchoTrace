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
