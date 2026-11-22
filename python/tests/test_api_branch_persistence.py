import json

import pytest

from cerbere_cohort import LeaseError, api
from cerbere_cohort.persistence import PersistentWorkspace


def test_api_branch_working_copy_restores_after_registry_reset(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "workspace.db", require_leases=True)
    store._conn.execute(
        """CREATE TABLE api_branches (
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
        )"""
    )
    store._conn.commit()
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})

    store.acquire_lease("agent-a", "service.status", duration_seconds=60)
    branch = store.create_branch("agent-a", "backend")
    item = {
        "branch": branch,
        "agent_id": "agent-a",
        "status": "open",
        "conflicting_keys": [],
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    branch.set("service.status", "ready", intent="persist this change")
    api._branches["branch-1"] = item
    api._save_api_branch("branch-1", item)

    store.close()
    reopened = PersistentWorkspace(tmp_path / "workspace.db", require_leases=True)
    monkeypatch.setattr(api, "workspace", reopened)
    monkeypatch.setattr(api, "_branches", {})
    api._restore_api_branches()

    restored = api._branches["branch-1"]
    assert restored["branch"].get("service.status") == "ready"
    assert restored["branch"].intents["service.status"] == "persist this change"
    assert restored["branch"].fencing_tokens == {"service.status": 1}
    assert restored["status"] == "open"
    committed = reopened.commit("agent-a", restored["branch"])
    assert committed.data["service.status"] == "ready"
    reopened.close()


def test_branch_diff_preview_reports_proposed_and_concurrent_values(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "diff.db")
    store.seed({"profile/name": "Initial", "profile/role": "writer"})
    target = store.create_branch("agent-a", "profile-update")
    concurrent = store.create_branch("agent-b", "role-update")

    target.set("profile/name", "Alice", intent="Update the display name")
    target.set("profile/role", "admin")
    concurrent.set("profile/name", "Bob")
    store.commit("agent-b", concurrent)

    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {
        "target-branch": {
            "branch": target,
            "agent_id": "agent-a",
            "status": "open",
            "conflicting_keys": ["profile/name"],
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    })

    result = api.get_branch_diff("target-branch", context={"principal_type": "user"})

    assert result["change_count"] == 2
    name_change = next(change for change in result["changes"] if change["key"] == "profile/name")
    assert name_change["base"] == "Initial"
    assert name_change["proposed"] == "Alice"
    assert name_change["head"] == "Bob"
    assert name_change["head_changed_since_base"] is True
    assert name_change["intent"] == "Update the display name"

    role_change = next(change for change in result["changes"] if change["key"] == "profile/role")
    assert role_change["base"] == "writer"
    assert role_change["proposed"] == "admin"
    assert role_change["head_changed_since_base"] is False
    store.close()


def test_restored_branch_rejects_stale_fencing_token_after_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "stale-token.db"
    store = PersistentWorkspace(db_path, require_leases=True)
    store._conn.execute(
        """CREATE TABLE api_branches (
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
        )"""
    )
    store._conn.commit()
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})
    store.acquire_lease("agent-a", "service.status", duration_seconds=60)
    branch = store.create_branch("agent-a", "backend")
    branch.set("service.status", "stale-write")
    item = {
        "branch": branch, "agent_id": "agent-a", "status": "open",
        "conflicting_keys": [], "created_at": "2026-01-01T00:00:00+00:00",
    }
    api._branches["branch-stale"] = item
    api._save_api_branch("branch-stale", item)

    store.release_lease("agent-a", "service.status")
    replacement = store.acquire_lease("agent-a", "service.status", duration_seconds=60)
    assert replacement.fencing_token == 2
    store.close()

    reopened = PersistentWorkspace(db_path, require_leases=True)
    monkeypatch.setattr(api, "workspace", reopened)
    monkeypatch.setattr(api, "_branches", {})
    api._restore_api_branches()
    restored = api._branches["branch-stale"]["branch"]

    with pytest.raises(LeaseError) as exc:
        reopened.commit("agent-a", restored)
    assert exc.value.code == "lease_fenced"
    reopened.close()


