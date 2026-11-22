"""HTTP adapter for Cohort with request-scoped, schema-isolated workspaces.

Each authenticated request selects a workspace through X-Organization-ID;
membership is verified before its isolated runtime is bound to the request.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import secrets
import threading
import uuid
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from cerbere_cohort.workspace import Branch, Workspace, _MISSING
from cerbere_cohort.telemetry import send_installation_ping
from cerbere_cohort.auth import (
    configure_membership_db,
    require_workspace_member,
    authenticate_agent_token,
    _decode_access_token,
    require_workspace_read_access,
    require_workspace_write_access,
    require_lease_read_access,
    require_lease_write_access,
)

from cerbere_cohort import (
    ConflictError,
    LeaseError,
    PreferBranch,
    PreferHead,
    PreferAgent,
    Resolution,
)
from cerbere_cohort.persistence import PersistentWorkspace

if DATABASE_URL:
    from psycopg.types.json import Jsonb

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = Path(os.getenv("COHORT_DB_PATH", "./data/cohort.db"))
if DATABASE_URL and not DATABASE_URL.startswith(("postgres://", "postgresql://")):
    raise RuntimeError("DATABASE_URL must be a PostgreSQL connection URL")
if DATABASE_URL and not os.getenv("SUPABASE_URL"):
    raise RuntimeError("SUPABASE_URL is required in production")
# Fail closed: without DATABASE_URL the API runs in the unauthenticated local
# development mode. Never let a hosted deployment fall into it by accident.
_HOSTED = bool(os.getenv("RENDER") or os.getenv("COHORT_ENV", "").lower() == "production")
if _HOSTED and not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required on hosted deployments; refusing to start without authentication")
if not DATABASE_URL:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
REQUIRE_LEASES = os.getenv("COHORT_REQUIRE_LEASES", "true" if DATABASE_URL else "false").lower() in {"1", "true", "yes", "on"}
if DATABASE_URL:
    from cerbere_cohort.postgres import _Connection
    from cerbere_cohort.tenant_runtime import (
        WorkspaceProxy, BranchesProxy, LockProxy, get_runtime, set_current_runtime, reset_current_runtime,
    )
    import cerbere_cohort.tenant_runtime as _tenant_runtime
    _tenant_runtime.DATABASE_URL = DATABASE_URL
    _tenant_runtime.REQUIRE_LEASES = REQUIRE_LEASES
    _membership_connection = _Connection(DATABASE_URL, schema="public")
    configure_membership_db(_membership_connection)
    workspace = WorkspaceProxy()
    _branches = BranchesProxy()
    _lock = LockProxy()
else:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    workspace = PersistentWorkspace(DB_PATH, require_leases=REQUIRE_LEASES)
    workspace._workspace._require_leases = REQUIRE_LEASES
    _membership_connection = workspace._conn
    _branches: dict[str, dict[str, Any]] = {}
    _lock = threading.RLock()

def _ensure_api_branches_schema(conn, *, postgres: bool) -> None:
    """Create the API branch registry and safely upgrade pre-fencing schemas."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS api_branches ("
        "branch_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, branch_name TEXT NOT NULL, "
        "base_version TEXT NOT NULL, current_data_json TEXT NOT NULL, intents_json TEXT NOT NULL, "
        "fencing_tokens_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL, "
        "conflicting_keys_json TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    if postgres:
        conn.execute(
            "ALTER TABLE api_branches ADD COLUMN IF NOT EXISTS "
            "fencing_tokens_json TEXT NOT NULL DEFAULT '{}'"
        )
    else:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(api_branches)")}
        if "fencing_tokens_json" not in columns:
            conn.execute(
                "ALTER TABLE api_branches ADD COLUMN fencing_tokens_json TEXT NOT NULL DEFAULT '{}'"
            )
    conn.commit()


if not DATABASE_URL:
    _ensure_api_branches_schema(workspace._conn, postgres=False)

def _save_api_branch(branch_id: str, item: dict[str, Any]) -> None:
    b = item['branch']
    workspace._conn.execute("INSERT INTO api_branches (branch_id, agent_id, branch_name, base_version, current_data_json, intents_json, fencing_tokens_json, status, conflicting_keys_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(branch_id) DO UPDATE SET current_data_json=excluded.current_data_json, intents_json=excluded.intents_json, fencing_tokens_json=excluded.fencing_tokens_json, status=excluded.status, conflicting_keys_json=excluded.conflicting_keys_json", (branch_id, item['agent_id'], b.name, b.base_version, json.dumps(b.current_data, ensure_ascii=False), json.dumps(b.intents, ensure_ascii=False), json.dumps(b.fencing_tokens, ensure_ascii=False), item['status'], json.dumps(item.get('conflicting_keys', [])), item['created_at']))
    workspace._conn.commit()

