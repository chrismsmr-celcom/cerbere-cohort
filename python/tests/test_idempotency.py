import pytest

from cerbere_cohort.persistence import IdempotencyConflict, PersistentWorkspace


def test_idempotent_operation_returns_original_response_after_retry_and_restart(tmp_path):
    db = tmp_path / "idempotency.db"
    store = PersistentWorkspace(db)
    store.seed({"invoice/count": 0})
    branch = store.create_branch("agent-a", "invoice-write")
    branch.set("invoice/count", 1)

    calls = []
    def operation():
        calls.append("executed")
        snapshot = store._workspace.commit("agent-a", branch)
        return {"head": snapshot.version_id, "created": True}

    first = store.execute_idempotent("invoice:42", "request-1", "hash-a", operation)
    assert first["created"] is True
    assert calls == ["executed"]
    store.close()

    reopened = PersistentWorkspace(db)
    retry = reopened.execute_idempotent(
        "invoice:42", "request-1", "hash-a",
        lambda: pytest.fail("the operation must not execute twice"),
    )
    assert retry == first
    assert reopened.head.data["invoice/count"] == 1
    assert len([event for event in reopened.get_events() if event.action == "workspace.commit"]) == 2
    reopened.close()


def test_same_idempotency_key_with_different_request_is_rejected(tmp_path):
    store = PersistentWorkspace(tmp_path / "idempotency-mismatch.db")
    first = store.execute_idempotent("task:1", "same-key", "body-a", lambda: {"ok": True})
    assert first == {"ok": True}

    with pytest.raises(IdempotencyConflict):
        store.execute_idempotent(
            "task:1", "same-key", "body-b",
            lambda: pytest.fail("a mismatched retry must not execute"),
        )
    store.close()


def test_workspace_commit_and_idempotency_record_roll_back_together(tmp_path):
    db = tmp_path / "idempotency-atomicity.db"
    store = PersistentWorkspace(db)
    store.seed({"counter": 0})
    branch = store.create_branch("agent-a", "atomic-write")
    branch.set("counter", 1)

    with pytest.raises(TypeError):
        store.execute_idempotent(
            "counter", "write-1", "hash-1",
            lambda: {
                "head": store._workspace.commit("agent-a", branch).version_id,
                "not_json": object(),
            },
        )
    store.close()

    reopened = PersistentWorkspace(db)
    assert reopened.head.data["counter"] == 0
    assert reopened._conn.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0] == 0
    reopened.close()