def test_legacy_api_branch_schema_upgrade_is_idempotent_and_preserves_rows(tmp_path):
    from cerbere_cohort.api import _ensure_api_branches_schema

    store = PersistentWorkspace(tmp_path / "legacy-schema.db")
    store._conn.execute(
        """CREATE TABLE api_branches (
            branch_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, branch_name TEXT NOT NULL,
            base_version TEXT NOT NULL, current_data_json TEXT NOT NULL, intents_json TEXT NOT NULL,
            status TEXT NOT NULL, conflicting_keys_json TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    store._conn.execute(
        """INSERT INTO api_branches
           (branch_id, agent_id, branch_name, base_version, current_data_json, intents_json,
            status, conflicting_keys_json, created_at)
           VALUES ('legacy-1', 'agent-a', 'backend', 'v1', '{}', '{}', 'open', '[]', 'now')"""
    )
    store._conn.commit()

    _ensure_api_branches_schema(store._conn, postgres=False)
    _ensure_api_branches_schema(store._conn, postgres=False)

    row = store._conn.execute(
        "SELECT branch_id, fencing_tokens_json FROM api_branches WHERE branch_id = 'legacy-1'"
    ).fetchone()
    assert row["branch_id"] == "legacy-1"
    assert json.loads(row["fencing_tokens_json"]) == {}
    store.close()


def test_postgres_api_branch_schema_upgrade_runs_after_create():
    class RecordingConnection:
        def __init__(self):
            self.statements = []
        def execute(self, sql):
            self.statements.append(sql)
        def commit(self):
            self.statements.append("COMMIT")

    from cerbere_cohort.api import _ensure_api_branches_schema

    conn = RecordingConnection()
    _ensure_api_branches_schema(conn, postgres=True)
    create_index = next(i for i, sql in enumerate(conn.statements) if sql.startswith("CREATE TABLE"))
    alter_index = next(i for i, sql in enumerate(conn.statements) if sql.startswith("ALTER TABLE"))
    assert create_index < alter_index
    assert "ADD COLUMN IF NOT EXISTS fencing_tokens_json" in conn.statements[alter_index]


def test_api_branch_write_rebinds_after_lease_mutation(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "api-lease-rebind.db", require_leases=True)
    _ensure_api_test_schema(store)
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})
    context = {"principal_type": "agent", "agent_id": "agent-a"}
    created = api.create_branch(api.BranchCreate(agent_id="agent-a", name="work"), context)
    store.acquire_lease("agent-a", "record/1", duration_seconds=60)

    written = api.write_branch(
        created["id"],
        api.BranchWrite(key="record/1", value={"status": "ready"}),
        context,
    )
    assert written["ok"] is True
    committed = api.commit_branch(created["id"], context)
    assert committed["ok"] is True
    assert store.head.data["record/1"] == {"status": "ready"}
    store.close()


def _ensure_api_test_schema(store):
    store._conn.execute(
        """CREATE TABLE api_branches (
            branch_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, branch_name TEXT NOT NULL,
            base_version TEXT NOT NULL, current_data_json TEXT NOT NULL, intents_json TEXT NOT NULL,
            fencing_tokens_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,
            conflicting_keys_json TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    store._conn.commit()



def test_conflict_api_reads_encoded_root_key_values_and_presence(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "encoded-conflict.db")
    key = "json-pointer:/service/status"
    store.seed({key: "base", "service": {"status": "untouched"}})
    branch = store.create_branch("agent-a", "api-conflict")
    concurrent = store.create_branch("agent-b", "concurrent")
    branch.set(key, "branch-value")
    concurrent.set(key, "head-value")
    store.commit("agent-b", concurrent)

    encoded = "root-key:v1:" + __import__("base64").urlsafe_b64encode(key.encode()).decode().rstrip("=")
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {
        "branch-conflict": {
            "branch": branch, "agent_id": "agent-a", "status": "open",
            "conflicting_keys": [encoded], "created_at": "2026-01-01T00:00:00+00:00",
        }
    })

    result = api.get_branch_conflict("branch-conflict", context={"principal_type": "user"})
    conflict = result["conflicts"][0]
    assert conflict["base"] == "base" and conflict["base_present"] is True
    assert conflict["head"] == "head-value" and conflict["head_present"] is True
    assert conflict["branch"] == "branch-value" and conflict["branch_present"] is True
    assert conflict["key"] == encoded
    store.close()


def test_api_commit_accepts_optional_compare_and_swap_version(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "api-cas.db")
    _ensure_api_test_schema(store)
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})
    context = {"principal_type": "agent", "agent_id": "agent-a"}
    created = api.create_branch(api.BranchCreate(agent_id="agent-a", name="cas"), context)
    branch = api._branches[created["id"]]["branch"]
    expected = branch.base_version
    branch.set("task", "done")
    api._save_api_branch(created["id"], api._branches[created["id"]])

    result = api.commit_branch(
        created["id"], context, api.CommitRequest(expected_head_version=expected)
    )
    assert result["ok"] is True
    assert store.head.data["task"] == "done"
    store.close()


def test_api_commit_returns_cas_mismatch_code(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "api-cas-mismatch.db")
    _ensure_api_test_schema(store)
    store.seed({"task": "pending"})
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})
    context = {"principal_type": "agent", "agent_id": "agent-a"}
    created = api.create_branch(api.BranchCreate(agent_id="agent-a", name="cas-stale"), context)
    stale = api._branches[created["id"]]["branch"]
    expected = stale.base_version
    stale.set("task", "agent-write")
    api._save_api_branch(created["id"], api._branches[created["id"]])

    winner = store.create_branch("agent-b", "winner")
    winner.set("task", "human-write")
    store.commit("agent-b", winner)

    result = api.commit_branch(
        created["id"], context, api.CommitRequest(expected_head_version=expected)
    )
    assert result["ok"] is False
    assert result["error_code"] == "cas_mismatch"
    assert store.head.data["task"] == "human-write"
    store.close()
