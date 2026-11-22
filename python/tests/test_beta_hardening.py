"""Beta hardening: membership cache, Postgres reconnect, fail-closed startup, payload bounds."""
import os
import subprocess
import sys
import types

import pytest

from cerbere_cohort import auth
from cerbere_cohort.postgres import _Connection

ORG = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"


def _fake_psycopg(counter):
    class Result:
        def fetchone(self):
            return ("member",)

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            return Result()

    def connect(*args, **kwargs):
        counter["n"] += 1
        return Conn()

    return types.SimpleNamespace(connect=connect)


class _MembershipCursor:
    def fetchone(self):
        return ("member",)


class _MembershipDb:
    def __init__(self):
        self.calls = 0

    def execute(self, sql, params=()):
        self.calls += 1
        assert "public.cohort_memberships" in sql
        assert params == (ORG, USER)
        return _MembershipCursor()


def test_membership_lookup_is_cached(monkeypatch):
    db = _MembershipDb()
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("COHORT_ORGANIZATION_ID", ORG)
    monkeypatch.setattr(auth, "_decode_access_token", lambda token: {"sub": USER, "email": "a@b.co"})
    monkeypatch.setattr(auth, "_membership_cache", {})
    monkeypatch.setattr(auth, "_membership_db", db)

    for _ in range(3):
        ctx = auth.require_workspace_member(authorization="Bearer t", x_organization_id=ORG)

    assert ctx["role"] == "member" and ctx["user_id"] == USER
    assert db.calls == 1, "membership must be looked up once within the TTL"


def test_membership_cache_expires(monkeypatch):
    db = _MembershipDb()
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("COHORT_ORGANIZATION_ID", ORG)
    monkeypatch.setattr(auth, "_decode_access_token", lambda token: {"sub": USER})
    monkeypatch.setattr(auth, "_membership_cache", {})
    monkeypatch.setattr(auth, "_membership_db", db)

    auth.require_workspace_member(authorization="Bearer t", x_organization_id=ORG)
    key = (ORG, USER)
    auth._membership_cache[key] = (0.0, "member")  # already expired
    auth.require_workspace_member(authorization="Bearer t", x_organization_id=ORG)

    assert db.calls == 2


class _FakeDb:
    def __init__(self, fail_times, errors):
        self.fail_times, self.errors, self.closed, self.broken, self.calls = fail_times, errors, False, False, 0

    def execute(self, sql, params=()):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.errors.OperationalError("server closed the connection unexpectedly")
        return "cursor"

    def close(self):
        self.closed = True

    def commit(self):
        pass

    def rollback(self):
        pass


def _connection(fail_times):
    errors = types.SimpleNamespace(OperationalError=type("OperationalError", (Exception,), {}),
                                   InterfaceError=type("InterfaceError", (Exception,), {}))
    conn = object.__new__(_Connection)
    import threading
    conn._psycopg, conn._lock, conn._in_txn = errors, threading.RLock(), False
    conn._transaction_lock_held = False
    conn._db = _FakeDb(fail_times, errors)
    conn._connect = lambda: _FakeDb(0, errors)
    return conn, errors


def test_postgres_reconnects_after_dropped_connection():
    conn, _ = _connection(fail_times=1)
    first = conn._db
    assert conn.execute("SELECT 1") == "cursor"
    assert first.closed and conn._db is not first


def test_postgres_connection_lock_is_held_until_transaction_finishes():
    import threading

    conn, _ = _connection(fail_times=0)
    transaction_started = threading.Event()
    allow_commit = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()

    def transaction():
        conn.execute("BEGIN IMMEDIATE")
        transaction_started.set()
        assert allow_commit.wait(timeout=2)
        conn.commit()

    def concurrent_request():
        assert transaction_started.wait(timeout=2)
        second_started.set()
        conn.execute("SELECT 1")
        second_finished.set()

    first = threading.Thread(target=transaction)
    second = threading.Thread(target=concurrent_request)
    first.start()
    second.start()
    assert second_started.wait(timeout=2)
    assert not second_finished.wait(timeout=0.05)
    allow_commit.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert second_finished.is_set()


def test_postgres_does_not_replay_inside_transaction():
    conn, errors = _connection(fail_times=1)
    conn._in_txn = True
    with pytest.raises(errors.OperationalError):
        conn.execute("INSERT INTO events VALUES (?)", ("x",))
    conn.rollback()
    assert conn._in_txn is False


def test_hosted_deployment_refuses_to_start_without_database():
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "COHORT_DB_PATH"}}
    env.update({"RENDER": "true", "COHORT_ENV": "production"})
    proc = subprocess.run([sys.executable, "-c", "import cerbere_cohort.api"], env=env,
                          capture_output=True, text=True)
    assert proc.returncode != 0
    assert "DATABASE_URL is required" in proc.stderr


