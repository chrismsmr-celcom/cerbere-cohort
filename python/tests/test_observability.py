import json

import pytest

from cerbere_cohort import observability


def test_normalizes_generic_otlp_endpoint():
    assert observability.normalize_otlp_endpoint("https://collector.example.com") == "https://collector.example.com/v1/logs"
    assert observability.normalize_otlp_endpoint("https://collector.example.com/otlp/v1/logs") == "https://collector.example.com/otlp/v1/logs"


@pytest.mark.parametrize("endpoint", [
    "http://collector.example.com",
    "https://user:pass@collector.example.com",
    "https://collector.example.com/v1/logs?token=x",
])
def test_rejects_unsafe_otlp_endpoints(endpoint):
    with pytest.raises(ValueError):
        observability.normalize_otlp_endpoint(endpoint)


def test_otlp_payload_excludes_workspace_values_and_secrets():
    event = {
        "event_id": "evt-1",
        "timestamp": "2026-11-22T12:00:00+00:00",
        "agent_id": "agent-a",
        "action": "workspace.commit",
        "payload": {
            "branch_name": "feature",
            "changes": {"customer_record": {"email": "private@example.com"}},
            "prompt": "do not export this",
            "api_key": "secret",
        },
        "metadata": {"actor": {"type": "agent", "id": "agent-a"}},
    }
    payload = observability.build_otlp_payload([event])
    encoded = json.dumps(payload)
    assert "workspace.commit" in encoded
    assert "feature" in encoded
    assert "private@example.com" not in encoded
    assert "do not export this" not in encoded
    assert "secret" not in encoded
    assert "customer_record" not in encoded
    assert "resource_key" not in encoded


def test_headers_are_authenticated_encrypted_and_round_trip(monkeypatch):
    monkeypatch.setenv("COHORT_EXPORT_ENCRYPTION_KEY", "test-key-material")
    encrypted = observability.encrypt_headers({"dd-api-key": "top-secret"})
    assert b"top-secret" not in encrypted
    assert observability.decrypt_headers(encrypted) == {"dd-api-key": "top-secret"}


def test_headers_reject_newlines_and_invalid_names():
    with pytest.raises(ValueError):
        observability.validate_headers({"Authorization": "Bearer ok\r\nX-Evil: yes"})
    with pytest.raises(ValueError):
        observability.validate_headers({"Bad Header": "value"})


def test_send_uses_otlp_http_and_custom_auth_header(monkeypatch):
    import socket

    captured = {}
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
    class Opener:
        def open(self, request, timeout):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data)
            captured["timeout"] = timeout
            return Response()
    monkeypatch.setattr(observability.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))
    ])
    monkeypatch.setattr(observability.urllib.request, "build_opener", lambda *args: Opener())
    observability.send_otlp_logs(
        "https://otlp.datadoghq.com", {"dd-api-key": "test-secret"},
        [{"event_id": "evt-2", "timestamp": "2026-11-22T12:00:00Z",
          "agent_id": "agent-a", "action": "workspace.commit", "payload": {}}],
    )
    assert captured["url"] == "https://otlp.datadoghq.com/v1/logs"
    assert captured["headers"]["Dd-api-key"] == "test-secret"
    assert captured["payload"]["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["severityText"] == "INFO"
    assert captured["timeout"] == 5.0


def test_send_rejects_private_ip_destinations():
    with pytest.raises(ValueError, match="public IP"):
        observability.send_otlp_logs(
            "https://127.0.0.1/v1/logs", {}, []
        )

def test_headers_cannot_override_transport_headers():
    with pytest.raises(ValueError, match="cannot be overridden"):
        observability.validate_headers({"Host": "attacker.example"})
    with pytest.raises(ValueError, match="cannot be overridden"):
        observability.validate_headers({"Authorization": "Bearer a", "authorization": "Bearer b"})
