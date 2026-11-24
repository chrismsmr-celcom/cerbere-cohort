import pytest

from cerbere_cohort.merge import ConflictError
from cerbere_cohort.lease import LeaseError
from mcp_server import CohortService


def test_branch_state_survives_server_restart(tmp_path):
    db = tmp_path / "cohort.db"
    service = CohortService(db)
    service.create_branch("cursor", "feature")
    service.write_branch("cursor", "feature", "architecture", {"status": "draft"})
    service.close()

    restarted = CohortService(db)
    assert restarted.read_branch("cursor", "feature", "architecture") == {
        "key": "architecture",
        "exists": True,
        "value": {"status": "draft"},
    }
    result = restarted.commit_branch("cursor", "feature")
    assert result["committed"] is True
    assert result["state"]["architecture"] == {"status": "draft"}
    restarted.close()


def test_conflicting_agents_never_silently_overwrite(tmp_path):
    db = tmp_path / "cohort.db"
    service = CohortService(db)
    service.create_branch("cursor", "cursor-work")
    service.create_branch("codex", "codex-work")
    service.write_branch("cursor", "cursor-work", "shared-file", "cursor version")
    service.write_branch("codex", "codex-work", "shared-file", "codex version")

    service.commit_branch("cursor", "cursor-work")
    with pytest.raises(ConflictError):
        service.commit_branch("codex", "codex-work")

    assert service.get_state()["state"]["shared-file"] == "cursor version"
    service.close()


def test_disjoint_agent_changes_merge(tmp_path):
    service = CohortService(tmp_path / "cohort.db")
    service.create_branch("cursor", "a")
    service.create_branch("codex", "b")
    service.write_branch("cursor", "a", "file-a", "A")
    service.write_branch("codex", "b", "file-b", "B")

    service.commit_branch("cursor", "a")
    result = service.commit_branch("codex", "b")
    assert result["state"] == {"file-a": "A", "file-b": "B"}
    service.close()


def test_event_limit_is_validated(tmp_path):
    service = CohortService(tmp_path / "cohort.db")
    with pytest.raises(ValueError):
        service.get_events(0)
    service.close()


def test_bound_mcp_identity_rejects_agent_spoofing(tmp_path):
    service = CohortService(
        tmp_path / "bound.db",
        authenticated_agent_id="agent-a",
    )
    with pytest.raises(ValueError, match="identity bound"):
        service.create_branch("agent-b", "forbidden")
    service.create_branch("agent-a", "allowed")
    service.close()


def test_bound_mcp_identity_filters_events(tmp_path):
    service = CohortService(
        tmp_path / "events.db",
        authenticated_agent_id="agent-a",
    )
    service.create_branch("agent-a", "own")
    service.workspace.create_branch("agent-b", "other")
    events = service.get_events()
    assert events
    assert {event["agent_id"] for event in events} == {"agent-a"}
    service.close()


def test_mcp_branch_fencing_token_survives_restart(tmp_path):
    db = tmp_path / "fencing.db"
    service = CohortService(db, require_leases=True)
    service.create_branch("agent-a", "work")
    lease = service.acquire_lease("agent-a", "record/1", 30)
    assert lease["fencing_token"] >= 1
    service.write_branch("agent-a", "work", "record/1", {"value": 1})
    service.close()

    restarted = CohortService(db, require_leases=True)
    result = restarted.commit_branch("agent-a", "work")
    assert result["committed"] is True
    assert result["state"]["record/1"] == {"value": 1}
    restarted.close()


def test_mcp_rejects_commit_after_lease_takeover(tmp_path):
    service = CohortService(tmp_path / "stale.db", require_leases=True)
    service.create_branch("agent-a", "work")
    service.acquire_lease("agent-a", "record/1", 30)
    service.write_branch("agent-a", "work", "record/1", {"value": "stale"})
    service.workspace.release_lease("agent-a", "record/1")
    service.workspace.acquire_lease("agent-b", "record/1", 30)
    with pytest.raises(LeaseError) as exc:
        service.commit_branch("agent-a", "work")
    assert exc.value.code == "lease_fenced"
    service.close()


def test_mcp_rejects_invalid_lease_duration(tmp_path):
    service = CohortService(tmp_path / "duration.db")
    with pytest.raises(ValueError, match="duration_seconds"):
        service.acquire_lease("agent-a", "record/1", 0)
    service.close()


def test_mcp_agent_scopes_separate_workspace_and_lease_permissions(tmp_path):
    service = CohortService(
        tmp_path / "scopes.db",
        authenticated_agent_id="agent-a",
        agent_scopes=["workspace:read"],
    )
    assert service.get_state()["state"] == {}
    with pytest.raises(Exception) as exc:
        service.create_branch("agent-a", "forbidden")
    assert getattr(exc.value, "code", None) == "insufficient_scope"
    service.close()



