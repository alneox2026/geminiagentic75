from datetime import datetime, timezone
import time

from common.schemas import AgentConfig, ChatRequest
from services.agent_gateway_v3.app.services.request_context import RequestContext
from services.agent_gateway_v3.app.services.turn_event_builder import (
    build_turn_completed_event,
)


def test_turn_event_builder_attaches_server_derived_shadow_meter() -> None:
    agent = AgentConfig(
        agent_id="maxima",
        backend="agent_runtime",
        resource_name="projects/test/locations/us-central1/reasoningEngines/123",
        region="us-central1",
    )
    context = RequestContext(
        request_id="req-server-generated",
        turn_id="turn-1",
        started_at=datetime.now(timezone.utc),
        agent_id="maxima",
        started_monotonic_ns=time.perf_counter_ns() - 50_000_000,
    )
    event = build_turn_completed_event(
        request_context=context,
        agent_config=agent,
        user_id="verified-user-1",
        payload=ChatRequest(message="hello"),
        thread_id="thread-1",
        session_id="session-1",
        assistant_message="hi 🌍",
        billing_metadata={"billing_subject_id": "server-payer-1"},
    )

    assert event.user_id == "verified-user-1"
    assert event.infrastructure_metering is not None
    assert event.infrastructure_metering.mode == "shadow"
    assert event.infrastructure_metering.billing_subject_id == "server-payer-1"
    assert event.infrastructure_metering.root_request_id == "req-server-generated"
    assert event.infrastructure_metering.agent_backend == "agent_runtime"
    assert event.infrastructure_metering.project_id
    assert event.infrastructure_metering.gateway_region
    assert event.infrastructure_metering.gateway_wall_duration_ms >= 50
    assert event.infrastructure_metering.assistant_content_bytes == len(
        "hi 🌍".encode("utf-8")
    )
    assert "provider" not in event.metadata


def test_turn_event_builder_keeps_gemini_interaction_id_in_private_provider_metadata():
    agent = AgentConfig(agent_id="maxima", backend="gemini_managed")
    context = RequestContext(
        request_id="req-1",
        turn_id="turn-1",
        started_at=datetime.now(timezone.utc),
        agent_id="maxima",
        started_monotonic_ns=time.perf_counter_ns(),
    )

    event = build_turn_completed_event(
        request_context=context,
        agent_config=agent,
        user_id="user-1",
        payload=ChatRequest(message="hello"),
        thread_id="thread-1",
        session_id="session-1",
        assistant_message="hi",
        provider_interaction_id="interaction-123",
    )

    assert event.metadata["provider"] == {
        "name": "gemini_interactions",
        "model": "gemini-3.8-flash",
        "interaction_id": "interaction-123",
    }
