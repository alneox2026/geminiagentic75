"""Usage metadata normalization and multi-model cost estimates."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from datetime import date, datetime, timezone
import os
from pathlib import Path
from typing import Any
import yaml


DEFAULT_MODEL_PRICING: dict[str, dict[str, Any]] = {
    "gemini-3.8-flash": {
        "rate_schedule": [
            {
                "effective_from": "2026-09-02",
                "pricing_version": "gemini-3.8-flash-usd-on-demand-intro-2026",
                "input_usd_per_million": 0.75,
                "input_cached_usd_per_million": 0.075,
                "input_audio_usd_per_million": 0.75,
                "output_usd_per_million": 3.75,
            },
            {
                "effective_from": "2027-01-01",
                "pricing_version": "gemini-3.8-flash-usd-on-demand-standard-2027",
                "input_usd_per_million": 1.5,
                "input_cached_usd_per_million": 0.15,
                "input_audio_usd_per_million": 1.5,
                "output_usd_per_million": 7.5,
            },
        ],
    },
}
DEFAULT_MODEL_NAME = os.getenv("DEFAULT_MODEL_NAME", "gemini-3.8-flash")
SUPPORTED_RUNTIME_MODEL_IDS = frozenset({"gemini-3.8-flash"})

def _normalize_model_id(model_name: str) -> str:
    # Permit Google's resource-name wrapper, but do not normalize model aliases,
    # release suffixes, or case variants into a billable model ID.
    cleaned = str(model_name).strip()
    if cleaned.startswith("models/"):
        cleaned = cleaned[7:]
    return cleaned


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _select_effective_rates(
    catalog_key: str,
    catalog_entry: dict[str, Any],
) -> dict[str, Any]:
    """Select the published rate schedule row effective on the current UTC date."""
    schedule = catalog_entry.get("rate_schedule")
    if not isinstance(schedule, list) or not schedule:
        return dict(catalog_entry)

    today = _utc_today()
    effective_rows: list[tuple[date, dict[str, Any]]] = []
    for row in schedule:
        if not isinstance(row, dict) or not row.get("effective_from"):
            raise ValueError(f"Invalid rate schedule for catalog entry '{catalog_key}'.")
        try:
            effective_from = date.fromisoformat(str(row["effective_from"]))
        except ValueError as exc:
            raise ValueError(
                f"Invalid effective_from date for catalog entry '{catalog_key}'."
            ) from exc
        if effective_from <= today:
            effective_rows.append((effective_from, row))

    if not effective_rows:
        raise ValueError(
            f"No published rate is effective yet for catalog entry '{catalog_key}'."
        )

    effective_from, selected = max(effective_rows, key=lambda item: item[0])
    required_fields = (
        "input_usd_per_million",
        "input_cached_usd_per_million",
        "input_audio_usd_per_million",
        "output_usd_per_million",
        "pricing_version",
    )
    if any(field not in selected for field in required_fields):
        raise ValueError(f"Incomplete effective rate row for '{catalog_key}'.")

    return {
        "pricing_version": str(selected["pricing_version"]),
        "pricing_effective_from": effective_from.isoformat(),
        "input_text_image_video": float(selected["input_usd_per_million"]),
        "input_cached": float(selected["input_cached_usd_per_million"]),
        "input_audio": float(selected["input_audio_usd_per_million"]),
        "output_including_thinking": float(selected["output_usd_per_million"]),
    }

_USAGE_KEYS = ("usage_metadata", "usageMetadata", "usage")


@lru_cache(maxsize=1)
def _load_models_from_yaml_catalog() -> dict[str, dict[str, Any]]:
    """Dynamically load model rates from billing YAML catalog files if available."""
    catalog: dict[str, dict[str, Any]] = {}
    custom_path = os.getenv("BILLING_CATALOG_PATH")
    if custom_path:
        search_paths = [custom_path]
    else:
        search_paths = [
            "config/billing.prod.yaml",
            "config/billing.test.yaml",
            "/app/config/billing.prod.yaml",
            "/app/config/billing.test.yaml",
        ]
    for path_str in search_paths:
        if not path_str:
            continue
        try:
            path = Path(path_str).resolve()
            if path.exists() and path.is_file():
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and "models" in raw and isinstance(raw["models"], dict):
                    for model_id, rates in raw["models"].items():
                        if isinstance(rates, dict):
                            cleaned_id = str(model_id).strip().lower()
                            if "rate_schedule" in rates:
                                catalog[cleaned_id] = {
                                    "rate_schedule": rates["rate_schedule"]
                                }
                                continue
                            default_rates = DEFAULT_MODEL_PRICING.get(cleaned_id, {})
                            default_row = _select_effective_rates(cleaned_id, default_rates)
                            input_usd = float(
                                rates.get(
                                    "input_usd_per_million",
                                    default_row.get("input_text_image_video", 0.75),
                                )
                            )
                            cached_usd = float(
                                rates.get(
                                    "input_cached_usd_per_million",
                                    default_row.get("input_cached", round(input_usd * 0.1, 6)),
                                )
                            )
                            output_usd = float(
                                rates.get(
                                    "output_usd_per_million",
                                    default_row.get("output_including_thinking", 3.75),
                                )
                            )
                            audio_usd = float(
                                rates.get(
                                    "input_audio_usd_per_million",
                                    default_row.get("input_audio", input_usd),
                                )
                            )
                            catalog[cleaned_id] = {
                                "pricing_version": rates.get("pricing_version", default_row.get(
                                    "pricing_version", f"{cleaned_id}-usd-on-demand-catalog"
                                )),
                                "input_text_image_video": input_usd,
                                "input_cached": cached_usd,
                                "input_audio": audio_usd,
                                "output_including_thinking": output_usd,
                            }
                    if catalog:
                        break
        except Exception:
            pass
    return catalog


def resolve_model_pricing(
    raw_model: str | None = None,
    pricing_model: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolves model name and pricing catalog rates from YAML catalog and presets."""
    active_catalog = dict(DEFAULT_MODEL_PRICING)
    yaml_catalog = _load_models_from_yaml_catalog()
    active_catalog.update(yaml_catalog)

    if not raw_model:
        env_default = os.getenv("DEFAULT_MODEL_NAME")
        if env_default:
            cleaned_env = _normalize_model_id(env_default)
            if cleaned_env in active_catalog:
                base_model = cleaned_env
            else:
                raise ValueError(
                    f"DEFAULT_MODEL_NAME '{env_default}' is not present in the pricing catalog."
                )
        elif yaml_catalog and DEFAULT_MODEL_NAME not in yaml_catalog:
            base_model = next(iter(yaml_catalog.keys()))
        else:
            base_model = DEFAULT_MODEL_NAME
    else:
        base_model = _normalize_model_id(raw_model)

    if base_model.endswith("-regional"):
        raise ValueError(
            f"Model '{base_model}' is not present in billing catalog as a supported runtime model ID."
        )
    if base_model not in SUPPORTED_RUNTIME_MODEL_IDS:
        raise ValueError(
            f"Model '{base_model}' is not present in billing catalog as a supported runtime model ID."
        )

    # 1. Reject unknown explicit pricing_model instead of silently falling back
    if pricing_model:
        canonical_pricing = _normalize_model_id(pricing_model)
        if canonical_pricing not in active_catalog:
            raise ValueError(
                f"Explicit pricing_model '{pricing_model}' is not present in pricing catalog."
            )
        if canonical_pricing != base_model:
            raise ValueError(
                f"Explicit pricing_model '{pricing_model}' does not match runtime model "
                f"'{base_model}'."
            )
        pricing_data = _select_effective_rates(
            canonical_pricing, active_catalog[canonical_pricing]
        )
        pricing_data["catalog_key"] = canonical_pricing
        return base_model, pricing_data

    # 2. Exact match in active_catalog
    if base_model in active_catalog:
        pricing_data = _select_effective_rates(base_model, active_catalog[base_model])
        pricing_data["catalog_key"] = base_model
        return base_model, pricing_data

    # 3. Fail closed for uncataloged models
    raise ValueError(
        f"Model '{base_model}' is not present in billing catalog. "
        f"An explicit catalog entry is required."
    )


