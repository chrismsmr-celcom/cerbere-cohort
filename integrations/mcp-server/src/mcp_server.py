"""Cerbere Cohort MCP server.

The default transport is Streamable HTTP so multiple MCP clients can share one
database-backed workspace. Bind to loopback unless an authenticated proxy is used.
"""
from __future__ import annotations

import argparse
import contextvars
import hashlib
import time
import uuid
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
from cerbere_cohort.tasks import TaskCoordinator, TaskError


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

        # Render's health probe must not require an agent credential. Keep this
        # endpoint deliberately minimal: it exposes no tenant or database data.
        path = scope.get("path", "")
        method = scope.get("method", "").upper()
        if (path == "/api/health" and method in {"GET", "HEAD"}) or (
            path == "/" and method == "HEAD"
        ):
            body = b'{"status":"ok"}'
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": b"" if method == "HEAD" else body,
            })
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
        self.authenticated_agent_id = (authenticated_agent_id or '').strip() or None
        self.agent_scopes = set(agent_scopes) if agent_scopes is not None else None
        if require_leases is None:
            require_leases = os.getenv('COHORT_REQUIRE_LEASES', 'true' if database_url else 'false').lower() in {'1', 'true', 'yes', 'on'}
        if database_url.startswith(('postgres://', 'postgresql://')):
            self.db_path = None
            self.workspace = PostgresPersistentWorkspace(database_url, require_leases=require_leases, organization_id=organization_id)
        else:
            if db_path is None:
                raise ValueError('Set DATABASE_URL or provide a local SQLite --db path')
            self.db_path = Path(db_path).expanduser()
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.workspace = PersistentWorkspace(self.db_path, require_leases=require_leases)
        self._db = self.workspace._conn
        self._lock = threading.RLock()
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS api_branches (
                branch_id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL,
                branch_name TEXT NOT NULL,
                base_version TEXT NOT NULL,
                current_data_json TEXT NOT NULL,
                intents_json TEXT NOT NULL,
                fencing_tokens_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                conflicting_keys_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        if self.db_path is None:
            self._db.execute(
                "ALTER TABLE api_branches ADD COLUMN IF NOT EXISTS fencing_tokens_json TEXT NOT NULL DEFAULT '{}'"
            )
        else:
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(api_branches)")}
            if "fencing_tokens_json" not in columns:
                self._db.execute(
                    "ALTER TABLE api_branches ADD COLUMN fencing_tokens_json TEXT NOT NULL DEFAULT '{}'"
                )
        self._db.commit()
        self._migrate_legacy_mcp_branches()

    def _migrate_legacy_mcp_branches(self) -> None:
        """Move pre-unification MCP branches into the REST branch registry once."""
        if self.db_path is not None:
            exists = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_branches'"
            ).fetchone()
        else:
            schema = getattr(self._db, "schema", "public")
            exists = self._db.execute(
                "SELECT to_regclass(?) AS name", (f"{schema}.cohort_mcp_branches",)
            ).fetchone()
        if not exists or (self.db_path is None and not exists["name"]):
            return
        rows = self._db.execute("SELECT * FROM mcp_branches").fetchall()
        for row in rows:
            digest = hashlib.sha256(
                f"{row['agent_id']}\\0{row['branch_name']}".encode("utf-8")
            ).hexdigest()[:32]
            self._db.execute(
                """
                INSERT INTO api_branches
                    (branch_id, agent_id, branch_name, base_version, current_data_json,
                     intents_json, fencing_tokens_json, status, conflicting_keys_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open', '[]', ?)
                ON CONFLICT(branch_id) DO NOTHING
                """,
                (
                    f"mcp-legacy-{digest}", row["agent_id"], row["branch_name"],
                    row["base_version"], row["current_data_json"], row["intents_json"],
                    row["fencing_tokens_json"] or "{}", str(time.time()),
                ),
            )
        self._db.commit()

    def _branch_row(self, agent_id: str, branch_name: str):
        rows = self._db.execute(
            "SELECT * FROM api_branches WHERE agent_id = ? AND branch_name = ? AND status = 'open'",
            (agent_id, branch_name),
        ).fetchall()
        if not rows:
            raise ValueError(
                f"Branch '{branch_name}' not found for agent '{agent_id}'. "
                "Create it first with workspace_create_branch."
            )
        if len(rows) > 1:
            raise ValueError(
                f"Multiple open branches named '{branch_name}' exist for agent '{agent_id}'; "
                "resolve the duplicate through the REST API before continuing."
            )
        return rows[0]

    def _branch_from_row(self, row) -> Branch:
        base = self.workspace._workspace._snapshots.get(row["base_version"])
        if base is None:
            raise RuntimeError(
                f"Cannot restore branch {row['agent_id']}:{row['branch_name']}: "
                "its base snapshot is missing"
            )
        branch = Branch(
            name=row["branch_name"],
            base_snapshot=base,
            lease_manager=self.workspace._workspace._lease_manager,
            agent_id=row["agent_id"],
            require_leases=self.workspace._workspace._require_leases,
        )
        branch.current_data = json.loads(row["current_data_json"])
        branch.intents = json.loads(row["intents_json"])
        branch.fencing_tokens = json.loads(row["fencing_tokens_json"] or "{}")
        return branch

    def _save_branch_row(self, branch_id: str, agent_id: str, branch: Branch,
                         *, status: str = "open", conflicting_keys=None) -> None:
        self._db.execute(
            """
            INSERT INTO api_branches
                (branch_id, agent_id, branch_name, base_version, current_data_json,
                 intents_json, fencing_tokens_json, status, conflicting_keys_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(branch_id) DO UPDATE SET
                current_data_json=excluded.current_data_json,
                intents_json=excluded.intents_json,
                fencing_tokens_json=excluded.fencing_tokens_json,
                status=excluded.status,
                conflicting_keys_json=excluded.conflicting_keys_json
            """,
            (
                branch_id, agent_id, branch.name, branch.base_version,
                json.dumps(branch.current_data, ensure_ascii=False, sort_keys=True),
                json.dumps(branch.intents, ensure_ascii=False, sort_keys=True),
                json.dumps(branch.fencing_tokens, ensure_ascii=False, sort_keys=True),
                status, json.dumps(conflicting_keys or []), str(time.time()),
            ),
        )

    def _task_coordinator(self) -> TaskCoordinator:
        return TaskCoordinator(self._db)

    def bind_agent_identity(self, agent_id: str, scopes: list[str]) -> None:
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError('authenticated agent identity is missing')
        self.authenticated_agent_id = agent_id
        self.agent_scopes = set(scopes)

    def _require_scope(self, scope: str) -> None:
        principal = _request_principal.get()
        scopes = set(principal["scopes"]) if principal is not None else self.agent_scopes
        if scopes is not None and scope not in scopes:
            raise MCPAuthorizationError(
                f'MCP agent credential lacks required scope: {scope}',
                details={'required_scope': scope},
            )

    def _assert_agent(self, agent_id: str) -> None:
        if not isinstance(agent_id, str) or not agent_id.strip():
            raise ValueError('agent_id must not be empty')
        principal = _request_principal.get()
        authenticated_agent_id = (
            principal["agent_id"] if principal is not None else self.authenticated_agent_id
        )
        if authenticated_agent_id and agent_id != authenticated_agent_id:
            raise ValueError('agent_id does not match the identity bound to this MCP request')

    def _branch(self, agent_id: str, branch_name: str) -> Branch:
        self._assert_agent(agent_id)
        return self._branch_from_row(self._branch_row(agent_id, branch_name))

    def get_state(self) -> dict[str, Any]:
        self._require_scope('workspace:read')
        with self._lock:
            self.workspace.refresh()
            return {
                "head_version": self.workspace.head.version_id,
                "state": self.workspace.head.data,
            }

    def create_branch(self, agent_id: str, branch_name: str) -> dict[str, str]:
        self._require_scope('workspace:write')
        self._assert_agent(agent_id)
        self._validate_identity(agent_id, branch_name)

        def create_and_persist():
            existing = self._db.execute(
                "SELECT branch_id FROM api_branches WHERE agent_id = ? AND branch_name = ? AND status = 'open'",
                (agent_id, branch_name),
            ).fetchone()
            if existing:
                raise ValueError(f"Branch '{branch_name}' already exists for '{agent_id}'.")
            branch = self.workspace._workspace.create_branch(agent_id, branch_name)
            branch_id = f"mcp-{uuid.uuid4()}"
            self._save_branch_row(branch_id, agent_id, branch)
            return {
                "branch_id": branch_id,
                "agent_id": agent_id,
                "branch_name": branch_name,
                "base_version": branch.base_version,
            }

        with self._lock:
            return self.workspace._mutate(create_and_persist)

    def read_branch(self, agent_id: str, branch_name: str, key: str) -> dict[str, Any]:
        self._require_scope('workspace:read')
        self._assert_agent(agent_id)
        with self._lock:
            self.workspace.refresh()
            branch = self._branch(agent_id, branch_name)
            return {"key": key, "exists": key in branch.current_data, "value": branch.get(key)}

    def write_branch(
        self, agent_id: str, branch_name: str, key: str, value: Any, intent: str | None = None
    ) -> dict[str, Any]:
        self._require_scope('workspace:write')
        self._assert_agent(agent_id)

        def write_and_persist():
            row = self._branch_row(agent_id, branch_name)
            branch = self._branch_from_row(row)
            branch.set(key, value, intent=intent)
            self._save_branch_row(row["branch_id"], agent_id, branch,
                                  conflicting_keys=json.loads(row["conflicting_keys_json"]))
            return {"updated": True, "key": key, "branch_name": branch_name}

        with self._lock:
            return self.workspace._mutate(write_and_persist)

    def delete_branch_key(
        self, agent_id: str, branch_name: str, key: str, intent: str | None = None
    ) -> dict[str, Any]:
        self._require_scope('workspace:write')
        self._assert_agent(agent_id)

        def delete_and_persist():
            row = self._branch_row(agent_id, branch_name)
            branch = self._branch_from_row(row)
            branch.delete(key, intent=intent)
            self._save_branch_row(row["branch_id"], agent_id, branch,
                                  conflicting_keys=json.loads(row["conflicting_keys_json"]))
            return {"deleted": True, "key": key, "branch_name": branch_name}

        with self._lock:
            return self.workspace._mutate(delete_and_persist)

    def commit_branch(
        self, agent_id: str, branch_name: str, task_id: str | None = None,
        task_fencing_token: int | None = None, idempotency_key: str | None = None,
        result: Any = None,
    ) -> dict[str, Any]:
        self._require_scope('workspace:write')
        self._assert_agent(agent_id)
        if task_id and (task_fencing_token is None or not idempotency_key):
            raise ValueError("Task-bound commits require task_fencing_token and idempotency_key")
        if not task_id and (task_fencing_token is not None or idempotency_key is not None):
            raise ValueError("Task fencing and idempotency arguments require task_id")

        task_coordinator = self._task_coordinator() if task_id else None

        def commit_once():
            row = self._branch_row(agent_id, branch_name)
            branch = self._branch_from_row(row)
            snapshot = self.workspace._workspace.commit(agent_id, branch)
            completed_task = None
            if task_id:
                completed_task = task_coordinator.complete_in_transaction(
                    task_id, agent_id, task_fencing_token,
                    result if result is not None else {
                        "branch_id": row["branch_id"], "head": snapshot.version_id
                    },
                    idempotency_key,
                )
            self._save_branch_row(
                row["branch_id"], agent_id, branch, status="merged", conflicting_keys=[]
            )
            response = {
                "committed": True,
                "branch_id": row["branch_id"],
                "head_version": snapshot.version_id,
                "state": snapshot.data,
            }
            if completed_task is not None:
                response["task"] = completed_task
            return response

        with self._lock:
            if task_id:
                request_hash = hashlib.sha256(json.dumps({
                    "agent_id": agent_id,
                    "branch_name": branch_name,
                    "task_id": task_id,
                    "task_fencing_token": task_fencing_token,
                    "result": result,
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
                return self.workspace.execute_idempotent(
                    f"task-branch-commit:{task_id}", idempotency_key, request_hash, commit_once
                )
            return self.workspace._mutate(commit_once)

    def create_task(self, title: str, payload: Any, idempotency_key: str | None = None):
        self._require_scope('workspace:write')
        return self._task_coordinator().create(title, payload, idempotency_key)

    def list_tasks(self, limit: int = 100):
        self._require_scope('workspace:read')
        items = self._task_coordinator().list(limit)
        principal = _request_principal.get()
        agent_id = principal["agent_id"] if principal is not None else self.authenticated_agent_id
        if agent_id:
            items = [item for item in items if item["status"] == "pending" or item["assigned_agent_id"] == agent_id]
        return items

    def get_task(self, task_id: str):
        self._require_scope('workspace:read')
        item = self._task_coordinator().get(task_id)
        principal = _request_principal.get()
        agent_id = principal["agent_id"] if principal is not None else self.authenticated_agent_id
        if agent_id and item["status"] != "pending" and item["assigned_agent_id"] != agent_id:
            raise ValueError("Task not found")
        return item

    def get_task_events(self, task_id: str):
        self.get_task(task_id)
        return self._task_coordinator().events(task_id)

    def claim_task(self, task_id: str, agent_id: str, lease_seconds: float = 60):
        self._require_scope('workspace:write')
        self._assert_agent(agent_id)
        return self._task_coordinator().claim(task_id, agent_id, lease_seconds)

    def start_task(self, task_id: str, agent_id: str, fencing_token: int):
        self._require_scope('workspace:write')
        self._assert_agent(agent_id)
        return self._task_coordinator().start(task_id, agent_id, fencing_token)

    def heartbeat_task(self, task_id: str, agent_id: str, fencing_token: int,
                       lease_seconds: float = 60):
        self._require_scope('workspace:write')
        self._assert_agent(agent_id)
        return self._task_coordinator().heartbeat(task_id, agent_id, fencing_token, lease_seconds)

    def complete_task(self, task_id: str, agent_id: str, fencing_token: int,
                      result: Any, idempotency_key: str):
        self._require_scope('workspace:write')
        self._assert_agent(agent_id)
        return self._task_coordinator().complete(
            task_id, agent_id, fencing_token, result, idempotency_key
        )

    def fail_task(self, task_id: str, agent_id: str, fencing_token: int, error: str):
        self._require_scope('workspace:write')
        self._assert_agent(agent_id)
        return self._task_coordinator().fail(task_id, agent_id, fencing_token, error)

    def retry_task(self, task_id: str):
        self._require_scope('tasks:admin')
        if self.authenticated_agent_id or _request_principal.get() is not None:
            raise MCPAuthorizationError("Only a workspace user can requeue a failed task")
        return self._task_coordinator().retry(task_id)

    def acquire_lease(
        self, agent_id: str, resource_key: str, duration_seconds: float = 30.0
    ) -> dict[str, Any]:
        self._require_scope('leases:write')
        self._assert_agent(agent_id)
        if not isinstance(duration_seconds, (int, float)) or not math.isfinite(duration_seconds) or duration_seconds <= 0 or duration_seconds > 86400:
            raise ValueError('duration_seconds must be greater than 0 and at most 86400')
        with self._lock:
            lease = self.workspace.acquire_lease(agent_id, resource_key, duration_seconds)
            return {
                "acquired": True,
                "resource_key": resource_key,
                "agent_id": agent_id,
                "expires_at": lease.expires_at,
                "fencing_token": lease.fencing_token,
            }

    def release_lease(self, agent_id: str, resource_key: str) -> dict[str, bool]:
        self._require_scope('leases:write')
        self._assert_agent(agent_id)
        with self._lock:
            self.workspace.release_lease(agent_id, resource_key)
            return {"released": True}

    def get_events(self, limit: int = 50) -> list[dict[str, Any]]:
        self._require_scope('workspace:read')
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._lock:
            events = self.workspace.get_events()
            principal = _request_principal.get()
            authenticated_agent_id = (
                principal["agent_id"] if principal is not None else self.authenticated_agent_id
            )
            if authenticated_agent_id:
                events = [event for event in events if event.agent_id == authenticated_agent_id]
            events = events[-limit:]
            return [event.model_dump(mode="json") for event in events]

    @staticmethod
    def _validate_identity(agent_id: str, branch_name: str) -> None:
        if not agent_id.strip() or not branch_name.strip():
            raise ValueError("agent_id and branch_name must not be empty")
        if len(agent_id) > 128 or len(branch_name) > 128:
            raise ValueError("agent_id and branch_name must be 128 characters or fewer")

    def close(self) -> None:
        with self._lock:
            self.workspace.close()


class TenantServiceRouter:
    """Route each authenticated MCP request to its organization schema."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.max_tenants = int(os.getenv("COHORT_MCP_MAX_TENANTS", "64"))
        if self.max_tenants < 1:
            raise RuntimeError("COHORT_MCP_MAX_TENANTS must be at least 1")
        self._services: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()

    def _acquire(self) -> tuple[str, CohortService]:
        principal = _request_principal.get()
        if principal is None:
            raise MCPAuthorizationError("Authenticated MCP request context is missing")
        organization_id = str(principal.get("organization_id") or "").strip()
        if not organization_id:
            raise MCPAuthorizationError("Authenticated credential has no organization")

        with self._lock:
            entry = self._services.get(organization_id)
            if entry is not None:
                entry["active"] += 1
                self._services.move_to_end(organization_id)
                return organization_id, entry["service"]

            if len(self._services) >= self.max_tenants:
                evicted = False
                for tenant_id, candidate in list(self._services.items()):
                    if candidate["active"] == 0:
                        candidate["service"].close()
                        del self._services[tenant_id]
                        evicted = True
                        break
                if not evicted:
                    raise RuntimeError("MCP tenant capacity is temporarily exhausted")

            service = CohortService(
                organization_id=organization_id,
                require_leases=True,
            )
            self._services[organization_id] = {"service": service, "active": 1}
            return organization_id, service

    def _release(self, organization_id: str) -> None:
        with self._lock:
            entry = self._services.get(organization_id)
            if entry is not None:
                entry["active"] = max(0, entry["active"] - 1)

    def _call(self, method_name: str, *args, **kwargs):
        organization_id, service = self._acquire()
        try:
            method = getattr(service, method_name)
            return method(*args, **kwargs)
        finally:
            self._release(organization_id)

    def __getattr__(self, method_name: str):
        if method_name.startswith("_"):
            raise AttributeError(method_name)
        return lambda *args, **kwargs: self._call(method_name, *args, **kwargs)

    def close(self) -> None:
        with self._lock:
            for entry in self._services.values():
                entry["service"].close()
            self._services.clear()


def create_server(service: CohortService, host: str = "127.0.0.1", port: int = 8765) -> FastMCP:
    mcp = FastMCP("cerbere-cohort", host=host, port=port)

    @mcp.tool()
    @structured_tool_errors
    def workspace_get_state() -> dict[str, Any]:
        """Return the current committed workspace state and HEAD version."""
        return service.get_state()

    @mcp.tool()
    @structured_tool_errors
    def workspace_create_branch(agent_id: str, branch_name: str) -> dict[str, str]:
        """Create an isolated branch from the current HEAD before editing shared state."""
        return service.create_branch(agent_id, branch_name)

    @mcp.tool()
    @structured_tool_errors
    def workspace_read_in_branch(agent_id: str, branch_name: str, key: str) -> dict[str, Any]:
        """Read one key from an agent's isolated branch."""
        return service.read_branch(agent_id, branch_name, key)

    @mcp.tool()
    @structured_tool_errors
    def workspace_set_in_branch(
        agent_id: str, branch_name: str, key: str, value: Any, intent: str | None = None
    ) -> dict[str, Any]:
        """Write a JSON value to a branch. The write is persisted immediately."""
        return service.write_branch(agent_id, branch_name, key, value, intent)

    @mcp.tool()
    @structured_tool_errors
    def workspace_delete_in_branch(
        agent_id: str, branch_name: str, key: str, intent: str | None = None
    ) -> dict[str, Any]:
        """Delete a key from a branch. The deletion is persisted immediately."""
        return service.delete_branch_key(agent_id, branch_name, key, intent)

    @mcp.tool()
    @structured_tool_errors
    def workspace_commit(
        agent_id: str, branch_name: str, task_id: str | None = None,
        task_fencing_token: int | None = None, idempotency_key: str | None = None,
        result: Any = None,
    ) -> dict[str, Any]:
        """Commit a branch, optionally completing its fenced task in the same transaction."""
        return service.commit_branch(
            agent_id, branch_name, task_id, task_fencing_token, idempotency_key, result
        )

    @mcp.tool()
    @structured_tool_errors
    def task_create(title: str, payload: Any, idempotency_key: str | None = None):
        """Create a durable shared task."""
        return service.create_task(title, payload, idempotency_key)

    @mcp.tool()
    @structured_tool_errors
    def task_list(limit: int = 100):
        """List pending tasks and tasks assigned to the authenticated agent."""
        return service.list_tasks(limit)

    @mcp.tool()
    @structured_tool_errors
    def task_get(task_id: str):
        """Read a task visible to the authenticated agent."""
        return service.get_task(task_id)

    @mcp.tool()
    @structured_tool_errors
    def task_events(task_id: str):
        """Read lifecycle events for a visible task."""
        return service.get_task_events(task_id)

    @mcp.tool()
    @structured_tool_errors
    def task_claim(task_id: str, agent_id: str, lease_seconds: float = 60):
        """Claim a pending or expired task and receive its fencing token."""
        return service.claim_task(task_id, agent_id, lease_seconds)

    @mcp.tool()
    @structured_tool_errors
    def task_start(task_id: str, agent_id: str, fencing_token: int):
        """Start an assigned task using its current fencing token."""
        return service.start_task(task_id, agent_id, fencing_token)

    @mcp.tool()
    @structured_tool_errors
    def task_heartbeat(task_id: str, agent_id: str, fencing_token: int, lease_seconds: float = 60):
        """Renew an active task lease."""
        return service.heartbeat_task(task_id, agent_id, fencing_token, lease_seconds)

    @mcp.tool()
    @structured_tool_errors
    def task_complete(task_id: str, agent_id: str, fencing_token: int,
                      result: Any, idempotency_key: str):
        """Complete a task exactly once using its current fencing token."""
        return service.complete_task(task_id, agent_id, fencing_token, result, idempotency_key)

    @mcp.tool()
    @structured_tool_errors
    def task_fail(task_id: str, agent_id: str, fencing_token: int, error: str):
        """Mark an active task failed using its current fencing token."""
        return service.fail_task(task_id, agent_id, fencing_token, error)

    @mcp.tool()
    @structured_tool_errors
    def task_retry(task_id: str):
        """Requeue a failed task; requires the tasks:admin scope and a non-agent principal."""
        return service.retry_task(task_id)

    @mcp.tool()
    @structured_tool_errors
    def lease_acquire(
        agent_id: str, resource_key: str, duration_seconds: float = 30.0
    ) -> dict[str, Any]:
        """Acquire a time-limited lease on a shared resource."""
        return service.acquire_lease(agent_id, resource_key, duration_seconds)

    @mcp.tool()
    @structured_tool_errors
    def lease_release(agent_id: str, resource_key: str) -> dict[str, bool]:
        """Release a lease owned by the calling agent."""
        return service.release_lease(agent_id, resource_key)

    @mcp.tool()
    @structured_tool_errors
    def get_events(limit: int = 50) -> list[dict[str, Any]]:
        """Read the latest append-only coordination events."""
        return service.get_events(limit)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Cerbere Cohort MCP server")
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "stdio"),
        default=os.getenv("COHORT_MCP_TRANSPORT", "streamable-http"),
    )
    parser.add_argument(
        "--db",
        default=os.getenv(
            "COHORT_DB_PATH", str(Path.home() / ".cerbere-cohort" / "workspace.db")
        ),
        help="Local SQLite path; production uses DATABASE_URL (Supabase PostgreSQL)",
    )
    parser.add_argument("--host", default=os.getenv("COHORT_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("COHORT_MCP_PORT", "8765")))
    args = parser.parse_args()

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
