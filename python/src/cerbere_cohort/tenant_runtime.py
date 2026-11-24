"""Request-scoped PostgreSQL runtime registry for multi-tenant Cohort."""
from __future__ import annotations

import contextvars
import json
import threading
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .postgres import PostgresPersistentWorkspace
from .workspace import Branch
from .tasks import ensure_task_schema

DATABASE_URL = ""
REQUIRE_LEASES = True
_current_runtime: contextvars.ContextVar["TenantRuntime | None"] = contextvars.ContextVar(
    "cohort_tenant_runtime", default=None
)
_registry: dict[str, "TenantRuntime"] = {}
_registry_lock = threading.RLock()


@dataclass
class TenantRuntime:
    organization_id: str
    workspace: PostgresPersistentWorkspace
    branches: dict[str, dict[str, Any]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


def _ensure_branch_table(runtime: TenantRuntime) -> None:
    conn = runtime.workspace._conn
    conn.execute(
        "CREATE TABLE IF NOT EXISTS api_branches ("
        "branch_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, branch_name TEXT NOT NULL, "
        "base_version TEXT NOT NULL, current_data_json TEXT NOT NULL, intents_json TEXT NOT NULL, "
        "fencing_tokens_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, "
        "conflicting_keys_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "ALTER TABLE api_branches ADD COLUMN IF NOT EXISTS "
        "fencing_tokens_json TEXT NOT NULL DEFAULT '{}'"
    )
    conn.commit()
    ensure_task_schema(conn)


def _restore_branches(runtime: TenantRuntime) -> None:
    workspace = runtime.workspace
    conn = workspace._conn
    committed = {
        (event.agent_id, event.payload.get("branch_name"))
        for event in workspace.get_events()
        if event.action == "workspace.commit"
    }
    for row in conn.execute("SELECT * FROM api_branches").fetchall():
        if (row["agent_id"], row["branch_name"]) in committed:
            conn.execute(
                "UPDATE api_branches SET status = 'merged', conflicting_keys_json = '[]' "
                "WHERE branch_id = ?", (row["branch_id"],)
            )
            continue
        base = workspace._workspace._snapshots.get(row["base_version"])
        if base is None:
            continue
        branch = Branch(
            row["branch_name"], base, workspace._workspace._lease_manager,
            row["agent_id"], workspace._workspace._require_leases,
        )
        branch.current_data = json.loads(row["current_data_json"])
        branch.intents = json.loads(row["intents_json"])
        branch.fencing_tokens = json.loads(row["fencing_tokens_json"] or "{}")
        runtime.branches[row["branch_id"]] = {
            "branch": branch,
            "agent_id": row["agent_id"],
            "status": row["status"],
            "conflicting_keys": json.loads(row["conflicting_keys_json"]),
            "created_at": row["created_at"],
        }
    conn.commit()


def get_runtime(organization_id: str) -> TenantRuntime:
    org_id = str(UUID(organization_id))
    with _registry_lock:
        runtime = _registry.get(org_id)
        if runtime is not None:
            return runtime
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is required for workspace-scoped runtime")
        workspace = PostgresPersistentWorkspace(
            DATABASE_URL, require_leases=REQUIRE_LEASES, organization_id=org_id
        )
        workspace._workspace._require_leases = REQUIRE_LEASES
        runtime = TenantRuntime(org_id, workspace)
        _ensure_branch_table(runtime)
        _restore_branches(runtime)
        _registry[org_id] = runtime
        return runtime


def set_current_runtime(runtime: TenantRuntime):
    return _current_runtime.set(runtime)


def reset_current_runtime(token) -> None:
    _current_runtime.reset(token)


def current_runtime() -> TenantRuntime:
    runtime = _current_runtime.get()
    if runtime is None:
        raise RuntimeError("No authenticated workspace runtime is bound to this request")
    return runtime


class WorkspaceProxy:
    def __getattr__(self, name):
        return getattr(current_runtime().workspace, name)


class BranchesProxy:
    def __getattr__(self, name):
        return getattr(current_runtime().branches, name)

    def __getitem__(self, key):
        return current_runtime().branches[key]

    def __setitem__(self, key, value):
        current_runtime().branches[key] = value

    def __delitem__(self, key):
        del current_runtime().branches[key]

    def __iter__(self):
        return iter(current_runtime().branches)

    def __len__(self):
        return len(current_runtime().branches)

    def items(self):
        return current_runtime().branches.items()

    def get(self, key, default=None):
        return current_runtime().branches.get(key, default)

    def values(self):
        return current_runtime().branches.values()


class LockProxy:
    def __init__(self):
        self._held = contextvars.ContextVar("cohort_held_tenant_locks", default=())

    def __enter__(self):
        lock = current_runtime().lock
        lock.acquire()
        self._held.set(self._held.get() + (lock,))
        return self

    def __exit__(self, exc_type, exc, tb):
        held = self._held.get()
        if not held:
            raise RuntimeError("Tenant lock released without being acquired")
        self._held.set(held[:-1])
        held[-1].release()
        return False
