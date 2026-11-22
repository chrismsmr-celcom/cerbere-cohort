"""Closed-beta golden path: branch, lease, write, conflict, inspect and resolve."""
import threading

from cerbere_cohort import api
from cerbere_cohort.persistence import PersistentWorkspace


def _prepare_workspace(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "tester-golden-path.db", require_leases=False)
    api._ensure_api_branches_schema(store._conn, postgres=False)
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})
    monkeypatch.setattr(api, "_lock", threading.RLock())
    store.seed({"shared_status": "pending"})
    return store


def test_two_agents_complete_the_conflict_resolution_golden_path(tmp_path, monkeypatch):
    store = _prepare_workspace(tmp_path, monkeypatch)
    agent_a = {"principal_type": "agent", "agent_id": "agent-a"}
    agent_b = {"principal_type": "agent", "agent_id": "agent-b"}

    branch_a = api.create_branch(api.BranchCreate(agent_id="agent-a", name="agent-a-change"), agent_a)
    branch_b = api.create_branch(api.BranchCreate(agent_id="agent-b", name="agent-b-change"), agent_b)

    api.write_branch(branch_a["id"], api.BranchWrite(
        key="shared_status", value="approved-by-a", intent="Agent A update"
    ), agent_a)
    api.write_branch(branch_b["id"], api.BranchWrite(
        key="shared_status", value="approved-by-b", intent="Agent B update"
    ), agent_b)

    first_commit = api.commit_branch(branch_a["id"], agent_a)
    assert first_commit["ok"] is True
    assert store.head.data["shared_status"] == "approved-by-a"

    second_commit = api.commit_branch(branch_b["id"], agent_b)
    assert second_commit["ok"] is False
    assert second_commit["conflict"] is True
    assert second_commit["conflicting_keys"]

    conflict = api.get_branch_conflict(branch_b["id"], agent_b)
    assert conflict["conflicts"]
    assert conflict["conflicts"][0]["head"] == "approved-by-a"
    assert conflict["conflicts"][0]["branch"] == "approved-by-b"

    resolved = api.resolve_branch(
        branch_b["id"],
        api.ResolveRequest(strategy="PreferBranch", rationale="Agent B owns the latest approved decision"),
        agent_b,
    )
    assert resolved["ok"] is True
    assert store.head.data["shared_status"] == "approved-by-b"
    assert api._branches[branch_b["id"]]["status"] == "merged"
    store.close()
