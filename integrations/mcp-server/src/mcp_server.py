"""Cerbere Cohort MCP server.

The default transport is Streamable HTTP so multiple MCP clients can share one
database-backed workspace. Bind to loopback unless an authenticated proxy is used.
"""
from __future__ import annotations

import argparse
import contextvars
import functools
import json
import os
import math
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from cerbere_cohort.errors import CerbereError
from cerbere_cohort.auth import authenticate_agent_token, configure_membership_db
from cerbere_cohort.persistence import PersistentWorkspace
from cerbere_cohort.postgres import _Connection as PostgresConnection
from cerbere_cohort.postgres import PostgresPersistentWorkspace
from cerbere_cohort.workspace import Branch


def _error_envelope(exc: Exception) -> dict[str, Any]:
    """Construit l'enveloppe structurée d'une erreur outil.

    Les erreurs de domaine exposent leur code stable et leurs détails ;
    toute autre exception est ramenée à un code générique, sans jamais
    exposer une repr Python comme seule information.
    """
    if isinstance(exc, CerbereError):
        return exc.to_dict()
    if isinstance(exc, ValueError):
        return {"code": "invalid_argument", "message": str(exc), "details": None}
    return {"code": "internal_error", "message": str(exc), "details": None}


def structured_tool_errors(fn):
    """Convertit les exceptions de domaine en ToolError structuré (JSON dans le message).

    Enveloppe : {"code": <code stable>, "message": <texte français d'origine>,
    "details": {...}}. Le message reste analysable par un humain et le code
    par un agent. Les exceptions FastMCP (validation d'arguments) ne passent
    pas par ce chemin : elles sont gérées par le SDK.
    """
    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - frontière de transport MCP
            raise ToolError(json.dumps(_error_envelope(exc), ensure_ascii=False)) from exc

    return wrapper


class MCPAuthorizationError(CerbereError):
    code = 'insufficient_scope'


_request_principal: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "cohort_mcp_request_principal", default=None
)


class MCPAgentAuthMiddleware:
    """Authenticate every remote MCP HTTP request with its own agent credential."""

    def __init__(self, app, organization_id: str | None = None):
        self.app = app
        self.organization_id = organization_id

    @staticmethod
    async def _reject(send, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1").strip()
        if not authorization.startswith("Bearer "):
            await self._reject(send, 401, "Bearer agent credential required")
            return

        agent_token = authorization[7:].strip()
        if not agent_token:
            await self._reject(send, 401, "Bearer agent credential required")
            return

        supplied_org = headers.get(b"x-organization-id")
        try:
            supplied_org_text = supplied_org.decode("ascii").strip() if supplied_org else None
        except UnicodeDecodeError:
            await self._reject(send, 400, "Invalid X-Organization-ID header")
            return

        try:
            principal = authenticate_agent_token(agent_token, self.organization_id)
            if supplied_org_text and supplied_org_text.lower() != principal["organization_id"].lower():
                await self._reject(send, 403, "Credential is not valid for this organization")
                return
        except Exception as exc:  # fail closed at the transport boundary
            status = getattr(exc, "status_code", 503)
            if status not in {400, 401, 403, 503}:
                status = 503
            detail = getattr(exc, "detail", None)
            message = detail if isinstance(detail, str) else "Unable to authenticate agent credential"
            await self._reject(send, status, message)
            return

        request_token = _request_principal.set(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            _request_principal.reset(request_token)


class CohortService:
    """Thread-safe MCP-facing facade over PersistentWorkspace."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        authenticated_agent_id: str | None = None,
        require_leases: bool | None = None,
        organization_id: str | None = None,
        agent_scopes: list[str] | None = None,
    ):
        database_url = os.getenv('DATABASE_URL', '').strip()
    agent_token = os.getenv('COHORT_MCP_AGENT_TOKEN', '').strip()
    organization_id = os.getenv('COHORT_ORGANIZATION_ID', '').strip() or None

    if not database_url and args.transport != "stdio" and args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(
            "Unauthenticated SQLite MCP HTTP is restricted to loopback. "
            "Configure DATABASE_URL for authenticated remote access."
        )

    if database_url:
        # Credential hashes are globally unique; resolve the organization from
        # the token, then route the request to that organization's schema.
        auth_db = PostgresConnection(database_url, schema="public")
        configure_membership_db(auth_db)

        if args.transport == "stdio":
            if not organization_id:
                raise RuntimeError('COHORT_ORGANIZATION_ID is required for production stdio MCP')
            if not agent_token:
                raise RuntimeError('COHORT_MCP_AGENT_TOKEN is required for production stdio authentication')
            service = CohortService(
                args.db,
                organization_id=organization_id,
                require_leases=True,
            )
            try:
                principal = authenticate_agent_token(agent_token, organization_id)
                expected_agent_id = os.getenv('COHORT_MCP_AGENT_ID', '').strip()
                if expected_agent_id and expected_agent_id != principal['agent_id']:
                    raise RuntimeError('COHORT_MCP_AGENT_ID does not match the supplied agent credential')
                service.bind_agent_identity(principal['agent_id'], principal['scopes'])
                mcp = create_server(service, host=args.host, port=args.port)
                mcp.run(transport="stdio")
            finally:
                service.close()
                auth_db.close()
            return

        service = TenantServiceRouter(database_url)
        mcp = create_server(service, host=args.host, port=args.port)
        import uvicorn

        app = MCPAgentAuthMiddleware(
            mcp.streamable_http_app(),
            organization_id=organization_id,
        )
        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        finally:
            service.close()
            auth_db.close()
        return

    service = CohortService(args.db)
    mcp = create_server(service, host=args.host, port=args.port)
    try:
        if args.transport == "stdio":
            mcp.run(transport="stdio")
        else:
            # Local-only development transport; never bind unauthenticated mode publicly.
            mcp.run(transport="streamable-http")
    finally:
        service.close()


if __name__ == "__main__":
    main()
