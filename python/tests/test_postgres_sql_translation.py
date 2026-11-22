"""Unit tests for SQLite-to-PostgreSQL SQL compatibility translation."""
from cerbere_cohort.postgres import _Connection


def test_begin_immediate_becomes_transaction_begin():
    assert _Connection._translate("BEGIN IMMEDIATE") == "BEGIN"


def test_insert_or_ignore_uses_postgres_conflict_clause():
    sql = _Connection._translate(
        "INSERT OR IGNORE INTO events (event_id, action) VALUES (?, ?)"
    )
    assert sql == (
        "INSERT INTO public.cohort_events (event_id, action) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING"
    )


def test_insert_or_replace_becomes_upsert():
    sql = _Connection._translate(
        "INSERT OR REPLACE INTO leases "
        "(resource_key, agent_id, expires_at) VALUES (?, ?, ?)"
    )
    assert "ON CONFLICT (resource_key) DO UPDATE SET" in sql
    assert "agent_id=EXCLUDED.agent_id" in sql
    assert "expires_at=EXCLUDED.expires_at" in sql
    assert "?" not in sql


def test_event_order_uses_persistent_sequence():
    sql = _Connection._translate("SELECT * FROM events ORDER BY rowid")
    assert sql == "SELECT * FROM public.cohort_events ORDER BY seq"


def test_lease_counter_uses_greatest():
    sql = _Connection._translate(
        "INSERT INTO lease_counters (resource_key, fencing_token) VALUES (?, ?) "
        "ON CONFLICT(resource_key) DO UPDATE SET "
        "fencing_token = MAX(fencing_token, excluded.fencing_token)"
    )
    assert "GREATEST(cohort_lease_counters.fencing_token, EXCLUDED.fencing_token)" in sql


def test_mcp_branch_registry_is_organization_scoped():
    sql = _Connection._translate("CREATE TABLE IF NOT EXISTS mcp_branches (agent_id TEXT)", "cohort_org_123")
    assert sql == "CREATE TABLE IF NOT EXISTS cohort_org_123.cohort_mcp_branches (agent_id TEXT)"


def test_mcp_branch_registry_keeps_public_compatibility():
    sql = _Connection._translate("SELECT * FROM mcp_branches")
    assert sql == "SELECT * FROM cohort_mcp_branches"



def test_audit_log_metadata_column_is_not_rewritten_as_metadata_table():
    sql = _Connection._translate(
        "INSERT INTO public.cohort_audit_logs "
        "(organization_id, actor_user_id, action, target_type, target_id, metadata) "
        "VALUES (?, ?, 'workspace.onboarding_completed', 'workspace', ?, "
        "jsonb_build_object('account_type', ?, 'email_domain', ?))"
    )
    assert "public.cohort_audit_logs" in sql
    assert ", metadata)" in sql
    assert "public.cohort_metadata" not in sql


def test_legacy_metadata_table_is_still_namespaced():
    create_sql = _Connection._translate(
        "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    select_sql = _Connection._translate("SELECT key, value FROM metadata")
    assert create_sql.startswith("CREATE TABLE IF NOT EXISTS public.cohort_metadata")
    assert select_sql == "SELECT key, value FROM public.cohort_metadata"
