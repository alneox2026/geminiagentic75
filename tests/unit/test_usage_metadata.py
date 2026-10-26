from datetime import date

import services.agent_gateway_v3.app.services.usage_metadata as usage_metadata_module
from services.agent_gateway_v3.app.services.usage_metadata import (
    _load_models_from_yaml_catalog,
    extract_usage_metadata,
    normalize_usage_metadata,
    resolve_model_pricing,
)


def test_normalize_usage_metadata_prices_gemini_flash_text_tokens() -> None:
    usage = normalize_usage_metadata(
        {
            "prompt_token_count": 1000,
            "candidates_token_count": 200,
            "thoughts_token_count": 50,
            "total_token_count": 1250,
        }
    )

    assert usage["pricing_model"] == "gemini-3.8-flash"
    assert usage["token_counts"] == {
        "prompt_token_count": 1000,
        "candidates_token_count": 200,
        "thoughts_token_count": 50,
        "total_token_count": 1250,
    }
    assert usage["billable_tokens"] == {
        "input_text_image_video": 1000,
        "input_audio": 0,
        "output_including_thinking": 250,
    }
    assert usage["estimated_cost_usd"] == 0.0016875
    assert usage["estimated_cost_breakdown_usd"] == {
        "input_text_image_video": 0.00075,
        "input_cached": 0.0,
        "input_audio": 0.0,
        "output_including_thinking": 0.0009375,
    }


def test_normalize_usage_metadata_splits_audio_prompt_tokens() -> None:
    usage = normalize_usage_metadata(
        {
            "promptTokenCount": 100,
            "candidatesTokenCount": 10,
            "promptTokensDetails": [
                {"modality": "TEXT", "tokenCount": 40},
                {"modality": "AUDIO", "tokenCount": 60},
            ],
        }
    )

    assert usage["token_counts"]["prompt_token_count"] == 100
    assert usage["billable_tokens"] == {
        "input_text_image_video": 40,
        "input_audio": 60,
        "output_including_thinking": 10,
    }
    # 40 * 0.75 / 1M = 0.00003; 60 * 0.75 / 1M = 0.000045; 10 * 3.75 / 1M = 0.0000375; total = 0.0001125
    assert usage["estimated_cost_usd"] == 0.0001125


def test_normalize_usage_metadata_prices_cached_context_tokens() -> None:
    usage = normalize_usage_metadata(
        {
            "prompt_token_count": 10000,
            "cached_content_token_count": 8000,
            "candidates_token_count": 500,
            "thoughts_token_count": 100,
            "total_token_count": 10600,
        },
        model_name="gemini-3.8-flash",
    )

    assert usage["pricing_model"] == "gemini-3.8-flash"
    assert usage["cached_input_tokens"] == 8000
    assert usage["uncached_input_tokens"] == 2000
    assert usage["billable_tokens"] == {
        "input_text_image_video": 10000,
        "input_cached": 8000,
        "input_uncached": 2000,
        "input_audio": 0,
        "output_including_thinking": 600,
    }
    assert usage["pricing"]["input_text_image_video"] == 0.75
    assert usage["pricing"]["input_cached"] == 0.075
    assert usage["pricing"]["output_including_thinking"] == 3.75
    # uncached = 2000 * 0.75 / 1M = 0.0015
    # cached = 8000 * 0.075 / 1M = 0.0006
    # output = 600 * 3.75 / 1M = 0.00225
    # total = 0.00435
    assert usage["estimated_cost_usd"] == 0.00435
    assert usage["estimated_cost_breakdown_usd"] == {
        "input_text_image_video": 0.0015,
        "input_cached": 0.0006,
        "input_audio": 0.0,
        "output_including_thinking": 0.00225,
    }


def test_extract_usage_metadata_finds_nested_agent_runtime_shape() -> None:
    event = {
        "payload": {
            "event": {
                "usageMetadata": {
                    "promptTokenCount": 5,
                    "candidatesTokenCount": 3,
                }
            }
        }
    }

    assert extract_usage_metadata(event) == {
        "promptTokenCount": 5,
        "candidatesTokenCount": 3,
    }


