import threading
import time

import pytest
from fastapi import HTTPException

from cerbere_cohort import api
from cerbere_cohort.persistence import PersistentWorkspace
from cerbere_cohort.tasks import TaskCoordinator


def _api_branch_schema(store):
    store._conn.execute(
        """CREATE TABLE api_branches (
            branch_id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, branch_name TEXT NOT NULL,
            base_version TEXT NOT NULL, current_data_json TEXT NOT NULL, intents_json TEXT NOT NULL,
            fencing_tokens_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL,
            conflicting_keys_json TEXT NOT NULL, created_at TEXT NOT NULL
        )"""
    )
    store._conn.commit()


def test_api_commit_retry_returns_original_result_after_branch_registry_reset(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "api-idempotency.db")
    _api_branch_schema(store)
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})
    monkeypatch.setattr(api, "_lock", threading.RLock())
    context = {"principal_type": "user"}

    created = api.create_branch(api.BranchCreate(agent_id="agent-a", name="invoice"), context)
    api.write_branch(
        created["id"], api.BranchWrite(key="invoice/42", value={"status": "paid"}), context
    )
    first = api.commit_branch(created["id"], context, api.CommitRequest(), "commit-invoice-42")
    assert first["ok"] is True

    # Simulate a new API process: its in-memory branch registry starts empty.
    monkeypatch.setattr(api, "_branches", {})
    retry = api.commit_branch(created["id"], context, api.CommitRequest(), "commit-invoice-42")
    assert retry == first
    assert store.head.data["invoice/42"] == {"status": "paid"}
    assert len([event for event in store.get_events() if event.action == "workspace.commit"]) == 1
    store.close()


def test_api_commit_rejects_idempotency_key_reuse_with_different_payload(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "api-idempotency-mismatch.db")
    _api_branch_schema(store)
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})
    monkeypatch.setattr(api, "_lock", threading.RLock())
    context = {"principal_type": "user"}

    created = api.create_branch(api.BranchCreate(agent_id="agent-a", name="cas"), context)
    api.write_branch(created["id"], api.BranchWrite(key="record", value="written"), context)
    api.commit_branch(created["id"], context, api.CommitRequest(), "same-key")

    with pytest.raises(HTTPException) as exc:
        api.commit_branch(
            created["id"], context,
            api.CommitRequest(expected_head_version="a" * 64), "same-key",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "idempotency_key_reused"
    store.close()


def test_task_fencing_and_branch_commit_are_one_atomic_operation(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "task-bound-commit.db")
    _api_branch_schema(store)
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})
    monkeypatch.setattr(api, "_lock", threading.RLock())
    context = {"principal_type": "user"}

    created = api.create_branch(api.BranchCreate(agent_id="agent-a", name="task-work"), context)
    api.write_branch(created["id"], api.BranchWrite(key="deployment", value={"version": 2}), context)
    tasks = TaskCoordinator(store._conn)
    task = tasks.create("Deploy version 2", {"service": "api"})
    claim = tasks.claim(task["task_id"], "agent-a", lease_seconds=30)
    tasks.start(task["task_id"], "agent-a", claim["fencing_token"])

    response = api.commit_branch(
        created["id"], context,
        api.CommitRequest(task_id=task["task_id"], task_fencing_token=claim["fencing_token"]),
        "deploy-version-2",
    )
    assert response["ok"] is True
    completed = tasks.get(task["task_id"])
    assert completed["status"] == "completed"
    assert completed["result"] == {
        "branch_id": created["id"], "head": response["head"]
    }
    store.close()


def test_expired_task_owner_cannot_publish_branch_commit(tmp_path, monkeypatch):
    store = PersistentWorkspace(tmp_path / "stale-task-commit.db")
    _api_branch_schema(store)
    monkeypatch.setattr(api, "workspace", store)
    monkeypatch.setattr(api, "_branches", {})
    monkeypatch.setattr(api, "_lock", threading.RLock())
    context = {"principal_type": "user"}

    created = api.create_branch(api.BranchCreate(agent_id="agent-a", name="stale-task"), context)
    api.write_branch(created["id"], api.BranchWrite(key="release", value="v1"), context)
    tasks = TaskCoordinator(store._conn)
    task = tasks.create("Release v1", {})
    old = tasks.claim(task["task_id"], "agent-a", lease_seconds=1)
    tasks.start(task["task_id"], "agent-a", old["fencing_token"])
    time.sleep(1.05)
    replacement = tasks.claim(task["task_id"], "agent-b", lease_seconds=30)
    tasks.start(task["task_id"], "agent-b", replacement["fencing_token"])
    before = store.head.version_id

    with pytest.raises(HTTPException) as exc:
        api.commit_branch(
            created["id"], context,
            api.CommitRequest(task_id=task["task_id"], task_fencing_token=old["fencing_token"]),
            "stale-release",
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "task_fenced"
    assert store.head.version_id == before
    assert tasks.get(task["task_id"])["status"] == "running"
    store.close()
