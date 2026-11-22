"""Real PostgreSQL integration tests; enabled by COHORT_TEST_DATABASE_URL in CI."""
import os
import threading
from pathlib import Path

import pytest

DATABASE_URL = os.getenv("COHORT_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="COHORT_TEST_DATABASE_URL is not configured")


@pytest.fixture(scope="module")
def migrated_database():
    import psycopg

    connection = psycopg.connect(DATABASE_URL, autocommit=True)
    connection.execute("CREATE SCHEMA IF NOT EXISTS auth")
    connection.execute("CREATE SCHEMA IF NOT EXISTS storage")
    connection.execute("CREATE TABLE IF NOT EXISTS auth.users (id UUID PRIMARY KEY, email TEXT)")
    connection.execute(
        "CREATE OR REPLACE FUNCTION auth.uid() RETURNS UUID "
        "LANGUAGE SQL STABLE AS $$ SELECT NULL::UUID $$"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS storage.buckets ("
        "id TEXT PRIMARY KEY, name TEXT NOT NULL, public BOOLEAN NOT NULL DEFAULT false, "
        "file_size_limit BIGINT, allowed_mime_types TEXT[])"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS storage.objects ("
        "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), bucket_id TEXT, name TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE OR REPLACE FUNCTION storage.foldername(object_name TEXT) RETURNS TEXT[] "
        "LANGUAGE SQL IMMUTABLE AS $$ SELECT string_to_array(object_name, '/') $$"
    )
    for role in ("anon", "authenticated"):
        connection.execute(
            f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN; "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
    migrations = Path(__file__).resolve().parents[2] / "supabase" / "migrations"
    for migration in sorted(migrations.glob("*.sql")):
        connection.execute(migration.read_text(encoding="utf-8"))
    yield connection
    connection.close()


def test_all_supabase_migrations_satisfy_api_readiness(migrated_database):
    from cerbere_cohort.api import (
        _REQUIRED_APPLICATION_SCHEMA_COLUMNS,
        _application_schema_ready,
        _onboarding_trigger_ready,
    )
    from cerbere_cohort.postgres import _Connection

    adapter = _Connection(DATABASE_URL, schema="public")
    try:
        assert _application_schema_ready(adapter)
        assert _onboarding_trigger_ready(adapter)
        assert len(_REQUIRED_APPLICATION_SCHEMA_COLUMNS) >= 70
    finally:
        adapter.close()


def test_organization_schemas_are_isolated_and_leases_are_enforced(migrated_database):
    from cerbere_cohort import LeaseError
    from cerbere_cohort.postgres import PostgresPersistentWorkspace

    org_a = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    org_b = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    workspace_a = PostgresPersistentWorkspace(DATABASE_URL, require_leases=True, organization_id=org_a)
    workspace_b = PostgresPersistentWorkspace(DATABASE_URL, require_leases=True, organization_id=org_b)
    try:
        workspace_a.seed({"shared_status": "org-a"})
        workspace_b.seed({"shared_status": "org-b"})
        assert workspace_a.head.data["shared_status"] == "org-a"
        assert workspace_b.head.data["shared_status"] == "org-b"

        with pytest.raises(LeaseError):
            workspace_a.create_branch("agent-a", "unleased-write").set("shared_status", "unleased-write")

        workspace_a.acquire_lease("agent-a", "shared_status", duration_seconds=60)
        branch = workspace_a.create_branch("agent-a", "write-with-lease")
        branch.set("shared_status", "org-a-updated")
        workspace_a.commit("agent-a", branch)

        assert workspace_a.head.data["shared_status"] == "org-a-updated"
        assert workspace_b.head.data["shared_status"] == "org-b"
    finally:
        workspace_a.close()
        workspace_b.close()


def test_shared_connection_serializes_complete_transactions(migrated_database):
    from cerbere_cohort.postgres import _Connection

    connection = _Connection(DATABASE_URL, schema="public")
    transaction_started = threading.Event()
    allow_commit = threading.Event()
    second_started = threading.Event()
    second_finished = threading.Event()

    def transaction():
        connection.execute("BEGIN IMMEDIATE")
        transaction_started.set()
        assert allow_commit.wait(timeout=5)
        connection.commit()

    def concurrent_request():
        assert transaction_started.wait(timeout=5)
        second_started.set()
        connection.execute("SELECT 1")
        second_finished.set()

    first = threading.Thread(target=transaction)
    second = threading.Thread(target=concurrent_request)
    try:
        first.start()
        second.start()
        assert second_started.wait(timeout=5)
        assert not second_finished.wait(timeout=0.1)
        allow_commit.set()
        first.join(timeout=5)
        second.join(timeout=5)
        assert not first.is_alive()
        assert not second.is_alive()
        assert second_finished.is_set()
    finally:
        allow_commit.set()
        first.join(timeout=5)
        second.join(timeout=5)
        connection.close()
