"""Remote MCP transport authentication and request-scoped identity tests."""
import asyncio

import pytest

import mcp_server
from mcp_server import (
    CohortService,
    MCPAgentAuthMiddleware,
    MCPAuthorizationError,
    create_server,
    _request_principal,
)


def _scope(headers=(), path="/mcp", method="POST"):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 12345),
        "server": ("test", 443),
    }


async def _invoke(middleware, headers=(), path="/mcp", method="POST"):
    messages = []
    observed = []

    async def app(scope, receive, send):
        observed.append(_request_principal.get())
        await asyncio.sleep(0)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware.app = app

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await middleware(_scope(headers, path=path, method=method), receive, send)
    return messages, observed


def _status(messages):
    return next(message["status"] for message in messages if message["type"] == "http.response.start")


def test_remote_mcp_rejects_missing_bearer_token_with_oauth_discovery_challenge(monkeypatch):
    monkeypatch.setenv("COHORT_MCP_RESOURCE_METADATA_URL", "https://cohort.example/.well-known/oauth-protected-resource")
    middleware = MCPAgentAuthMiddleware(lambda *args: None, "org-a")
    messages, observed = asyncio.run(_invoke(middleware))
    assert _status(messages) == 401
    start = next(message for message in messages if message["type"] == "http.response.start")
    headers = dict(start["headers"])
    assert b'Bearer realm="cohort"' in headers[b"www-authenticate"]
    assert b'resource_metadata="https://cohort.example/.well-known/oauth-protected-resource"' in headers[b"www-authenticate"]
    assert observed == []