def test_rest_and_mcp_share_one_branch_registry(tmp_path, monkeypatch):
    import threading
    from cerbere_cohort import api
    from cerbere_cohort.persistence import PersistentWorkspace

    db = tmp_path / "shared-registry.db"
    rest_store = PersistentWorkspace(db)
    api._ensure_api_branches_schema(rest_store._conn, postgres=False)
    monkeypatch.setattr(api, "workspace", rest_store)
    monkeypatch.setattr(api, "_branches", {})
    monkeypatch.setattr(api, "_lock", threading.RLock())

    mcp_service = CohortService(db)
    context = {"principal_type": "agent", "agent_id": "agent-a"}

    # REST creates the branch; MCP must immediately resolve the same durable row.
    rest_branch = api.create_branch(
        api.BranchCreate(agent_id="agent-a", name="created-by-rest"), context
    )
    mcp_service.write_branch(
        "agent-a", "created-by-rest", "shared/config", {"source": "mcp"}
    )
    assert mcp_service.read_branch(
        "agent-a", "created-by-rest", "shared/config"
    )["value"] == {"source": "mcp"}

    # MCP-created branches must be visible through the REST list endpoint.
    mcp_branch = mcp_service.create_branch("agent-a", "created-by-mcp")
    visible = api.list_branches(context)["items"]
    assert any(item["id"] == mcp_branch["branch_id"] for item in visible)

    # REST can commit a branch whose latest working copy was written through MCP.
    api._restore_api_branches(clear=True)
    committed = api.commit_branch(rest_branch["id"], context)
    assert committed["ok"] is True
    assert mcp_service.get_state()["state"]["shared/config"] == {"source": "mcp"}

    mcp_service.close()
    rest_store.close()


def test_mcp_task_lifecycle_and_task_bound_commit_are_atomic(tmp_path):
    service = CohortService(tmp_path / "task-commit.db", require_leases=True)
    task = service.create_task("Update deployment config", {"key": "deployment/config"})
    claimed = service.claim_task(task["task_id"], "agent-a", lease_seconds=30)
    started = service.start_task(task["task_id"], "agent-a", claimed["fencing_token"])
    assert started["status"] == "running"

    service.create_branch("agent-a", "deployment-update")
    service.acquire_lease("agent-a", "deployment/config", 30)
    service.write_branch(
        "agent-a", "deployment-update", "deployment/config", {"version": "v2"}
    )
    committed = service.commit_branch(
        "agent-a", "deployment-update",
        task_id=task["task_id"],
        task_fencing_token=claimed["fencing_token"],
        idempotency_key="deployment-update-once",
        result={"deployed": "v2"},
    )

    assert committed["committed"] is True
    assert committed["task"]["status"] == "completed"
    assert service.get_state()["state"]["deployment/config"] == {"version": "v2"}
    assert service.get_task_events(task["task_id"])[-1]["action"] == "task.completed"
    service.close()


def test_mcp_task_bound_commit_rejects_stale_owner_without_publishing(tmp_path):
    import time
    from cerbere_cohort.tasks import TaskError

    service = CohortService(tmp_path / "stale-task-commit.db", require_leases=True)
    task = service.create_task("Update config", {})
    first = service.claim_task(task["task_id"], "agent-a", lease_seconds=1)
    service.start_task(task["task_id"], "agent-a", first["fencing_token"])
    service.create_branch("agent-a", "stale-work")
    service.acquire_lease("agent-a", "config", 30)
    service.write_branch("agent-a", "stale-work", "config", "stale")
    time.sleep(1.05)
    replacement = service.claim_task(task["task_id"], "agent-b", lease_seconds=30)
    service.start_task(task["task_id"], "agent-b", replacement["fencing_token"])

    with pytest.raises(TaskError) as exc:
        service.commit_branch(
            "agent-a", "stale-work",
            task_id=task["task_id"],
            task_fencing_token=first["fencing_token"],
            idempotency_key="stale-publication",
        )
    assert exc.value.code == "task_fenced"
    assert "config" not in service.get_state()["state"]
    service.close()



def test_legacy_mcp_branch_is_migrated_into_shared_registry(tmp_path):
    import json
    from cerbere_cohort.persistence import PersistentWorkspace

    db = tmp_path / "legacy-mcp.db"
    legacy = PersistentWorkspace(db)
    base_version = legacy.head.version_id
    legacy._conn.execute(
        """
        CREATE TABLE mcp_branches (
            agent_id TEXT NOT NULL,
            branch_name TEXT NOT NULL,
            base_version TEXT NOT NULL,
            current_data_json TEXT NOT NULL,
            intents_json TEXT NOT NULL,
            fencing_tokens_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (agent_id, branch_name)
        )
        """
    )
    legacy._conn.execute(
        """
        INSERT INTO mcp_branches
            (agent_id, branch_name, base_version, current_data_json,
             intents_json, fencing_tokens_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "agent-legacy", "unfinished", base_version,
            json.dumps({"preserved": {"from": "old-mcp"}}), "{}", "{}",
        ),
    )
    legacy._conn.commit()
    legacy.close()

    service = CohortService(db)
    assert service.read_branch("agent-legacy", "unfinished", "preserved") == {
        "key": "preserved",
        "exists": True,
        "value": {"from": "old-mcp"},
    }
    # The old table remains for compatibility, but successfully migrated rows
    # are removed so a later startup cannot replay stale copies over REST edits.
    assert service._db.execute("SELECT COUNT(*) AS count FROM mcp_branches").fetchone()["count"] == 0
    service.close()