def test_normalize_usage_metadata_does_not_price_total_only_usage() -> None:
    usage = normalize_usage_metadata({"total_token_count": 21})

    assert usage["total_token_count"] == 21
    assert usage["token_counts"] == {"total_token_count": 21}
    assert "estimated_cost_usd" not in usage
    assert "billable_tokens" not in usage


def test_normalize_usage_metadata_rejects_models_earlier_than_gemini_3_8() -> None:
    import pytest

    for retired_model in (
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
    ):
        with pytest.raises(ValueError, match="not present in billing catalog"):
            normalize_usage_metadata(
                {
                    "prompt_token_count": 100,
                    "candidates_token_count": 50,
                },
                model_name=retired_model,
            )


def test_normalize_usage_metadata_prices_gemini_3_8_flash_tokens() -> None:
    usage = normalize_usage_metadata(
        {
            "prompt_token_count": 280,
            "candidates_token_count": 50,
            "thoughts_token_count": 29,
            "total_token_count": 359,
        },
        model_name="gemini-3.8-flash",
    )

    assert usage["pricing_model"] == "gemini-3.8-flash"
    assert usage["pricing"]["input_text_image_video"] == 0.75
    assert usage["pricing"]["output_including_thinking"] == 3.75
    assert usage["billable_tokens"]["input_text_image_video"] == 280
    assert usage["billable_tokens"]["output_including_thinking"] == 79
    assert usage["estimated_cost_usd"] == 0.00050625