def extract_usage_metadata(event: dict[str, Any]) -> dict[str, Any] | None:
    """Find ADK/Gemini usage metadata in common event envelope shapes."""

    def walk(value: Any, depth: int = 0) -> dict[str, Any] | None:
        if depth > 8:
            return None
        if isinstance(value, dict):
            for key in _USAGE_KEYS:
                usage = value.get(key)
                if isinstance(usage, dict):
                    return usage
            for nested_value in value.values():
                usage = walk(nested_value, depth + 1)
                if usage is not None:
                    return usage
        elif isinstance(value, list):
            for item in value:
                usage = walk(item, depth + 1)
                if usage is not None:
                    return usage
        return None

    return walk(event)


def normalize_usage_metadata(
    usage_metadata: dict[str, Any],
    model_name: str | None = None,
    pricing_model: str | None = None,
) -> dict[str, Any]:
    """Preserve raw usage fields while adding normalized counts and dynamic cost fields."""

    usage = deepcopy(usage_metadata)
    token_counts = _token_counts(usage_metadata)
    if token_counts:
        usage["token_counts"] = token_counts

    prompt_token_count = token_counts.get("prompt_token_count")
    candidates_token_count = token_counts.get("candidates_token_count")
    thoughts_token_count = token_counts.get("thoughts_token_count", 0)
    total_token_count = token_counts.get("total_token_count")

    input_text_image_video_tokens, input_audio_tokens = _input_token_split(
        usage_metadata,
        prompt_token_count,
    )
    output_tokens = _output_token_count(
        prompt_token_count=prompt_token_count,
        candidates_token_count=candidates_token_count,
        thoughts_token_count=thoughts_token_count,
        total_token_count=total_token_count,
    )

    if (
        input_text_image_video_tokens is None
        and input_audio_tokens is None
        and output_tokens is None
    ):
        return usage

    model_candidate = (
        model_name
        or usage_metadata.get("pricing_model")
        or usage_metadata.get("model_version")
        or usage_metadata.get("model_name")
        or usage_metadata.get("model")
    )
    pricing_model_candidate = (
        pricing_model
        or (
            usage_metadata.get("pricing_model")
            if usage_metadata.get("pricing_model") != model_candidate
            else None
        )
    )
    resolved_model, pricing_info = resolve_model_pricing(
        raw_model=model_candidate,
        pricing_model=pricing_model_candidate,
    )

    input_rate = pricing_info["input_text_image_video"]
    cached_rate = pricing_info.get("input_cached", round(input_rate * 0.1, 6))
    audio_rate = pricing_info["input_audio"]
    output_rate = pricing_info["output_including_thinking"]
    pricing_version = pricing_info["pricing_version"]
    catalog_key = pricing_info.get("catalog_key", resolved_model)

    cached_token_count = token_counts.get("cached_content_token_count", 0)
    total_input = input_text_image_video_tokens or 0
    cached_input = min(cached_token_count, total_input)
    uncached_input = max(total_input - cached_input, 0)
    total_audio = input_audio_tokens or 0
    total_output = output_tokens or 0

    billable_tokens = {
        "input_text_image_video": total_input,
        "input_audio": total_audio,
        "output_including_thinking": total_output,
    }
    if cached_input > 0:
        billable_tokens["input_cached"] = cached_input
        billable_tokens["input_uncached"] = uncached_input

    usage["pricing_model"] = resolved_model
    usage["pricing_catalog_key"] = catalog_key
    usage["pricing_version"] = pricing_version
    if pricing_info.get("pricing_effective_from"):
        usage["pricing_effective_from"] = pricing_info["pricing_effective_from"]
    usage["pricing_unit"] = "usd_per_1m_tokens"
    usage["endpoint_location"] = "global"
    usage["endpoint_type"] = "global"
    usage["pricing"] = {
        "input_text_image_video": input_rate,
        "input_cached": cached_rate,
        "input_audio": audio_rate,
        "output_including_thinking": output_rate,
    }
    usage["billable_tokens"] = billable_tokens
    if cached_input > 0:
        usage["cached_input_tokens"] = cached_input
        usage["uncached_input_tokens"] = uncached_input

    uncached_cost = uncached_input * input_rate / 1_000_000
    cached_cost = cached_input * cached_rate / 1_000_000
    audio_cost = total_audio * audio_rate / 1_000_000
    output_cost = total_output * output_rate / 1_000_000

    usage["estimated_cost_usd"] = _round_usd(
        uncached_cost + cached_cost + audio_cost + output_cost
    )
    usage["estimated_cost_breakdown_usd"] = {
        "input_text_image_video": _round_usd(uncached_cost),
        "input_cached": _round_usd(cached_cost),
        "input_audio": _round_usd(audio_cost),
        "output_including_thinking": _round_usd(output_cost),
    }
    return usage