def test_branch_value_is_bounded():
    from cerbere_cohort.api import BranchWrite, MAX_VALUE_BYTES
    from pydantic import ValidationError
    assert BranchWrite(key="k", value={"ok": True}).value == {"ok": True}
    with pytest.raises(ValidationError):
        BranchWrite(key="k", value="x" * (MAX_VALUE_BYTES + 1))



def test_health_reports_safe_hosted_configuration(monkeypatch):
    from cerbere_cohort import api

    monkeypatch.setattr(api, "DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("COHORT_CORS_ORIGINS", "https://dashboard.example")
    result = api.health()

    assert result["authentication"] == "supabase"
    assert result["tenant_isolation"] == "postgresql_schema_per_organization"
    assert result["single_instance_required"] is True
    assert result["configuration"] == {
        "database_configured": True,
        "supabase_url_configured": True,
        "cors_origins_configured": True,
    }


def test_readiness_checks_database_and_hosted_configuration(monkeypatch):
    from cerbere_cohort import api

    class ReadyDatabase:
        query = ""

        def execute(self, sql, params=()):
            self.query = sql
            return self

        def fetchone(self):
            if "information_schema.columns" in self.query:
                return (len(api._REQUIRED_APPLICATION_SCHEMA_COLUMNS),)
            if "pg_trigger" in self.query:
                return (True,)
            return (1,)

    monkeypatch.setattr(api, "DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(api, "_membership_connection", ReadyDatabase())
    class JwksClient:
        def get_signing_keys(self):
            return [object()]

    monkeypatch.setattr(api, "_jwks_client", lambda: JwksClient())
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("COHORT_CORS_ORIGINS", "https://dashboard.example")

    assert api.ready() == {
        "ok": True,
        "checks": {
            "database": True,
            "membership_table": True,
            "application_schema": True,
            "onboarding_trigger": True,
            "supabase_url": True,
            "supabase_jwks": True,
            "cors_origins": True,
        },
    }


def test_readiness_fails_closed_when_supabase_or_database_is_unavailable(monkeypatch):
    from fastapi import HTTPException
    from cerbere_cohort import api

    class BrokenDatabase:
        def execute(self, sql):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(api, "DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(api, "_membership_connection", BrokenDatabase())
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("COHORT_CORS_ORIGINS", "https://dashboard.example")

    with pytest.raises(HTTPException) as exc:
        api.ready()

    assert exc.value.status_code == 503
    assert exc.value.detail["checks"] == {
        "database": False,
        "membership_table": False,
        "application_schema": False,
        "onboarding_trigger": False,
        "supabase_url": False,
        "supabase_jwks": False,
        "cors_origins": True,
    }


def test_readiness_fails_when_a_later_supabase_migration_is_missing(monkeypatch):
    from fastapi import HTTPException
    from cerbere_cohort import api

    class IncompleteDatabase:
        query = ""

        def execute(self, sql, params=()):
            self.query = sql
            return self

        def fetchone(self):
            if "information_schema.columns" in self.query:
                return (len(api._REQUIRED_APPLICATION_SCHEMA_COLUMNS) - 1,)
            if "pg_trigger" in self.query:
                return (True,)
            return (1,)

    monkeypatch.setattr(api, "DATABASE_URL", "postgresql://example")
    monkeypatch.setattr(api, "_membership_connection", IncompleteDatabase())
    monkeypatch.setattr(api, "_jwks_client", lambda: type("Jwks", (), {"get_signing_keys": lambda self: [object()]})())
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("COHORT_CORS_ORIGINS", "https://dashboard.example")

    with pytest.raises(HTTPException) as exc:
        api.ready()

    assert exc.value.status_code == 503
    assert exc.value.detail["checks"]["application_schema"] is False


def test_render_blueprint_enforces_leases_for_hosted_beta():
    from pathlib import Path

    blueprint = (Path(__file__).resolve().parents[2] / "render.yaml").read_text()
    assert "key: COHORT_REQUIRE_LEASES\n        value: \"true\"" in blueprint



def test_readiness_rejects_a_disabled_signup_trigger(monkeypatch):
    from cerbere_cohort import api

    class DisabledTriggerDatabase:
        def execute(self, sql, params=()):
            assert "tgenabled IN ('O', 'A')" in sql
            assert "auth.users" in sql
            return self

        def fetchone(self):
            return (False,)

    assert api._onboarding_trigger_ready(DisabledTriggerDatabase()) is False