def test_resolve_model_pricing_respects_env_default(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_MODEL_NAME", "gemini-3.8-flash")
    model, pricing = resolve_model_pricing(None)
    assert model == "gemini-3.8-flash"
    assert pricing["input_text_image_video"] == 0.75
    assert pricing["output_including_thinking"] == 3.75


def test_resolve_model_pricing_rejects_unlisted_env_default(monkeypatch) -> None:
    import pytest

    monkeypatch.setenv("DEFAULT_MODEL_NAME", "gemini-3.8-flash-preview")
    with pytest.raises(ValueError, match="DEFAULT_MODEL_NAME"):
        resolve_model_pricing(None)


def test_resolve_model_pricing_uses_catalog_when_default_absent(tmp_path, monkeypatch) -> None:
    catalog_path = tmp_path / "billing.yaml"
    catalog_path.write_text(
        """
models:
  gemini-3.8-flash:
    input_usd_per_million: 0.75
    output_usd_per_million: 3.75
""",
        encoding="utf-8",
    )
    _load_models_from_yaml_catalog.cache_clear()
    monkeypatch.setenv("BILLING_CATALOG_PATH", str(catalog_path))
    model, pricing = resolve_model_pricing(None)
    _load_models_from_yaml_catalog.cache_clear()
    assert model == "gemini-3.8-flash"
    assert pricing["input_text_image_video"] == 0.75


def test_normalize_usage_metadata_interactions_api_schema() -> None:
    raw_usage = {
        "total_input_tokens": 1000,
        "total_output_tokens": 200,
        "total_thought_tokens": 150,
        "total_tokens": 1350,
    }
    usage = normalize_usage_metadata(raw_usage, model_name="gemini-3.8-flash")
    assert usage["pricing_model"] == "gemini-3.8-flash"
    assert usage["token_counts"]["prompt_token_count"] == 1000
    assert usage["token_counts"]["candidates_token_count"] == 200
    assert usage["token_counts"]["thoughts_token_count"] == 150
    assert usage["token_counts"]["total_token_count"] == 1350
    assert usage["billable_tokens"]["input_text_image_video"] == 1000
    assert usage["billable_tokens"]["output_including_thinking"] == 350
    # 1000 * 0.75/1M + 350 * 3.75/1M = 0.00075 + 0.0013125 = 0.0020625
    assert usage["estimated_cost_usd"] == 0.0020625


def test_normalize_usage_metadata_interactions_api_with_cache() -> None:
    raw_usage = {
        "total_input_tokens": 1000,
        "total_cached_tokens": 600,
        "total_output_tokens": 200,
        "total_thought_tokens": 150,
        "total_tokens": 1350,
    }
    usage = normalize_usage_metadata(raw_usage, model_name="gemini-3.8-flash")
    assert usage["pricing_model"] == "gemini-3.8-flash"
    assert usage["token_counts"]["prompt_token_count"] == 1000
    assert usage["token_counts"]["cached_content_token_count"] == 600
    assert usage["cached_input_tokens"] == 600
    assert usage["uncached_input_tokens"] == 400
    assert usage["billable_tokens"]["input_text_image_video"] == 1000
    assert usage["billable_tokens"]["input_cached"] == 600
    assert usage["billable_tokens"]["input_uncached"] == 400
    assert usage["billable_tokens"]["output_including_thinking"] == 350
    # uncached = 400 * 0.75 / 1M = 0.000300
    # cached = 600 * 0.075 / 1M = 0.000045
    # output = 350 * 3.75 / 1M = 0.0013125
    # total = 0.0016575
    assert usage["estimated_cost_usd"] == 0.0016575
    assert usage["pricing_catalog_key"] == "gemini-3.8-flash"
    assert usage["endpoint_type"] == "global"


def test_resolve_model_pricing_rejects_unknown_explicit_pricing_model() -> None:
    import pytest

    with pytest.raises(ValueError, match="not present in pricing catalog"):
        resolve_model_pricing("gemini-3.8-flash", pricing_model="nonexistent-model")


def test_resolve_model_pricing_rejects_models_earlier_than_gemini_3_8() -> None:
    import pytest

    for earlier_model in (
        "gemini-3.7-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-1.5-flash",
    ):
        with pytest.raises(ValueError, match="not present in billing catalog"):
            resolve_model_pricing(earlier_model)


def test_resolve_model_pricing_accepts_only_canonical_model_id() -> None:
    import pytest

    model, pricing = resolve_model_pricing("gemini-3.8-flash")
    assert model == "gemini-3.8-flash"
    assert pricing["catalog_key"] == "gemini-3.8-flash"
    assert pricing["input_text_image_video"] == 0.75

    wrapped_model, _ = resolve_model_pricing("models/gemini-3.8-flash")
    assert wrapped_model == "gemini-3.8-flash"
    for unsupported_suffix in ("001", "preview", "regional"):
        with pytest.raises(ValueError, match="not present in billing catalog"):
            resolve_model_pricing(f"gemini-3.8-flash-{unsupported_suffix}")


def test_resolve_model_pricing_fails_closed_for_unlisted_model() -> None:
    import pytest

    with pytest.raises(ValueError, match="not present in billing catalog"):
        resolve_model_pricing("unlisted-model-xyz")


def test_resolve_model_pricing_rejects_future_model_even_if_catalogued(monkeypatch) -> None:
    import pytest

    monkeypatch.setattr(
        usage_metadata_module,
        "_load_models_from_yaml_catalog",
        lambda: {
            "gemini-3.9-flash": {
                "input_text_image_video": 0.75,
                "input_cached": 0.075,
                "input_audio": 0.75,
                "output_including_thinking": 3.75,
                "pricing_version": "test-only",
            }
        },
    )
    with pytest.raises(ValueError, match="supported runtime model ID"):
        resolve_model_pricing("gemini-3.9-flash")


def test_resolve_model_pricing_rejects_case_variant() -> None:
    import pytest

    with pytest.raises(ValueError, match="supported runtime model ID"):
        resolve_model_pricing("Gemini-3.8-Flash")


def test_resolve_model_pricing_rejects_unlisted_model_version_suffix() -> None:
    import pytest

    with pytest.raises(ValueError, match="not present in billing catalog"):
        resolve_model_pricing("gemini-2.5-flash-002")


def test_resolve_model_pricing_switches_to_2027_rates(monkeypatch) -> None:
    monkeypatch.setattr(usage_metadata_module, "_utc_today", lambda: date(2027, 1, 1))

    _, pricing = resolve_model_pricing("gemini-3.8-flash")

    assert pricing["input_text_image_video"] == 1.5
    assert pricing["input_cached"] == 0.15
    assert pricing["input_audio"] == 1.5
    assert pricing["output_including_thinking"] == 7.5
    assert pricing["pricing_version"] == "gemini-3.8-flash-usd-on-demand-standard-2027"
    assert pricing["pricing_effective_from"] == "2027-01-01"




