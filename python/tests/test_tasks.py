import threading
import time

import pytest

from cerbere_cohort.persistence import PersistentWorkspace
from cerbere_cohort.tasks import TaskCoordinator, TaskError


def test_task_lifecycle_and_idempotent_completion(tmp_path):
    store = PersistentWorkspace(tmp_path / "tasks.db")
    tasks = TaskCoordinator(store._conn)
    created = tasks.create("Process invoice", {"invoice_id": "inv-42"}, "create-inv-42")
    claimed = tasks.claim(created["task_id"], "agent-a", lease_seconds=30)
    assert claimed["status"] == "assigned"
    assert claimed["fencing_token"] == 1
    running = tasks.start(created["task_id"], "agent-a", 1)
    assert running["status"] == "running"
    assert tasks.heartbeat(created["task_id"], "agent-a", 1, 30)["status"] == "running"

    completed = tasks.complete(
        created["task_id"], "agent-a", 1, {"ledger_id": "ledger-9"}, "complete-inv-42"
    )
    retry = tasks.complete(
        created["task_id"], "agent-a", 1, {"ledger_id": "ledger-9"}, "complete-inv-42"
    )
    assert completed["status"] == "completed"
    assert retry == completed
    assert [event["action"] for event in tasks.events(created["task_id"])] == [
        "task.created", "task.assigned", "task.started", "task.heartbeat", "task.completed"
    ]
    store.close()


def test_expired_lease_reassignment_fences_old_agent_after_restart(tmp_path):
    db = tmp_path / "task-recovery.db"
    first = PersistentWorkspace(db)
    coordinator_a = TaskCoordinator(first._conn)
    task = coordinator_a.create("Generate report", {"report": "weekly"})
    owner_a = coordinator_a.claim(task["task_id"], "agent-a", lease_seconds=1)
    coordinator_a.start(task["task_id"], "agent-a", owner_a["fencing_token"])
    first.close()

    time.sleep(1.05)
    second = PersistentWorkspace(db)
    coordinator_b = TaskCoordinator(second._conn)
    owner_b = coordinator_b.claim(task["task_id"], "agent-b", lease_seconds=30)
    assert owner_b["fencing_token"] == owner_a["fencing_token"] + 1
    coordinator_b.start(task["task_id"], "agent-b", owner_b["fencing_token"])

    with pytest.raises(TaskError) as exc:
        coordinator_a.complete(
            task["task_id"], "agent-a", owner_a["fencing_token"],
            {"report": "stale"}, "stale-result",
        )
    assert exc.value.code == "task_fenced"

    final = coordinator_b.complete(
        task["task_id"], "agent-b", owner_b["fencing_token"],
        {"report": "published"}, "published-result",
    )
    assert final["result"] == {"report": "published"}
    second.close()


def test_independent_sqlite_clients_cannot_claim_the_same_task(tmp_path):
    db = tmp_path / "task-claim-race.db"
    first = PersistentWorkspace(db)
    task = TaskCoordinator(first._conn).create("Run migration", {})
    second = PersistentWorkspace(db)
    coordinator_a = TaskCoordinator(first._conn)
    coordinator_b = TaskCoordinator(second._conn)
    barrier = threading.Barrier(2)
    outcomes = []

    def claim(coordinator, agent):
        barrier.wait(timeout=5)
        try:
            result = coordinator.claim(task["task_id"], agent, lease_seconds=30)
            outcomes.append((agent, "claimed", result["fencing_token"]))
        except TaskError as exc:
            outcomes.append((agent, exc.code, None))

    a = threading.Thread(target=claim, args=(coordinator_a, "agent-a"))
    b = threading.Thread(target=claim, args=(coordinator_b, "agent-b"))
    a.start()
    b.start()
    a.join(timeout=10)
    b.join(timeout=10)
    assert not a.is_alive() and not b.is_alive()
    assert sum(status == "claimed" for _, status, _ in outcomes) == 1
    assert sum(status == "task_leased" for _, status, _ in outcomes) == 1
    first.close()
    second.close()


def test_task_creation_idempotency_rejects_changed_payload(tmp_path):
    store = PersistentWorkspace(tmp_path / "task-create-idempotency.db")
    tasks = TaskCoordinator(store._conn)
    first = tasks.create("Send invoice", {"invoice": 7}, "create-key")
    retry = tasks.create("Send invoice", {"invoice": 7}, "create-key")
    assert retry["task_id"] == first["task_id"]
    with pytest.raises(TaskError) as exc:
        tasks.create("Send invoice", {"invoice": 8}, "create-key")
    assert exc.value.code == "idempotency_key_reused"
    store.close()