def _restore_api_branches() -> None:
    committed = {
        (event.agent_id, event.payload.get('branch_name'))
        for event in workspace.get_events()
        if event.action == 'workspace.commit'
    }
    for row in workspace._conn.execute('SELECT * FROM api_branches').fetchall():
        if (row['agent_id'], row['branch_name']) in committed:
            workspace._conn.execute("UPDATE api_branches SET status = 'merged', conflicting_keys_json = '[]' WHERE branch_id = ?", (row['branch_id'],))
            continue
        base = workspace._workspace._snapshots.get(row['base_version'])
        if base is None:
            continue
        b = Branch(row['branch_name'], base, workspace._workspace._lease_manager, row['agent_id'], workspace._workspace._require_leases)
        b.current_data = json.loads(row['current_data_json'])
        b.intents = json.loads(row['intents_json'])
        b.fencing_tokens = json.loads(row['fencing_tokens_json'] or "{}")
        _branches[row['branch_id']] = {'branch': b, 'agent_id': row['agent_id'], 'status': row['status'], 'conflicting_keys': json.loads(row['conflicting_keys_json']), 'created_at': row['created_at']}
    workspace._conn.commit()

if not DATABASE_URL:
    _restore_api_branches()

app = FastAPI(title="Cerbere Cohort API", version="0.1.0")

@app.middleware("http")
async def bind_workspace_runtime(request: Request, call_next):
    """Bind the verified tenant runtime for the lifetime of each HTTP request."""
    organization_id = request.headers.get("X-Organization-ID")
    authorization = request.headers.get("Authorization")
    runtime_token = None
    try:
        if DATABASE_URL and organization_id:
            if not authorization or not authorization.startswith("Bearer "):
                return JSONResponse(status_code=401, content={"detail": "Bearer access token required"})
            bearer = authorization[7:].strip()
            # Validate the tenant boundary before constructing or exposing its runtime.
            if bearer.startswith("coh_"):
                authenticate_agent_token(bearer, organization_id)
            else:
                require_workspace_member(authorization, organization_id)
            runtime = get_runtime(organization_id)
            runtime_token = set_current_runtime(runtime)
        return await call_next(request)
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    finally:
        if runtime_token is not None:
            reset_current_runtime(runtime_token)



class WorkspaceOnboardingRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=120)
    job_title: str | None = Field(default=None, max_length=120)
    account_type: Literal["individual", "team", "enterprise"]
    workspace_name: str = Field(min_length=1, max_length=120)
    company_size: Literal["1-10", "10-100", "100+"] | None = None
    industry: str = Field(min_length=1, max_length=80)
    primary_use_case: Literal[
        "personal_projects", "internal_automation", "production_agents",
        "customer_facing_agents", "research"
    ]
    expected_agents: Literal["1-5", "6-20", "21-100", "100+"]
    frameworks: list[Literal[
        "langchain", "crewai", "autogen", "openai_agents", "custom", "other"
    ]] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _require_company_size_for_organizations(self):
        if self.account_type in {"team", "enterprise"} and not self.company_size:
            raise ValueError("Company size is required for team and enterprise workspaces")
        return self

    @field_validator("full_name", "workspace_name", "industry")
    @classmethod
    def _trim_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank")
        return value

    @field_validator("job_title")
    @classmethod
    def _trim_optional_text(cls, value: str | None) -> str | None:
        return value.strip()[:120] if value and value.strip() else None

    @field_validator("frameworks")
    @classmethod
    def _unique_frameworks(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Framework selections must be unique")
        return value


@app.post("/api/onboarding", status_code=200)
def complete_workspace_onboarding(
    body: WorkspaceOnboardingRequest,
    authorization: str | None = Header(default=None),
):
    """Create or complete the first owned workspace for the authenticated user."""
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Workspace onboarding requires PostgreSQL")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer access token required")
    claims = _decode_access_token(authorization[7:].strip())
    user_id = str(UUID(str(claims["sub"])))
    email = str(claims.get("email", "")).strip().lower()

    # A transaction + adapter advisory lock makes retries safe and prevents two
    # simultaneous onboarding submissions from creating duplicate workspaces.
    try:
        _membership_connection.execute("BEGIN IMMEDIATE")
        existing = _membership_connection.execute(
            """SELECT o.id
               FROM public.cohort_memberships m
               JOIN public.cohort_organizations o ON o.id = m.organization_id
               WHERE m.user_id = ? AND m.status = 'active' AND m.role = 'owner'
               ORDER BY o.created_at ASC LIMIT 1""",
            (user_id,),
        ).fetchone()

        if existing:
            organization_id = str(existing["id"])
            _membership_connection.execute(
                """UPDATE public.cohort_organizations
                   SET name = ?, account_type = ?, company_size = ?, industry = ?,
                       primary_use_case = ?, expected_agents = ?, frameworks = ?,
                       onboarding_completed_at = COALESCE(onboarding_completed_at, now())
                   WHERE id = ?""",
                (body.workspace_name, body.account_type,
                 body.company_size if body.account_type != "individual" else None,
                 body.industry, body.primary_use_case, body.expected_agents,
                 Jsonb(body.frameworks), organization_id),
            )
        else:
            created = _membership_connection.execute(
                """INSERT INTO public.cohort_organizations
                   (name, account_type, company_size, industry, primary_use_case,
                    expected_agents, frameworks, onboarding_completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, now())
                   RETURNING id""",
                (body.workspace_name, body.account_type,
                 body.company_size if body.account_type != "individual" else None,
                 body.industry, body.primary_use_case, body.expected_agents,
                 Jsonb(body.frameworks)),
            ).fetchone()
            organization_id = str(created["id"])
            _membership_connection.execute(
                """INSERT INTO public.cohort_memberships
                   (organization_id, user_id, role, status)
                   VALUES (?, ?, 'owner', 'active')
                   ON CONFLICT (organization_id, user_id)
                   DO UPDATE SET role = 'owner', status = 'active'""",
                (organization_id, user_id),
            )

        _membership_connection.execute(
            """INSERT INTO public.cohort_user_profiles(user_id, full_name, job_title, updated_at)
               VALUES (?, ?, ?, now())
               ON CONFLICT (user_id) DO UPDATE SET
                 full_name = EXCLUDED.full_name,
                 job_title = EXCLUDED.job_title,
                 updated_at = now()""",
            (user_id, body.full_name, body.job_title),
        )
        _membership_connection.execute(
            """INSERT INTO public.cohort_audit_logs
               (organization_id, actor_user_id, action, target_type, target_id, metadata)
               VALUES (?, ?, 'workspace.onboarding_completed', 'workspace', ?,
                       jsonb_build_object('account_type', ?, 'email_domain', ?))""",
            (organization_id, user_id, organization_id, body.account_type,
             email.rsplit("@", 1)[-1] if "@" in email else None),
        )
        _membership_connection.commit()
    except Exception as exc:
        _membership_connection.rollback()
        raise HTTPException(status_code=503, detail="Unable to complete workspace onboarding") from exc

    return {"completed": True, "workspace": {
        "id": organization_id, "name": body.workspace_name, "role": "owner",
        "account_type": body.account_type,
    }}


@app.get("/api/workspaces")
def list_user_workspaces(authorization: str | None = Header(default=None)):
    """List workspaces for the authenticated identity; no active workspace is assumed."""
    if not DATABASE_URL:
        return {"items": [{"id": "local", "name": "Local workspace", "role": "owner"}]}
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer access token required")
    claims = _decode_access_token(authorization[7:].strip())
    user_id = str(UUID(str(claims["sub"])))
    try:
        rows = _membership_connection.execute(
            """SELECT o.id, o.name, m.role, m.status, o.account_type, o.onboarding_completed_at
               FROM public.cohort_memberships m
               JOIN public.cohort_organizations o ON o.id = m.organization_id
               WHERE m.user_id = ? AND m.status = 'active'
               ORDER BY o.created_at ASC""",
            (user_id,),
        ).fetchall()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Unable to list workspace memberships") from exc
    return {"items": [
        {"id": str(row["id"]), "name": row["name"], "role": row["role"],
         "account_type": row["account_type"],
         "onboarding_completed": row["onboarding_completed_at"] is not None}
        for row in rows
    ]}


def _telemetry_collector_enabled() -> bool:
    return os.getenv("COHORT_TELEMETRY_COLLECTOR_ENABLED", "false").lower() in {"1", "true", "yes", "on"}


