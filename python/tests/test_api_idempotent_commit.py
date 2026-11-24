import threading

import pytest
from fastapi import HTTPException

from cerbere_cohort import api
from cerbere_cohort.persistence import PersistentWorkspace


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
