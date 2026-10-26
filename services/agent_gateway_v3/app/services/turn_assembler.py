"""Helpers to assemble streamed tokens into a final turn result."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from services.agent_gateway_v3.app.services.usage_metadata import (
    extract_usage_metadata,
    normalize_usage_metadata,
)


@dataclass
class TurnAssembler:
    text_fragments: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    raw_events: list[dict[str, Any]] = field(default_factory=list)
    model_name: str | None = None
    provider_interaction_id: str | None = None
    _accumulated_text: str = field(default="", init=False, repr=False)

    def add_text(self, value: str) -> str:
        if not value:
            return ""

        current_text = self._accumulated_text
        if not current_text and self.text_fragments:
            current_text = "".join(self.text_fragments)
            self._accumulated_text = current_text

        if value.startswith(current_text):
            delta = value[len(current_text) :]
            if delta:
                self.text_fragments.append(delta)
                self._accumulated_text += delta
            return delta

        overlap = self._suffix_prefix_overlap(current_text, value)
        delta = value[overlap:]
        if delta:
            self.text_fragments.append(delta)
            self._accumulated_text += delta
        return delta

    def add_event(self, event: dict[str, Any]) -> None:
        self.raw_events.append(event)
        provider_interaction_id = event.get("_provider_interaction_id")
        if isinstance(provider_interaction_id, str) and provider_interaction_id.strip():
            self.provider_interaction_id = provider_interaction_id.strip()
        model = (
            event.get("model_version")
            or event.get("model_name")
            or event.get("model")
            or (event.get("agent_config", {}).get("model") if isinstance(event.get("agent_config"), dict) else None)
            or (event.get("interaction", {}).get("agent_config", {}).get("model") if isinstance(event.get("interaction"), dict) else None)
        )
        if model and not self.model_name:
            self.model_name = str(model).strip()
        usage = extract_usage_metadata(event)
        if usage is not None:
            self.usage = normalize_usage_metadata(usage, model_name=self.model_name)

    def set_usage(self, usage: dict[str, Any]) -> None:
        self.usage = normalize_usage_metadata(usage, model_name=self.model_name)

    def reply_text(self) -> str:
        if self._accumulated_text:
            return self._accumulated_text.strip()
        return "".join(self.text_fragments).strip()

    def _suffix_prefix_overlap(self, current_text: str, next_value: str) -> int:
        max_overlap = min(len(current_text), len(next_value))
        for overlap in range(max_overlap, 0, -1):
            if current_text[-overlap:] == next_value[:overlap]:
                return overlap
        return 0