def test_remote_mcp_publishes_rfc9728_protected_resource_metadata(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    middleware = MCPAgentAuthMiddleware(lambda *args: None, "org-a")
    messages, observed = asyncio.run(
        _invoke(middleware, path="/.well-known/oauth-protected-resource", method="GET")
    )
    assert _status(messages) == 200
    body = next(message["body"] for message in messages if message["type"] == "http.response.body")
    metadata = __import__("json").loads(body)
    assert metadata["resource"] == "https://cerbere-cohort.onrender.com/mcp"
    assert metadata["authorization_servers"] == ["https://project.supabase.co/auth/v1"]
    assert "workspace:read" in metadata["scopes_supported"]
    assert observed == []


def test_remote_mcp_accepts_supabase_oauth_access_token(monkeypatch):
    principal = {
        "principal_type": "oauth_user",
        "organization_id": "org-a",
        "agent_id": "oauth-client",
        "scopes": ["workspace:read"],
    }
    calls = []
    monkeypatch.setattr(mcp_server, "authenticate_oauth_mcp_token", lambda token: calls.append(token) or principal)
    middleware = MCPAgentAuthMiddleware(lambda *args: None, "org-a")
    messages, observed = asyncio.run(_invoke(
        middleware, [(b"authorization", b"Bearer oauth.jwt.token")]
    ))
    assert _status(messages) == 200
    start = next(message for message in messages if message["type"] == "http.response.start")
    assert dict(start["headers"])[b"access-control-allow-origin"] == b"*"
    assert calls == ["oauth.jwt.token"]
    assert observed == [principal]


def test_remote_mcp_routes_options_without_agent_authentication():
    middleware = MCPAgentAuthMiddleware(lambda *args: None, "org-a")
    messages, observed = asyncio.run(_invoke(middleware, method="OPTIONS"))
    assert _status(messages) == 204
    assert observed == []


def test_remote_mcp_rejects_organization_header_mismatch(monkeypatch):
    monkeypatch.setattr(mcp_server, "authenticate_agent_token", lambda token, org: {
        "organization_id": "org-a",
        "agent_id": "agent-a",
        "scopes": ["workspace:read"],
    })
    middleware = MCPAgentAuthMiddleware(lambda *args: None)
    messages, observed = asyncio.run(_invoke(
        middleware,
        [
            (b"authorization", b"Bearer coh_valid"),
            (b"x-organization-id", b"org-b"),
        ],
    ))
    assert _status(messages) == 403
    assert observed == []


def test_remote_mcp_authenticates_each_request_and_resets_context(monkeypatch):
    principal = {
        "principal_type": "agent",
        "organization_id": "org-a",
        "agent_id": "agent-a",
        "scopes": ["workspace:read"],
    }
    calls = []

    def authenticate(token, organization_id):
        calls.append((token, organization_id))
        return principal

    monkeypatch.setattr(mcp_server, "authenticate_agent_token", authenticate)
    middleware = MCPAgentAuthMiddleware(lambda *args: None, "org-a")
    messages, observed = asyncio.run(_invoke(
        middleware, [(b"authorization", b"Bearer coh_valid")]
    ))

    assert _status(messages) == 200
    assert calls == [("coh_valid", "org-a")]
    assert observed == [principal]
    assert _request_principal.get() is None


def test_request_identity_and_scopes_are_enforced_per_tool(tmp_path):
    service = CohortService(tmp_path / "remote-mcp.db")
    read_only = _request_principal.set({
        "agent_id": "agent-a",
        "scopes": ["workspace:read", "workspace:write"],
    })
    try:
        assert service.get_state()["state"] == {}
        with pytest.raises(ValueError, match="identity bound"):
            service.create_branch("agent-b", "branch")
    finally:
        _request_principal.reset(read_only)

    read_only = _request_principal.set({
        "agent_id": "agent-a",
        "scopes": ["workspace:read"],
    })
    try:
        with pytest.raises(MCPAuthorizationError):
            service.create_branch("agent-a", "branch")
    finally:
        _request_principal.reset(read_only)

    write_access = _request_principal.set({
        "agent_id": "agent-b",
        "scopes": ["workspace:write", "workspace:read", "leases:write"],
    })
    try:
        result = service.create_branch("agent-b", "branch")
        assert result["agent_id"] == "agent-b"
    finally:
        _request_principal.reset(write_access)
        service.close()


def test_concurrent_requests_do_not_share_agent_identity(monkeypatch):
    async def run():
        async def app(scope, receive, send):
            await asyncio.sleep(0)
            principal = _request_principal.get()
            await send({
                "type": "http.response.start",
                "status": 200 if principal else 500,
                "headers": [],
            })
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def invoke(agent_id):
            middleware = MCPAgentAuthMiddleware(app, "org-a")
            monkeypatch.setattr(
                mcp_server,
                "authenticate_agent_token",
                lambda token, org: {
                    "agent_id": token,
                    "scopes": ["workspace:read"],
                },
            )
            messages = []

            async def send(message):
                messages.append(message)

            await middleware(
                _scope([(b"authorization", f"Bearer {agent_id}".encode())]),
                receive,
                send,
            )
            return _status(messages)

        return await asyncio.gather(invoke("agent-a"), invoke("agent-b"))

    assert asyncio.run(run()) == [200, 200]



def test_tenant_router_isolates_services_by_authenticated_organization(monkeypatch):
    created = []

    class FakeService:
        def __init__(self, organization_id, require_leases):
            self.organization_id = organization_id
            self.closed = False
            created.append(self)

        def get_state(self):
            return {"organization_id": self.organization_id}

        def close(self):
            self.closed = True

    monkeypatch.setattr(mcp_server, "CohortService", FakeService)
    monkeypatch.setenv("COHORT_MCP_MAX_TENANTS", "2")
    router = mcp_server.TenantServiceRouter("postgresql://unused")

    first = _request_principal.set({"organization_id": "org-a", "agent_id": "a", "scopes": ["workspace:read"]})
    try:
        assert router.get_state()["organization_id"] == "org-a"
    finally:
        _request_principal.reset(first)

    second = _request_principal.set({"organization_id": "org-b", "agent_id": "b", "scopes": ["workspace:read"]})
    try:
        assert router.get_state()["organization_id"] == "org-b"
    finally:
        _request_principal.reset(second)

    first_again = _request_principal.set({"organization_id": "org-a", "agent_id": "a", "scopes": ["workspace:read"]})
    try:
        assert router.get_state()["organization_id"] == "org-a"
    finally:
        _request_principal.reset(first_again)

    assert len(created) == 2
    assert created[0] is not created[1]
    router.close()
    assert all(service.closed for service in created)


def test_tenant_router_evicts_only_idle_tenant_services(monkeypatch):
    created = []

    class FakeService:
        def __init__(self, organization_id, require_leases):
            self.organization_id = organization_id
            self.closed = False
            created.append(self)

        def get_state(self):
            return {"organization_id": self.organization_id}

        def close(self):
            self.closed = True

    monkeypatch.setattr(mcp_server, "CohortService", FakeService)
    monkeypatch.setenv("COHORT_MCP_MAX_TENANTS", "1")
    router = mcp_server.TenantServiceRouter("postgresql://unused")

    for org_id in ("org-a", "org-b"):
        token = _request_principal.set({"organization_id": org_id, "agent_id": "agent", "scopes": ["workspace:read"]})
        try:
            assert router.get_state()["organization_id"] == org_id
        finally:
            _request_principal.reset(token)

    assert len(created) == 2
    assert created[0].closed is True
    assert created[1].closed is False
    router.close()



def test_fastmcp_exposes_asgi_streamable_http_app(tmp_path):
    service = CohortService(tmp_path / "streamable-http.db")
    try:
        app = create_server(service).streamable_http_app()
        assert callable(app)
    finally:
        service.close()
