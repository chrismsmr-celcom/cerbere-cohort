"""Remote MCP transport authentication and request-scoped identity tests."""
import asyncio

import pytest

import mcp_server
from mcp_server import (
    CohortService,
    MCPAgentAuthMiddleware,
    MCPAuthorizationError,
    _request_principal,
)


def _scope(headers=()):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": list(headers),
        "client": ("127.0.0.1", 12345),
        "server": ("test", 443),
    }


async def _invoke(middleware, headers=()):
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

    await middleware(_scope(headers), receive, send)
    return messages, observed


def _status(messages):
    return next(message["status"] for message in messages if message["type"] == "http.response.start")


def test_remote_mcp_rejects_missing_bearer_token():
    middleware = MCPAgentAuthMiddleware(lambda *args: None, "org-a")
    messages, observed = asyncio.run(_invoke(middleware))
    assert _status(messages) == 401
    assert observed == []


def test_remote_mcp_rejects_organization_header_mismatch(monkeypatch):
    def should_not_authenticate(*args):
        raise AssertionError("credential lookup must not run for a mismatched organization")

    monkeypatch.setattr(mcp_server, "authenticate_agent_token", should_not_authenticate)
    middleware = MCPAgentAuthMiddleware(lambda *args: None, "org-a")
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