@app.on_event("startup")
def startup_optional_telemetry():
    if _telemetry_collector_enabled():
        if not DATABASE_URL:
            raise RuntimeError("Telemetry collection requires PostgreSQL DATABASE_URL")
        _membership_connection.execute("""CREATE TABLE IF NOT EXISTS cohort_telemetry_installations (
            installation_id TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            deployment_type TEXT NOT NULL,
            first_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        _membership_connection.commit()
    if os.getenv("COHORT_TELEMETRY", "false").lower() in {"1", "true", "yes", "on"}:
        threading.Thread(target=send_installation_ping, daemon=True, name="cohort-telemetry").start()
origins = [x.strip() for x in os.getenv("COHORT_CORS_ORIGINS", "*").split(",") if x.strip()]
if DATABASE_URL and (not os.getenv("COHORT_CORS_ORIGINS") or "*" in origins):
    raise RuntimeError("Set COHORT_CORS_ORIGINS to explicit frontend origins in production")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


MAX_VALUE_BYTES = 256 * 1024


class BranchCreate(BaseModel):
    agent_id: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)


class BranchWrite(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    value: Any
    intent: str | None = Field(default=None, max_length=2000)

    @field_validator("value")
    @classmethod
    def _bounded_value(cls, value: Any) -> Any:
        if len(json.dumps(value, ensure_ascii=False, default=str)) > MAX_VALUE_BYTES:
            raise ValueError(f"value exceeds {MAX_VALUE_BYTES} bytes")
        return value


class CommitRequest(BaseModel):
    # Exact snapshot version read when the branch was created.
    expected_head_version: str | None = Field(default=None, min_length=64, max_length=64)


class LeaseRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=120)
    key: str = Field(min_length=1, max_length=500)
    duration_seconds: float = Field(default=60, gt=0, le=86400)


class TelemetryInstall(BaseModel):
    installation_id: UUID
    version: str = Field(min_length=1, max_length=40)
    deployment_type: Literal["self-hosted"] = "self-hosted"


class ResolveRequest(BaseModel):
    strategy: Literal["PreferHead", "PreferBranch", "PreferAgent"] = "PreferHead"
    rationale: str = Field(min_length=1, max_length=2000)
    priority: list[str] = Field(default_factory=list)


def _event_dict(event):
    return event.model_dump(mode="json")


def _agent_owns(context: dict[str, Any], resource_agent_id: str) -> bool:
    return context.get("principal_type") != "agent" or context.get("agent_id") == resource_agent_id


def _require_agent_identity(context: dict[str, Any], requested_agent_id: str) -> None:
    if context.get("principal_type") == "agent" and context.get("agent_id") != requested_agent_id:
        raise HTTPException(status_code=403, detail="Agent credentials cannot act as another agent")


def _branch_summary(branch_id: str, item: dict[str, Any]) -> dict[str, Any]:
    branch = item["branch"]
    return {
        "id": branch_id,
        "name": branch.name,
        "agent_id": item["agent_id"],
        "base_version": branch.base_version,
        "status": item["status"],
        "conflicting_keys": item.get("conflicting_keys", []),
        "created_at": item["created_at"],
    }


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "cerbere-cohort",
        "runtime": "python",
        "persistence": "postgresql" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite",
        "branch_registry": "database",
        "authentication": "supabase" if DATABASE_URL else "development-bypass",
    }


@app.post("/api/telemetry/install", status_code=202)
def register_telemetry_installation(body: TelemetryInstall):
    """Accept an opt-in, anonymous self-hosted installation heartbeat."""
    if not _telemetry_collector_enabled():
        raise HTTPException(status_code=404, detail="Telemetry collection is disabled")
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Telemetry collector requires PostgreSQL")
    workspace._conn.execute(
        """INSERT INTO cohort_telemetry_installations
           (installation_id, version, deployment_type, first_seen, last_seen)
           VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT (installation_id) DO UPDATE SET
             version = EXCLUDED.version,
             deployment_type = EXCLUDED.deployment_type,
             last_seen = CURRENT_TIMESTAMP""",
        (str(body.installation_id), body.version, body.deployment_type),
    )
    workspace._conn.commit()
    return {"accepted": True}


@app.get("/api/traction", dependencies=[Depends(require_workspace_read_access)])
def get_traction_metrics():
    """Return aggregate opt-in installation counts to authenticated workspace members."""
    if not _telemetry_collector_enabled() or not DATABASE_URL:
        raise HTTPException(status_code=404, detail="Traction telemetry is not enabled")
    row = _membership_connection.execute(
        """SELECT COUNT(*) AS total_installations,
                  COUNT(*) FILTER (WHERE last_seen >= CURRENT_TIMESTAMP - INTERVAL '30 days') AS active_30d
           FROM cohort_telemetry_installations"""
    ).fetchone()
    versions = _membership_connection.execute(
        """SELECT version, COUNT(*) AS installations
           FROM cohort_telemetry_installations GROUP BY version ORDER BY installations DESC"""
    ).fetchall()
    return {
        "self_hosted_installations_total": row["total_installations"],
        "self_hosted_installations_active_30d": row["active_30d"],
        "versions": [{"version": item["version"], "installations": item["installations"]} for item in versions],
        "note": "Opt-in, self-reported installation telemetry; not a count of unique people or customers.",
    }


@app.get("/api/overview")
def overview(context: dict[str, Any] = Depends(require_workspace_read_access)):
    events = workspace.get_events()
    leases = workspace._workspace._lease_manager.get_active_leases()
    successful = sum(e.action == "workspace.commit" for e in events)
    conflicts = sum(e.action == "coordination.conflict" for e in events)
    return {
        "head": workspace.head.version_id,
        "key_count": len(workspace.head.entries),
        "snapshot_count": workspace.get_snapshot_count(),
        "event_count": workspace.get_event_count(),
        "active_lease_count": len(leases),
        "commit_count": successful,
        "conflict_count": conflicts,
        "branches": len(_branches),
        "sample_data": False,
    }


@app.get("/api/branches")
def list_branches(context: dict[str, Any] = Depends(require_workspace_read_access)):
    with _lock:
        return {"items": [_branch_summary(k, v) for k, v in _branches.items() if _agent_owns(context, v["agent_id"])]}


@app.get("/api/branches/{branch_id}/diff")
def get_branch_diff(branch_id: str, context: dict[str, Any] = Depends(require_workspace_read_access)):
    """Preview every key changed by a branch against its base and current HEAD."""
    with _lock:
        item = _branches.get(branch_id)
        if not item or not _agent_owns(context, item["agent_id"]):
            raise HTTPException(404, "Branch not found")
        branch = item["branch"]
        base = workspace._workspace._snapshots.get(branch.base_version)
        if base is None:
            raise HTTPException(409, "Branch base snapshot is no longer available")

        base_data = base.data
        branch_data = branch.current_data
        head_data = workspace.head.data
        changed_keys = sorted(
            key
            for key in set(base_data) | set(branch_data)
            if (key in base_data) != (key in branch_data)
            or base_data.get(key) != branch_data.get(key)
        )
        changes = []
        for key in changed_keys:
            base_present = key in base_data
            branch_present = key in branch_data
            head_present = key in head_data
            changes.append({
                "key": key,
                "base": base_data.get(key),
                "proposed": branch_data.get(key),
                "head": head_data.get(key),
                "base_present": base_present,
                "proposed_present": branch_present,
                "head_present": head_present,
                "head_changed_since_base": (
                    base_present != head_present
                    or base_data.get(key) != head_data.get(key)
                ),
                "intent": branch.intents.get(key),
            })

        return {
            "branch": _branch_summary(branch_id, item),
            "base_version": base.version_id,
            "head_version": workspace.head.version_id,
            "change_count": len(changes),
            "changes": changes,
        }


@app.post("/api/branches", status_code=201)
def create_branch(body: BranchCreate, context: dict[str, Any] = Depends(require_workspace_write_access)):
    _require_agent_identity(context, body.agent_id)
    branch_id = str(uuid.uuid4())
    try:
        branch = workspace.create_branch(body.agent_id, body.name)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    with _lock:
        _branches[branch_id] = {
            "branch": branch,
            "agent_id": body.agent_id,
            "status": "open",
            "conflicting_keys": [],
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _save_api_branch(branch_id, _branches[branch_id])
    return _branch_summary(branch_id, _branches[branch_id])


@app.put("/api/branches/{branch_id}/state")
def write_branch(branch_id: str, body: BranchWrite, context: dict[str, Any] = Depends(require_workspace_write_access)):
    with _lock:
        item = _branches.get(branch_id)
        if not item or item["status"] != "open" or not _agent_owns(context, item["agent_id"]):
            raise HTTPException(404, "Open branch not found")
        try:
            # PersistentWorkspace replays its runtime after lease mutations.
            # Rebind the branch so writes validate against the current lease manager.
            item["branch"]._lease_manager = workspace._workspace._lease_manager
            item["branch"].set(body.key, body.value, intent=body.intent)
            _save_api_branch(branch_id, item)
        except (LeaseError, ValueError) as exc:
            raise HTTPException(409, detail={"message": str(exc), "code": getattr(exc, "code", "write_error")}) from exc
    return {"ok": True, "branch_id": branch_id, "key": body.key}


@app.post("/api/branches/{branch_id}/commit")
def commit_branch(
    branch_id: str,
    context: dict[str, Any] = Depends(require_workspace_write_access),
    body: CommitRequest | None = None,
):
    with _lock:
        item = _branches.get(branch_id)
        if not item or item["status"] != "open" or not _agent_owns(context, item["agent_id"]):
            raise HTTPException(404, "Open branch not found")
        try:
            snapshot = workspace.commit(
                item["agent_id"], item["branch"],
                expected_head_version=body.expected_head_version if body else None,
            )
            item["status"] = "merged"
            item["conflicting_keys"] = []
            _save_api_branch(branch_id, item)
            return {"ok": True, "head": snapshot.version_id, "branch": _branch_summary(branch_id, item)}
        except ConflictError as exc:
            item["conflicting_keys"] = list(exc.conflicting_keys)
            _save_api_branch(branch_id, item)
            return {
                "ok": False,
                "conflict": True,
                "branch": _branch_summary(branch_id, item),
                "conflicting_keys": list(exc.conflicting_keys),
                "head_version": exc.details.get("head_version"),
                "base_version": exc.details.get("base_version"),
                "error_code": exc.code,
                "expected_head_version": exc.details.get("expected_head_version"),
                "actual_head_version": exc.details.get("actual_head_version"),
            }


@app.get("/api/branches/{branch_id}/conflict")
def get_branch_conflict(branch_id: str, context: dict[str, Any] = Depends(require_workspace_read_access)):
    with _lock:
        item = _branches.get(branch_id)
        if not item or item["status"] != "open" or not _agent_owns(context, item["agent_id"]):
            raise HTTPException(404, "Open branch not found")
        keys = item.get("conflicting_keys", [])
        if not keys:
            raise HTTPException(409, "Branch has no recorded conflict")
        base = workspace._workspace._snapshots.get(item["branch"].base_version)
        if base is None:
            raise HTTPException(409, "Branch base snapshot is no longer available")
        head = workspace.head
        branch_data = item["branch"].current_data
        base_data = base.data
        head_data = head.data
        conflicts = []
        for key in keys:
            tokens = Workspace._conflict_path_tokens(key)
            base_value = Workspace._read_conflict_path(base_data, tokens)
            head_value = Workspace._read_conflict_path(head_data, tokens)
            branch_value = Workspace._read_conflict_path(branch_data, tokens)
            conflicts.append({
                "key": key,
                "base": None if base_value is _MISSING else base_value,
                "head": None if head_value is _MISSING else head_value,
                "branch": None if branch_value is _MISSING else branch_value,
                "base_present": base_value is not _MISSING,
                "head_present": head_value is not _MISSING,
                "branch_present": branch_value is not _MISSING,
            })
        return {
            "branch_id": branch_id,
            "base_version": base.version_id,
            "head_version": head.version_id,
            "conflicts": conflicts,
        }


@app.post("/api/branches/{branch_id}/resolve")
def resolve_branch(branch_id: str, body: ResolveRequest, context: dict[str, Any] = Depends(require_workspace_write_access)):
    with _lock:
        item = _branches.get(branch_id)
        if not item or item["status"] != "open" or not _agent_owns(context, item["agent_id"]):
            raise HTTPException(404, "Open branch not found")
        if not item.get("conflicting_keys"):
            raise HTTPException(409, "Branch has no recorded conflict")
        if body.strategy == "PreferHead":
            resolver = PreferHead()
        elif body.strategy == "PreferBranch":
            resolver = PreferBranch()
        else:
            if not body.priority:
                raise HTTPException(422, "priority is required for PreferAgent")
            resolver = PreferAgent(body.priority)
        # Keep the user rationale in the event metadata through a small resolver wrapper.
        class AuditedResolver:
            name = resolver.name
            def resolve(self, conflict):
                decision = resolver.resolve(conflict)
                if decision is None:
                    return None
                return Resolution(
                    present=decision.present,
                    value=decision.value,
                    rationale=body.rationale,
                )
        try:
            snapshot = workspace.commit(item["agent_id"], item["branch"], resolver=AuditedResolver())
            item["status"] = "merged"
            item["conflicting_keys"] = []
            _save_api_branch(branch_id, item)
            return {"ok": True, "head": snapshot.version_id, "branch": _branch_summary(branch_id, item)}
        except ConflictError as exc:
            item["conflicting_keys"] = list(exc.conflicting_keys)
            _save_api_branch(branch_id, item)
            raise HTTPException(409, detail={"message": str(exc), "conflicting_keys": list(exc.conflicting_keys)}) from exc


@app.get("/api/state")
def get_state(context: dict[str, Any] = Depends(require_workspace_read_access)):
    head = workspace.head
    return {
        "head": head.version_id,
        "parent": head.parent_version,
        "items": [
            {"key": key, "value_hash": digest, "value": workspace.store.get(digest)}
            for key, digest in head.entries.items()
        ],
    }


@app.get("/api/events")
def get_events(limit: int = 100, context: dict[str, Any] = Depends(require_workspace_read_access)):
    if limit < 1 or limit > 1000:
        raise HTTPException(422, "limit must be between 1 and 1000")
    events = workspace.get_events()
    # Agent credentials are scoped to one agent identity. Event payloads can
    # contain tool arguments and state changes, so do not expose other agents'
    # event details through the shared workspace read scope.
    if context.get("principal_type") == "agent":
        events = [event for event in events if event.agent_id == context.get("agent_id")]
    return {"items": [_event_dict(e) for e in events[-limit:]][::-1]}


@app.get("/api/leases")
def get_leases(context: dict[str, Any] = Depends(require_lease_read_access)):
    leases = workspace._workspace._lease_manager.get_active_leases()
    return {
        "items": [
            {
                "key": lease.resource_key,
                "agent_id": lease.agent_id,
                "expires_at": lease.expires_at,
                "acquired_at": lease.acquired_at,
                "fencing_token": lease.fencing_token,
            }
            for lease in leases.values()
            if _agent_owns(context, lease.agent_id)
        ]
    }


@app.post("/api/leases", status_code=201)
def acquire_lease(body: LeaseRequest, context: dict[str, Any] = Depends(require_lease_write_access)):
    _require_agent_identity(context, body.agent_id)
    try:
        lease = workspace.acquire_lease(body.agent_id, body.key, body.duration_seconds)
    except LeaseError as exc:
        raise HTTPException(409, detail={"message": str(exc), "code": exc.code, "details": exc.details}) from exc
    return {
        "key": lease.resource_key,
        "agent_id": lease.agent_id,
        "expires_at": lease.expires_at,
        "acquired_at": lease.acquired_at,
        "fencing_token": lease.fencing_token,
    }


@app.delete("/api/leases/{key:path}")
def release_lease(key: str, agent_id: str, context: dict[str, Any] = Depends(require_lease_write_access)):
    _require_agent_identity(context, agent_id)
    try:
        workspace.release_lease(agent_id, key)
    except LeaseError as exc:
        raise HTTPException(409, detail={"message": str(exc), "code": exc.code}) from exc
    return {"ok": True}


@app.get("/api/settings", dependencies=[Depends(require_workspace_member)])
def get_settings():
    return {
        "require_leases": workspace._workspace._require_leases,
        "persistence": "postgresql" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite",
        "database_path": "managed" if DATABASE_URL.startswith(("postgres://", "postgresql://")) else str(DB_PATH),
        "multi_process": False,
        "database_transactions": DATABASE_URL.startswith(("postgres://", "postgresql://")),
        "authentication": "supabase" if DATABASE_URL else "development-bypass",
    }


def main():
    """Run the local Cohort HTTP API."""
    import uvicorn
    uvicorn.run(
        "cerbere_cohort.api:app",
        host=os.getenv("COHORT_API_HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", os.getenv("COHORT_API_PORT", "8000"))),
        reload=False,
    )


class InvitationCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["admin", "member", "viewer"] = "member"
    expires_in_days: int = Field(default=7, ge=1, le=30)

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if not re.fullmatch(r"[^\\s@]+@[^\\s@]+\\.[^\\s@]+", value):
            raise ValueError("A valid email address is required")
        return value


class InvitationAccept(BaseModel):
    token: str = Field(min_length=32, max_length=256)


def _require_admin(context: dict[str, Any]) -> None:
    if context.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Organization admin permission required")


def _invitation_db():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Organization invitations require PostgreSQL")
    return workspace._conn


@app.post("/api/organizations/invitations", status_code=201)
def create_organization_invitation(
    body: InvitationCreate,
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=body.expires_in_days)
    try:
        row = _invitation_db().execute(
            """INSERT INTO public.cohort_invitations
               (organization_id, email, role, token_hash, invited_by, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)
               RETURNING id, email, role, expires_at, created_at""",
            (context["organization_id"], body.email, body.role, token_hash,
             context["user_id"], expires_at.isoformat()),
        ).fetchone()
        workspace._conn.commit()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Unable to create organization invitation") from exc
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "role": row["role"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "token": token,
    }


@app.get("/api/organizations/invitations")
def list_organization_invitations(
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    rows = _invitation_db().execute(
        """SELECT id, email, role, expires_at, accepted_at, revoked_at, created_at
           FROM public.cohort_invitations
           WHERE organization_id = ? ORDER BY created_at DESC LIMIT 200""",
        (context["organization_id"],),
    ).fetchall()
    return {"items": [
        {
            "id": str(row["id"]), "email": row["email"], "role": row["role"],
            "expires_at": row["expires_at"], "accepted_at": row["accepted_at"],
            "revoked_at": row["revoked_at"], "created_at": row["created_at"],
        } for row in rows
    ]}


@app.delete("/api/organizations/invitations/{invitation_id}")
def revoke_organization_invitation(
    invitation_id: UUID,
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    row = _invitation_db().execute(
        """UPDATE public.cohort_invitations SET revoked_at = now()
           WHERE id = ? AND organization_id = ? AND accepted_at IS NULL
             AND revoked_at IS NULL AND expires_at > now()
           RETURNING id""",
        (str(invitation_id), context["organization_id"]),
    ).fetchone()
    workspace._conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Active invitation not found")
    return {"revoked": True, "id": str(invitation_id)}


@app.post("/api/organizations/invitations/accept")
def accept_organization_invitation(
    body: InvitationAccept,
    authorization: str | None = Header(default=None),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer access token required")
    claims = _decode_access_token(authorization[7:].strip())
    user_id = str(UUID(str(claims["sub"])))
    email = str(claims.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(status_code=403, detail="An account email is required")
    token_hash = hashlib.sha256(body.token.encode("utf-8")).hexdigest()
    try:
        row = _invitation_db().execute(
            """WITH eligible_user AS (
                 SELECT id FROM auth.users
                 WHERE id = ? AND lower(email) = ? AND email_confirmed_at IS NOT NULL
               ), accepted AS (
                 UPDATE public.cohort_invitations
                 SET accepted_at = now(), accepted_by = ?
                 WHERE token_hash = ? AND lower(email) = ? AND accepted_at IS NULL
                   AND revoked_at IS NULL AND expires_at > now()
                   AND EXISTS (SELECT 1 FROM eligible_user)
                 RETURNING organization_id, role
               )
               INSERT INTO public.cohort_memberships
                 (organization_id, user_id, role, status)
               SELECT organization_id, ?, role, 'active' FROM accepted
               ON CONFLICT (organization_id, user_id)
               DO UPDATE SET role = EXCLUDED.role, status = 'active'
               RETURNING organization_id, role""",
            (user_id, email, user_id, token_hash, email, user_id),
        ).fetchone()
        workspace._conn.commit()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Unable to accept organization invitation") from exc
    if not row:
        raise HTTPException(status_code=404, detail="Invitation is invalid, expired, revoked, or the account email is not confirmed")
    return {"accepted": True, "organization_id": str(row["organization_id"]), "role": row["role"]}


AGENT_SCOPES = {
    "workspace:read", "workspace:write", "leases:read", "leases:write"
}


class AgentCredentialCreate(BaseModel):
    agent_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(
        default_factory=lambda: ["workspace:read", "workspace:write", "leases:read", "leases:write"],
        min_length=1,
        max_length=4,
    )
    expires_in_days: int | None = Field(default=None, ge=1, le=365)

    @field_validator("agent_id", "name")
    @classmethod
    def _trim_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Value cannot be blank")
        return value

    @field_validator("scopes")
    @classmethod
    def _valid_scopes(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or not set(value).issubset(AGENT_SCOPES):
            raise ValueError("Scopes must be unique supported Cohort scopes")
        return value


def _agent_credentials_db():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Agent credentials require PostgreSQL")
    return workspace._conn


@app.post("/api/agents/credentials", status_code=201)
def create_agent_credential(
    body: AgentCredentialCreate,
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    token = "coh_" + secrets.token_urlsafe(40)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None else None
    )
    try:
        row = _agent_credentials_db().execute(
            """INSERT INTO public.cohort_agent_credentials
               (organization_id, agent_id, name, token_hash, scopes, created_by, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               RETURNING id, organization_id, agent_id, name, scopes, expires_at, created_at""",
            (context["organization_id"], body.agent_id, body.name, token_hash,
             body.scopes, context["user_id"], expires_at.isoformat() if expires_at else None),
        ).fetchone()
        workspace._conn.commit()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Unable to create agent credential") from exc
    return {
        "id": str(row["id"]), "agent_id": row["agent_id"], "name": row["name"],
        "scopes": list(row["scopes"]), "expires_at": row["expires_at"],
        "created_at": row["created_at"], "token": token,
    }


@app.get("/api/agents/credentials")
def list_agent_credentials(
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    rows = _agent_credentials_db().execute(
        """SELECT id, agent_id, name, scopes, expires_at, last_used_at, revoked_at, created_at
           FROM public.cohort_agent_credentials
           WHERE organization_id = ? ORDER BY created_at DESC LIMIT 200""",
        (context["organization_id"],),
    ).fetchall()
    return {"items": [
        {
            "id": str(row["id"]), "agent_id": row["agent_id"], "name": row["name"],
            "scopes": list(row["scopes"]), "expires_at": row["expires_at"],
            "last_used_at": row["last_used_at"], "revoked_at": row["revoked_at"],
            "created_at": row["created_at"],
        } for row in rows
    ]}


@app.delete("/api/agents/credentials/{credential_id}")
def revoke_agent_credential(
    credential_id: UUID,
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    row = _agent_credentials_db().execute(
        """UPDATE public.cohort_agent_credentials SET revoked_at = now()
           WHERE id = ? AND organization_id = ? AND revoked_at IS NULL
           RETURNING id""",
        (str(credential_id), context["organization_id"]),
    ).fetchone()
    workspace._conn.commit()
    if not row:
        raise HTTPException(status_code=404, detail="Active agent credential not found")
    return {"revoked": True, "id": str(credential_id)}