def _token_counts(usage_metadata: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for normalized_key, key_options in {
        "prompt_token_count": (
            "prompt_token_count",
            "promptTokenCount",
            "total_input_tokens",
            "totalInputTokens",
        ),
        "candidates_token_count": (
            "candidates_token_count",
            "candidatesTokenCount",
            "total_output_tokens",
            "totalOutputTokens",
        ),
        "thoughts_token_count": (
            "thoughts_token_count",
            "thoughtsTokenCount",
            "total_thought_tokens",
            "totalThoughtTokens",
        ),
        "total_token_count": (
            "total_token_count",
            "totalTokenCount",
            "total_tokens",
            "totalTokens",
        ),
        "cached_content_token_count": (
            "cached_content_token_count",
            "cachedContentTokenCount",
            "total_cached_tokens",
            "totalCachedTokens",
        ),
        "tool_use_prompt_token_count": (
            "tool_use_prompt_token_count",
            "toolUsePromptTokenCount",
        ),
    }.items():
        value = _first_int(usage_metadata, key_options)
        if value is not None:
            counts[normalized_key] = value
    return counts


def _input_token_split(
    usage_metadata: dict[str, Any],
    prompt_token_count: int | None,
) -> tuple[int | None, int | None]:
    if prompt_token_count is None:
        return None, None

    audio_tokens = 0
    non_audio_tokens = 0
    details_found = False
    for detail in _prompt_token_details(usage_metadata):
        modality = str(
            detail.get("modality")
            or detail.get("Modality")
            or detail.get("type")
            or ""
        ).upper()
        token_count = _first_int(detail, ("token_count", "tokenCount"))
        if token_count is None:
            continue
        details_found = True
        if modality == "AUDIO":
            audio_tokens += token_count
        else:
            non_audio_tokens += token_count

    if not details_found:
        return prompt_token_count, 0

    residual_tokens = max(prompt_token_count - audio_tokens - non_audio_tokens, 0)
    return non_audio_tokens + residual_tokens, audio_tokens


def _prompt_token_details(usage_metadata: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("prompt_tokens_details", "promptTokensDetails"):
        value = usage_metadata.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _output_token_count(
    *,
    prompt_token_count: int | None,
    candidates_token_count: int | None,
    thoughts_token_count: int | None,
    total_token_count: int | None,
) -> int | None:
    if candidates_token_count is not None or thoughts_token_count:
        return (candidates_token_count or 0) + (thoughts_token_count or 0)
    if total_token_count is not None and prompt_token_count is not None:
        return max(total_token_count - prompt_token_count, 0)
    return None


def _first_int(source: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(value, 0)
        if isinstance(value, float) and value.is_integer():
            return max(int(value), 0)
        if isinstance(value, str):
            try:
                parsed = int(value)
            except ValueError:
                continue
            return max(parsed, 0)
    return None


def _round_usd(value: float) -> float:
    return round(value, 9)
