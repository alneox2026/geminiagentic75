from services.agent_persistence_worker_v3.app.services.gemini_interaction_usage_hydrator import (
    GeminiInteractionUsageHydrator,
)


class FakeResponse:
    status_code = 200

    def json(self):
        return {
            "id": "interaction/123",
            "usage": {"total_input_tokens": 12, "total_output_tokens": 4},
        }


class FakeHttpClient:
    def __init__(self):
        self.calls = []

    def get(self, url, headers):
        self.calls.append((url, headers))
        return FakeResponse()


def test_hydrator_retrieves_usage_from_saved_interaction(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    client = FakeHttpClient()
    hydrator = GeminiInteractionUsageHydrator(http_client=client)

    usage = hydrator.retrieve_usage("interaction/123")

    assert usage == {"total_input_tokens": 12, "total_output_tokens": 4}
    assert client.calls == [
        (
            "https://generativelanguage.googleapis.com/v1/interactions/interaction%2F123",
            {"x-goog-api-key": "test-api-key", "Accept": "application/json"},
        )
    ]


def test_hydrator_falls_back_to_beta_endpoint_for_existing_beta_interactions(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")

    class BetaResponse(FakeResponse):
        def __init__(self, status_code):
            self.status_code = status_code

    class BetaHttpClient:
        def __init__(self):
            self.calls = []

        def get(self, url, headers):
            self.calls.append(url)
            return BetaResponse(404 if len(self.calls) == 1 else 200)

    client = BetaHttpClient()
    hydrator = GeminiInteractionUsageHydrator(http_client=client)

    usage = hydrator.retrieve_usage("interaction-123")

    assert usage == {"total_input_tokens": 12, "total_output_tokens": 4}
    assert client.calls == [
        "https://generativelanguage.googleapis.com/v1/interactions/interaction-123",
        "https://generativelanguage.googleapis.com/v1beta/interactions/interaction-123",
    ]
