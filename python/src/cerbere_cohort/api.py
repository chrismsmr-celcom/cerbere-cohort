"""HTTP adapter for Cohort with request-scoped, schema-isolated workspaces.

Each authenticated request selects a workspace through X-Organization-ID;
membership is verified before its isolated runtime is bound to the request.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
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
from cerbere_cohort.events import event_actor
from cerbere_cohort.telemetry import send_installation_ping
from cerbere_cohort.observability import (
    encrypt_headers, decrypt_headers, normalize_otlp_endpoint,
    validate_headers, send_otlp_logs,
)
from cerbere_cohort.email import (
    EmailDeliveryError,
    build_workspace_invitation_url,
    send_workspace_invitation_email,
)
from cerbere_cohort.auth import (
    configure_membership_db,
    require_workspace_member,
    authenticate_agent_token,
    _decode_access_token,
    _jwks_client,
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
from cerbere_cohort.persistence import PersistentWorkspace, IdempotencyConflict
from cerbere_cohort.tasks import TaskCoordinator, TaskError, ensure_task_schema

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
    ensure_task_schema(workspace._conn)

def _save_api_branch(branch_id: str, item: dict[str, Any], *, commit: bool = True) -> None:
    b = item['branch']
    workspace._conn.execute("INSERT INTO api_branches (branch_id, agent_id, branch_name, base_version, current_data_json, intents_json, fencing_tokens_json, status, conflicting_keys_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(branch_id) DO UPDATE SET current_data_json=excluded.current_data_json, intents_json=excluded.intents_json, fencing_tokens_json=excluded.fencing_tokens_json, status=excluded.status, conflicting_keys_json=excluded.conflicting_keys_json", (branch_id, item['agent_id'], b.name, b.base_version, json.dumps(b.current_data, ensure_ascii=False), json.dumps(b.intents, ensure_ascii=False), json.dumps(b.fencing_tokens, ensure_ascii=False), item['status'], json.dumps(item.get('conflicting_keys', [])), item['created_at']))
    if commit:
        workspace._conn.commit()

def _restore_api_branches(*, clear: bool = False, commit: bool = True) -> None:
    if clear:
        for branch_id in list(_branches):
            del _branches[branch_id]
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
    if commit:
        workspace._conn.commit()


def _refresh_api_branches() -> None:
    """Refresh PostgreSQL workspace and branch registry across API processes."""
    if not DATABASE_URL:
        return
    workspace.refresh()
    _restore_api_branches(clear=True, commit=True)

if not DATABASE_URL:
    _restore_api_branches()

logger = logging.getLogger(__name__)

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
                 body.frameworks, organization_id),
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
                 body.frameworks),
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
                       jsonb_build_object('account_type', ?::text, 'email_domain', ?::text))""",
            (organization_id, user_id, organization_id, body.account_type,
             email.rsplit("@", 1)[-1] if "@" in email else None),
        )
        _membership_connection.commit()
    except Exception as exc:
        logger.exception("Workspace onboarding transaction failed")
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
            """SELECT o.id, o.name, m.role, m.status, o.account_type, o.logo_url, o.onboarding_completed_at
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



class WorkspaceProfileUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    legal_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    website: str | None = Field(default=None, max_length=240)
    industry: str | None = Field(default=None, max_length=80)
    country: str | None = Field(default=None, max_length=80)
    contact_email: str | None = Field(default=None, max_length=254)
    logo_url: str | None = Field(default=None, max_length=600)
    brand_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("name", "legal_name", "description", "website", "industry", "country", "contact_email", "logo_url")
    @classmethod
    def _trim_profile_text(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("website")
    @classmethod
    def _validate_website(cls, value: str | None) -> str | None:
        if value and not re.match(r"^https?://[^\s/]+(?:/[^\s]*)?$", value, re.IGNORECASE):
            raise ValueError("Website must be a valid HTTP or HTTPS URL")
        return value

    @field_validator("contact_email")
    @classmethod
    def _validate_contact_email(cls, value: str | None) -> str | None:
        if value and not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", value):
            raise ValueError("Enter a valid contact email")
        return value


@app.get("/api/workspace/profile")
def get_workspace_profile(context: dict[str, Any] = Depends(require_workspace_member)):
    organization_id = context["organization_id"]
    if not DATABASE_URL:
        return {"profile": {"id": organization_id, "name": "Local workspace"}}
    row = _membership_connection.execute(
        """SELECT id, name, legal_name, description, website, industry, country,
                  contact_email, logo_url, brand_color, account_type
           FROM public.cohort_organizations WHERE id = ?""",
        (organization_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"profile": {key: (str(row[key]) if key == "id" else row[key]) for key in
        ("id", "name", "legal_name", "description", "website", "industry", "country",
         "contact_email", "logo_url", "brand_color", "account_type")}}


@app.patch("/api/workspace/profile")
def update_workspace_profile(
    body: WorkspaceProfileUpdate,
    context: dict[str, Any] = Depends(require_workspace_member),
):
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Organization profiles require PostgreSQL")
    if context.get("role") not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Only workspace owners and admins can update the organization profile")
    organization_id = context["organization_id"]
    expected_logo_prefix = f"{os.getenv('SUPABASE_URL', '').rstrip('/')}/storage/v1/object/public/cohort-organization-logos/{organization_id}/"
    if body.logo_url and (not expected_logo_prefix.startswith("https://") or not body.logo_url.startswith(expected_logo_prefix)):
        raise HTTPException(status_code=422, detail="Logo must be uploaded to this workspace's organization logo storage")
    try:
        _membership_connection.execute(
            """UPDATE public.cohort_organizations
               SET name = ?, legal_name = ?, description = ?, website = ?, industry = ?,
                   country = ?, contact_email = ?, logo_url = ?, brand_color = ?,
                   updated_at = now()
               WHERE id = ?""",
            (body.name, body.legal_name, body.description, body.website, body.industry,
             body.country, body.contact_email, body.logo_url, body.brand_color, organization_id),
        )
        _membership_connection.execute(
            """INSERT INTO public.cohort_audit_logs
               (organization_id, actor_user_id, action, target_type, target_id, metadata)
               VALUES (?, ?, 'workspace.profile_updated', 'workspace', ?,
                       jsonb_build_object('fields', ?::jsonb))""",
            (organization_id, context["user_id"], organization_id,
             json.dumps(list(body.model_dump().keys()))),
        )
        _membership_connection.commit()
    except Exception as exc:
        _membership_connection.rollback()
        logger.exception("Workspace profile update failed")
        raise HTTPException(status_code=503, detail="Unable to update workspace profile") from exc
    return {"updated": True, "profile": {
        "id": organization_id, **body.model_dump()
    }}



_observability_worker_started = False
_observability_worker_lock = threading.Lock()


def _observability_export_loop() -> None:
    """Poll enabled workspace exporters and deliver newly committed events."""
    while True:
        try:
            configs = _membership_connection.execute(
                """SELECT organization_id, endpoint, encrypted_headers, last_event_id
                   FROM public.cohort_observability_exports
                   WHERE enabled = true AND next_attempt_at <= now()"""
            ).fetchall()
            for config in configs:
                organization_id = str(config["organization_id"])
                try:
                    runtime = get_runtime(organization_id)
                    events = runtime.workspace.get_events()
                    cursor = config["last_event_id"]
                    if not cursor:
                        # A newly configured destination starts from now, not from
                        # the entire historical workspace journal.
                        if events:
                            _membership_connection.execute(
                                """UPDATE public.cohort_observability_exports
                                   SET last_event_id = ?, updated_at = now()
                                   WHERE organization_id = ?""",
                                (events[-1].event_id, organization_id),
                            )
                            _membership_connection.commit()
                        continue
                    event_ids = [event.event_id for event in events]
                    try:
                        start = event_ids.index(cursor) + 1
                    except ValueError:
                        # The cursor is no longer in the retained journal. Do not
                        # replay an ambiguous history; move forward explicitly.
                        if events:
                            _membership_connection.execute(
                                """UPDATE public.cohort_observability_exports
                                   SET last_event_id = ?, last_error = ?, updated_at = now()
                                   WHERE organization_id = ?""",
                                (events[-1].event_id, "Export cursor not found; resumed at current head", organization_id),
                            )
                            _membership_connection.commit()
                        continue
                    batch = events[start:start + 100]
                    if not batch:
                        continue
                    serialized = [event.model_dump(mode="json") for event in batch]
                    send_otlp_logs(
                        config["endpoint"], decrypt_headers(config["encrypted_headers"]), serialized
                    )
                    _membership_connection.execute(
                        """UPDATE public.cohort_observability_exports
                           SET last_event_id = ?, last_success_at = now(),
                               last_error = NULL, next_attempt_at = now(), updated_at = now()
                           WHERE organization_id = ?""",
                        (batch[-1].event_id, organization_id),
                    )
                    _membership_connection.commit()
                except Exception as exc:
                    logger.warning(
                        "Cohort observability export failed for workspace %s (%s)",
                        organization_id, type(exc).__name__,
                    )
                    try:
                        _membership_connection.execute(
                            """UPDATE public.cohort_observability_exports
                               SET last_error = ?, next_attempt_at = now() + interval '15 seconds',
                                   updated_at = now()
                               WHERE organization_id = ?""",
                            (f"Delivery failed ({type(exc).__name__})", organization_id),
                        )
                        _membership_connection.commit()
                    except Exception:
                        logger.exception("Unable to record observability export failure")
        except Exception:
            logger.exception("Cohort observability export poll failed")
        threading.Event().wait(3)


@app.on_event("startup")
def startup_observability_exporter():
    global _observability_worker_started
    if not DATABASE_URL:
        return
    # Idempotent bootstrap; the matching Supabase migration also creates this
    # server-only table for managed deployments.
    _membership_connection.execute(
        """CREATE TABLE IF NOT EXISTS public.cohort_observability_exports (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            organization_id UUID NOT NULL UNIQUE
                REFERENCES public.cohort_organizations(id) ON DELETE CASCADE,
            provider TEXT NOT NULL CHECK (provider IN ('otlp_http')),
            endpoint TEXT NOT NULL,
            encrypted_headers BYTEA NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT true,
            last_event_id TEXT,
            last_success_at TIMESTAMPTZ,
            last_error TEXT,
            next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID REFERENCES auth.users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )"""
    )
    _membership_connection.commit()
    if not os.getenv("COHORT_EXPORT_ENCRYPTION_KEY", "").strip():
        logger.warning("OTLP exporter is inactive: COHORT_EXPORT_ENCRYPTION_KEY is not configured")
        return
    with _observability_worker_lock:
        if not _observability_worker_started:
            threading.Thread(
                target=_observability_export_loop,
                daemon=True,
                name="cohort-otlp-exporter",
            ).start()
            _observability_worker_started = True


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

class ObservabilityExportConfig(BaseModel):
    endpoint: str = Field(min_length=8, max_length=2048)
    headers: dict[str, str] | None = Field(default=None, max_length=10)

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        try:
            return normalize_otlp_endpoint(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("headers")
    @classmethod
    def _validate_export_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is None:
            return None
        try:
            return validate_headers(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc



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


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    payload: Any = Field(default_factory=dict)


class TaskClaimRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=120)
    lease_seconds: float = Field(default=60, ge=1, le=86400)


class TaskOwnerRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=120)
    fencing_token: int = Field(ge=1)


class TaskHeartbeatRequest(TaskOwnerRequest):
    lease_seconds: float = Field(default=60, ge=1, le=86400)


class TaskCompleteRequest(TaskOwnerRequest):
    result: Any
    idempotency_key: str = Field(min_length=1, max_length=200)


class TaskFailRequest(TaskOwnerRequest):
    error: str = Field(min_length=1, max_length=4000)


def _task_coordinator() -> TaskCoordinator:
    return TaskCoordinator(workspace._conn)


def _task_call(operation):
    try:
        return operation()
    except TaskError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc


@app.post("/api/tasks", status_code=201)
def create_task(
    body: TaskCreateRequest,
    context: dict[str, Any] = Depends(require_workspace_write_access),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 200):
        raise HTTPException(422, "Idempotency-Key must contain 1 to 200 characters")
    with _lock:
        return _task_call(lambda: _task_coordinator().create(body.title, body.payload, idempotency_key))


@app.get("/api/tasks")
def list_tasks(
    limit: int = 100,
    context: dict[str, Any] = Depends(require_workspace_read_access),
):
    with _lock:
        items = _task_call(lambda: _task_coordinator().list(limit))
        if context.get("principal_type") == "agent":
            items = [item for item in items if item["status"] == "pending" or item["assigned_agent_id"] == context.get("agent_id")]
        return {"items": items}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, context: dict[str, Any] = Depends(require_workspace_read_access)):
    with _lock:
        item = _task_call(lambda: _task_coordinator().get(task_id))
        if context.get("principal_type") == "agent" and item["status"] != "pending" and item["assigned_agent_id"] != context.get("agent_id"):
            raise HTTPException(404, "Task not found")
        return item


@app.get("/api/tasks/{task_id}/events")
def get_task_events(task_id: str, context: dict[str, Any] = Depends(require_workspace_read_access)):
    with _lock:
        item = _task_call(lambda: _task_coordinator().get(task_id))
        if context.get("principal_type") == "agent" and item["assigned_agent_id"] != context.get("agent_id"):
            raise HTTPException(404, "Task not found")
        return {"items": _task_call(lambda: _task_coordinator().events(task_id))}


@app.post("/api/tasks/{task_id}/claim")
def claim_task(
    task_id: str,
    body: TaskClaimRequest,
    context: dict[str, Any] = Depends(require_workspace_write_access),
):
    _require_agent_identity(context, body.agent_id)
    with _lock:
        return _task_call(lambda: _task_coordinator().claim(task_id, body.agent_id, body.lease_seconds))


@app.post("/api/tasks/{task_id}/start")
def start_task(
    task_id: str,
    body: TaskOwnerRequest,
    context: dict[str, Any] = Depends(require_workspace_write_access),
):
    _require_agent_identity(context, body.agent_id)
    with _lock:
        return _task_call(lambda: _task_coordinator().start(task_id, body.agent_id, body.fencing_token))


@app.post("/api/tasks/{task_id}/heartbeat")
def heartbeat_task(
    task_id: str,
    body: TaskHeartbeatRequest,
    context: dict[str, Any] = Depends(require_workspace_write_access),
):
    _require_agent_identity(context, body.agent_id)
    with _lock:
        return _task_call(lambda: _task_coordinator().heartbeat(
            task_id, body.agent_id, body.fencing_token, body.lease_seconds
        ))


@app.post("/api/tasks/{task_id}/complete")
def complete_task(
    task_id: str,
    body: TaskCompleteRequest,
    context: dict[str, Any] = Depends(require_workspace_write_access),
):
    _require_agent_identity(context, body.agent_id)
    with _lock:
        return _task_call(lambda: _task_coordinator().complete(
            task_id, body.agent_id, body.fencing_token, body.result, body.idempotency_key
        ))


@app.post("/api/tasks/{task_id}/fail")
def fail_task(
    task_id: str,
    body: TaskFailRequest,
    context: dict[str, Any] = Depends(require_workspace_write_access),
):
    _require_agent_identity(context, body.agent_id)
    with _lock:
        return _task_call(lambda: _task_coordinator().fail(
            task_id, body.agent_id, body.fencing_token, body.error
        ))


@app.get("/api/health")
def health():
    """Liveness and safe, non-secret deployment configuration diagnostics."""
    origins = [item.strip() for item in os.getenv("COHORT_CORS_ORIGINS", "").split(",") if item.strip()]
    hosted = bool(DATABASE_URL)
    return {
        "ok": True,
        "service": "cerbere-cohort",
        "runtime": "python",
        "persistence": "postgresql" if hosted else "sqlite",
        "branch_registry": "database",
        "authentication": "supabase" if hosted else "development-bypass",
        "configuration": {
            "database_configured": hosted,
            "supabase_url_configured": bool(os.getenv("SUPABASE_URL", "").strip()) if hosted else False,
            "cors_origins_configured": bool(origins) and "*" not in origins if hosted else True,
        },
        "tenant_isolation": "postgresql_schema_per_organization" if hosted else "local_sqlite",
        "single_instance_required": hosted,
    }


# Columns introduced by the ordered Supabase migrations. Readiness fails closed
# when any migration is missing or only partially applied.
_REQUIRED_APPLICATION_SCHEMA_COLUMNS = (
        ("cohort_organizations", "id"),
        ("cohort_organizations", "name"),
        ("cohort_organizations", "created_at"),
        ("cohort_organizations", "account_type"),
        ("cohort_organizations", "company_size"),
        ("cohort_organizations", "industry"),
        ("cohort_organizations", "primary_use_case"),
        ("cohort_organizations", "expected_agents"),
        ("cohort_organizations", "frameworks"),
        ("cohort_organizations", "onboarding_completed_at"),
        ("cohort_organizations", "legal_name"),
        ("cohort_organizations", "description"),
        ("cohort_organizations", "website"),
        ("cohort_organizations", "country"),
        ("cohort_organizations", "contact_email"),
        ("cohort_organizations", "logo_url"),
        ("cohort_organizations", "brand_color"),
        ("cohort_organizations", "updated_at"),
        ("cohort_memberships", "organization_id"),
        ("cohort_memberships", "user_id"),
        ("cohort_memberships", "role"),
        ("cohort_memberships", "status"),
        ("cohort_memberships", "created_at"),
        ("cohort_invitations", "id"),
        ("cohort_invitations", "organization_id"),
        ("cohort_invitations", "email"),
        ("cohort_invitations", "role"),
        ("cohort_invitations", "token_hash"),
        ("cohort_invitations", "invited_by"),
        ("cohort_invitations", "expires_at"),
        ("cohort_invitations", "accepted_at"),
        ("cohort_invitations", "accepted_by"),
        ("cohort_invitations", "revoked_at"),
        ("cohort_invitations", "created_at"),
        ("cohort_agent_credentials", "id"),
        ("cohort_agent_credentials", "organization_id"),
        ("cohort_agent_credentials", "agent_id"),
        ("cohort_agent_credentials", "name"),
        ("cohort_agent_credentials", "token_hash"),
        ("cohort_agent_credentials", "scopes"),
        ("cohort_agent_credentials", "created_by"),
        ("cohort_agent_credentials", "expires_at"),
        ("cohort_agent_credentials", "last_used_at"),
        ("cohort_agent_credentials", "revoked_at"),
        ("cohort_agent_credentials", "created_at"),
        ("cohort_permissions", "key"),
        ("cohort_permissions", "description"),
        ("cohort_roles", "id"),
        ("cohort_roles", "organization_id"),
        ("cohort_roles", "name"),
        ("cohort_roles", "is_system"),
        ("cohort_roles", "created_by"),
        ("cohort_roles", "created_at"),
        ("cohort_role_permissions", "role_id"),
        ("cohort_role_permissions", "permission_key"),
        ("cohort_audit_logs", "id"),
        ("cohort_audit_logs", "organization_id"),
        ("cohort_audit_logs", "actor_user_id"),
        ("cohort_audit_logs", "action"),
        ("cohort_audit_logs", "target_type"),
        ("cohort_audit_logs", "target_id"),
        ("cohort_audit_logs", "metadata"),
        ("cohort_audit_logs", "created_at"),
        ("cohort_domains", "id"),
        ("cohort_domains", "organization_id"),
        ("cohort_domains", "domain"),
        ("cohort_domains", "verified_at"),
        ("cohort_domains", "auto_join"),
        ("cohort_domains", "require_admin_approval"),
        ("cohort_domains", "created_at"),
        ("cohort_sso_configs", "id"),
        ("cohort_sso_configs", "organization_id"),
        ("cohort_sso_configs", "protocol"),
        ("cohort_sso_configs", "issuer"),
        ("cohort_sso_configs", "metadata_url"),
        ("cohort_sso_configs", "client_id"),
        ("cohort_sso_configs", "encrypted_client_secret"),
        ("cohort_sso_configs", "enabled"),
        ("cohort_sso_configs", "enforce_sso"),
        ("cohort_sso_configs", "created_at"),
        ("cohort_sso_configs", "updated_at"),
        ("cohort_user_profiles", "user_id"),
        ("cohort_user_profiles", "full_name"),
        ("cohort_user_profiles", "job_title"),
        ("cohort_user_profiles", "created_at"),
        ("cohort_user_profiles", "updated_at"),
)


def _application_schema_ready(connection) -> bool:
    values = ", ".join(["(?, ?)"] * len(_REQUIRED_APPLICATION_SCHEMA_COLUMNS))
    params = tuple(value for pair in _REQUIRED_APPLICATION_SCHEMA_COLUMNS for value in pair)
    row = connection.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = 'public' AND (table_name, column_name) IN (" + values + ")",
        params,
    ).fetchone()
    return bool(row and int(row[0]) == len(_REQUIRED_APPLICATION_SCHEMA_COLUMNS))


def _onboarding_trigger_ready(connection) -> bool:
    row = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_trigger "
        "WHERE tgname = ? AND tgrelid = 'auth.users'::regclass "
        "AND tgenabled IN ('O', 'A') "
        "AND (tgtype & 4) = 4 AND (tgtype & 1) = 1 "
        "AND NOT tgisinternal)",
        ("cohort_auth_user_workspace_bootstrap",),
    ).fetchone()
    return bool(row and row[0])


@app.get("/api/ready")
def ready():
    """Readiness check: verify database connectivity and required hosted config."""
    hosted = bool(DATABASE_URL)
    origins = [item.strip() for item in os.getenv("COHORT_CORS_ORIGINS", "").split(",") if item.strip()]
    checks = {
        "database": False,
        "membership_table": not hosted,
        "application_schema": not hosted,
        "onboarding_trigger": not hosted,
        "supabase_url": not hosted or bool(os.getenv("SUPABASE_URL", "").strip()),
        "supabase_jwks": not hosted,
        "cors_origins": not hosted or (bool(origins) and "*" not in origins),
    }
    try:
        _membership_connection.execute("SELECT 1").fetchone()
        checks["database"] = True
    except Exception:
        logger.exception("Readiness database check failed")
    if hosted and checks["database"]:
        try:
            _membership_connection.execute(
                "SELECT 1 FROM public.cohort_memberships LIMIT 0"
            ).fetchone()
            checks["membership_table"] = True
        except Exception:
            logger.exception("Readiness membership schema check failed")
        try:
            checks["application_schema"] = _application_schema_ready(_membership_connection)
        except Exception:
            logger.exception("Readiness application schema check failed")
        try:
            checks["onboarding_trigger"] = _onboarding_trigger_ready(_membership_connection)
        except Exception:
            logger.exception("Readiness onboarding trigger check failed")
    if hosted and checks["supabase_url"]:
        try:
            if not _jwks_client().get_signing_keys():
                raise RuntimeError("Supabase JWKS endpoint returned no signing keys")
            checks["supabase_jwks"] = True
        except Exception:
            logger.exception("Readiness Supabase JWKS check failed")
    if not all(checks.values()):
        raise HTTPException(
            status_code=503,
            detail={"message": "Cohort is not ready", "checks": checks},
        )
    return {"ok": True, "checks": checks}


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
        with _lock:
            with event_actor(context):
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
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if idempotency_key is not None:
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise HTTPException(422, "Idempotency-Key must contain 1 to 200 characters")
        request_hash = hashlib.sha256(json.dumps({
            "branch_id": branch_id,
            "agent_id": context.get("agent_id") or _branches.get(branch_id, {}).get("agent_id"),
            "expected_head_version": body.expected_head_version if body else None,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

        def commit_once():
            # Reload the durable branch row after the DB lock and latest replay.
            row = workspace._conn.execute(
                "SELECT * FROM api_branches WHERE branch_id = ?", (branch_id,)
            ).fetchone()
            if not row or row["status"] != "open" or not _agent_owns(context, row["agent_id"]):
                raise HTTPException(404, "Open branch not found")
            base = workspace._workspace._snapshots.get(row["base_version"])
            if base is None:
                raise HTTPException(409, "Branch base snapshot is no longer available")
            branch = Branch(
                row["branch_name"], base, workspace._workspace._lease_manager,
                row["agent_id"], workspace._workspace._require_leases,
            )
            branch.current_data = json.loads(row["current_data_json"])
            branch.intents = json.loads(row["intents_json"])
            branch.fencing_tokens = json.loads(row["fencing_tokens_json"] or "{}")
            item = {
                "branch": branch, "agent_id": row["agent_id"], "status": row["status"],
                "conflicting_keys": json.loads(row["conflicting_keys_json"]),
                "created_at": row["created_at"],
            }
            _branches[branch_id] = item
            try:
                with event_actor(context):
                    snapshot = workspace._workspace.commit(
                        item["agent_id"], branch,
                        expected_head_version=body.expected_head_version if body else None,
                    )
                item["status"] = "merged"
                item["conflicting_keys"] = []
                _save_api_branch(branch_id, item, commit=False)
                return {"ok": True, "head": snapshot.version_id, "branch": _branch_summary(branch_id, item)}
            except ConflictError as exc:
                item["conflicting_keys"] = list(exc.conflicting_keys)
                _save_api_branch(branch_id, item, commit=False)
                return {
                    "ok": False, "conflict": True, "branch": _branch_summary(branch_id, item),
                    "conflicting_keys": list(exc.conflicting_keys),
                    "head_version": exc.details.get("head_version"),
                    "base_version": exc.details.get("base_version"),
                    "error_code": exc.code,
                    "expected_head_version": exc.details.get("expected_head_version"),
                    "actual_head_version": exc.details.get("actual_head_version"),
                }

        try:
            with _lock:
                return workspace.execute_idempotent(
                    f"branch:{branch_id}:commit", key, request_hash, commit_once
                )
        except IdempotencyConflict as exc:
            raise HTTPException(409, detail={"code": "idempotency_key_reused", "message": str(exc)}) from exc

    with _lock:
        item = _branches.get(branch_id)
        if not item or item["status"] != "open" or not _agent_owns(context, item["agent_id"]):
            raise HTTPException(404, "Open branch not found")
        try:
            with event_actor(context):
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
                "ok": False, "conflict": True, "branch": _branch_summary(branch_id, item),
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
            with event_actor(context):
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
        with _lock:
            with event_actor(context):
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
        with _lock:
            with event_actor(context):
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
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
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
    # Invitations and memberships live in the shared public schema. Acceptance
    # happens before the invitee has an active workspace context, so this must
    # use the global membership connection rather than WorkspaceProxy._conn.
    return _membership_connection


def _observability_db():
    if not DATABASE_URL:
        raise HTTPException(status_code=503, detail="Observability exports require PostgreSQL")
    if not os.getenv("COHORT_EXPORT_ENCRYPTION_KEY", "").strip():
        raise HTTPException(status_code=503, detail="Observability encryption is not configured on this deployment")
    return _membership_connection


@app.get("/api/observability/export")
def get_observability_export(
    context: dict[str, Any] = Depends(require_workspace_member),
):
    row = _observability_db().execute(
        """SELECT provider, endpoint, enabled, last_success_at, last_error,
                  encrypted_headers
           FROM public.cohort_observability_exports WHERE organization_id = ?""",
        (context["organization_id"],),
    ).fetchone()
    if not row:
        return {"configured": False, "provider": "otlp_http", "enabled": False}
    return {
        "configured": True,
        "provider": row["provider"],
        "endpoint": row["endpoint"],
        "enabled": row["enabled"],
        "has_credentials": bool(row["encrypted_headers"]),
        "last_success_at": row["last_success_at"].isoformat() if row["last_success_at"] else None,
        "last_error": row["last_error"],
    }


@app.put("/api/observability/export")
def put_observability_export(
    body: ObservabilityExportConfig,
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    db = _observability_db()
    try:
        existing = db.execute(
            """SELECT encrypted_headers FROM public.cohort_observability_exports
               WHERE organization_id = ?""",
            (context["organization_id"],),
        ).fetchone()
        if body.headers is None and existing:
            encrypted = existing["encrypted_headers"]
        else:
            encrypted = encrypt_headers(body.headers or {})
        current_events = workspace.get_events()
        cursor = current_events[-1].event_id if current_events else None
        db.execute(
            """INSERT INTO public.cohort_observability_exports
               (organization_id, provider, endpoint, encrypted_headers, enabled,
                last_event_id, created_by, created_at, updated_at)
               VALUES (?, 'otlp_http', ?, ?, true, ?, ?, now(), now())
               ON CONFLICT (organization_id) DO UPDATE SET
                 provider = EXCLUDED.provider,
                 endpoint = EXCLUDED.endpoint,
                 encrypted_headers = EXCLUDED.encrypted_headers,
                 enabled = true,
                 last_error = NULL,
                 updated_at = now()""",
            (context["organization_id"], body.endpoint, encrypted, cursor, context["user_id"]),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        if isinstance(exc, HTTPException):
            raise
        logger.exception("Unable to save observability export configuration")
        raise HTTPException(status_code=503, detail="Unable to save observability configuration") from exc
    return {"configured": True, "enabled": True, "provider": "otlp_http", "endpoint": body.endpoint}


@app.post("/api/observability/export/test")
def test_observability_export(
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    row = _observability_db().execute(
        """SELECT endpoint, encrypted_headers FROM public.cohort_observability_exports
           WHERE organization_id = ? AND enabled = true""",
        (context["organization_id"],),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Configure an enabled OTLP destination first")
    test_event = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "agent_id": "cohort-control-plane",
        "action": "cohort.export_test",
        "payload": {},
        "metadata": {"source": "observability_connection_test"},
        "schema_version": "1.0.0",
    }
    try:
        send_otlp_logs(row["endpoint"], decrypt_headers(row["encrypted_headers"]), [test_event])
    except Exception as exc:
        logger.warning("OTLP connection test failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"OTLP delivery failed ({type(exc).__name__})") from exc
    return {"ok": True, "message": "Test log delivered to the OTLP endpoint"}


@app.delete("/api/observability/export")
def delete_observability_export(
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    db = _observability_db()
    db.execute(
        "DELETE FROM public.cohort_observability_exports WHERE organization_id = ?",
        (context["organization_id"],),
    )
    db.commit()
    return {"deleted": True}


@app.get("/api/organizations/members")
def list_organization_members(
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    try:
        rows = _membership_connection.execute(
            """SELECT m.user_id, m.role, m.status, m.created_at,
                      u.email, p.full_name, p.job_title
               FROM public.cohort_memberships m
               LEFT JOIN auth.users u ON u.id = m.user_id
               LEFT JOIN public.cohort_user_profiles p ON p.user_id = m.user_id
               WHERE m.organization_id = ?
               ORDER BY m.created_at ASC LIMIT 500""",
            (context["organization_id"],),
        ).fetchall()
    except Exception as exc:
        logger.exception("Unable to list organization members")
        raise HTTPException(status_code=503, detail="Unable to list organization members") from exc
    return {"items": [
        {
            "user_id": str(row["user_id"]),
            "email": row["email"],
            "full_name": row["full_name"],
            "job_title": row["job_title"],
            "role": row["role"],
            "status": row["status"],
            "created_at": row["created_at"],
        } for row in rows
    ]}


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
        db = _invitation_db()
        organization = db.execute(
            "SELECT name FROM public.cohort_organizations WHERE id = ?",
            (context["organization_id"],),
        ).fetchone()
        workspace_name = str(organization["name"]) if organization else "your Cohort workspace"
        row = db.execute(
            """INSERT INTO public.cohort_invitations
               (organization_id, email, role, token_hash, invited_by, expires_at)
               VALUES (?, ?, ?, ?, ?, ?)
               RETURNING id, email, role, expires_at, created_at""",
            (context["organization_id"], body.email, body.role, token_hash,
             context["user_id"], expires_at.isoformat()),
        ).fetchone()
        db.commit()
    except Exception as exc:
        _invitation_db().rollback()
        logger.exception("Unable to create organization invitation")
        raise HTTPException(status_code=503, detail="Unable to create organization invitation") from exc

    try:
        invite_url = build_workspace_invitation_url(token)
        email_id = send_workspace_invitation_email(
            to_email=body.email,
            invite_url=invite_url,
            role=body.role,
            workspace_name=workspace_name,
        )
    except EmailDeliveryError as exc:
        # Email is a notification channel, not a prerequisite for workspace access.
        # Keep the invitation valid and return its one-time token so the admin can
        # copy the link or retry delivery without recreating the invitation.
        logger.warning("Workspace invitation email delivery failed: %s", str(exc))
        return {
            "id": str(row["id"]),
            "email": row["email"],
            "role": row["role"],
            "expires_at": row["expires_at"],
            "created_at": row["created_at"],
            "token": token,
            "email_sent": False,
            "email_error": "Email delivery failed. The invitation is still valid; copy the link or retry.",
        }

    return {
        "id": str(row["id"]),
        "email": row["email"],
        "role": row["role"],
        "expires_at": row["expires_at"],
        "created_at": row["created_at"],
        "token": token,
        "email_sent": True,
        "email_id": email_id,
    }


@app.post("/api/organizations/invitations/{invitation_id}/resend")
def resend_organization_invitation(
    invitation_id: UUID,
    context: dict[str, Any] = Depends(require_workspace_member),
):
    _require_admin(context)
    db = _invitation_db()
    row = db.execute(
        """SELECT i.id, i.email, i.role, i.token_hash, i.expires_at, o.name AS workspace_name
           FROM public.cohort_invitations i
           JOIN public.cohort_organizations o ON o.id = i.organization_id
           WHERE i.id = ? AND i.organization_id = ?
             AND i.accepted_at IS NULL AND i.revoked_at IS NULL""",
        (str(invitation_id), context["organization_id"]),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Invitation not found or no longer resendable")

    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    previous_hash = row["token_hash"]
    previous_expiry = row["expires_at"]
    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=7)
    try:
        db.execute(
            """UPDATE public.cohort_invitations
               SET token_hash = ?, expires_at = ?
               WHERE id = ? AND organization_id = ? AND accepted_at IS NULL AND revoked_at IS NULL""",
            (token_hash, expires_at.isoformat(), str(invitation_id), context["organization_id"]),
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception("Unable to rotate workspace invitation token")
        raise HTTPException(status_code=503, detail="Unable to prepare invitation resend") from exc

    try:
        invite_url = build_workspace_invitation_url(token)
        email_id = send_workspace_invitation_email(
            to_email=row["email"],
            invite_url=invite_url,
            role=row["role"],
            workspace_name=row["workspace_name"],
        )
    except EmailDeliveryError as exc:
        # Keep the newly rotated token valid and return it to the admin as a
        # manual-delivery fallback. Never make email delivery block access.
        logger.warning("Workspace invitation resend failed: %s", str(exc))
        return {
            "resent": False,
            "email": row["email"],
            "expires_at": expires_at.isoformat(),
            "token": token,
            "email_sent": False,
            "email_error": "Email delivery failed. The new invitation link is still valid; copy it below.",
        }

    return {
        "resent": True,
        "email": row["email"],
        "expires_at": expires_at.isoformat(),
        "token": token,
        "email_sent": True,
        "email_id": email_id,
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
    _invitation_db().commit()
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
        _invitation_db().commit()
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
