"""Independent PostgreSQL client tests for durable task ownership and fencing."""
import os
import threading
import time
from uuid import uuid4

import pytest

DATABASE_URL = os.getenv("COHORT_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="COHORT_TEST_DATABASE_URL is not configured")


def test_independent_postgres_clients_serialize_claim_and_fence_expired_owner():
    from cerbere_cohort.postgres import PostgresPersistentWorkspace
    from cerbere_cohort.tasks import TaskCoordinator, TaskError

    organization_id = str(uuid4())
    first = PostgresPersistentWorkspace(
        DATABASE_URL, require_leases=True, organization_id=organization_id
    )
    second = PostgresPersistentWorkspace(
        DATABASE_URL, require_leases=True, organization_id=organization_id
    )
    stale = None
    try:
        tasks_a = TaskCoordinator(first._conn)
        tasks_b = TaskCoordinator(second._conn)
        task = tasks_a.create("Reconcile ledger", {"ledger": "daily"})
        barrier = threading.Barrier(2)
        outcomes = []

        def claim(coordinator, agent_id):
            barrier.wait(timeout=10)
            try:
                value = coordinator.claim(task["task_id"], agent_id, lease_seconds=1)
                outcomes.append((agent_id, "claimed", value["fencing_token"]))
            except TaskError as exc:
                outcomes.append((agent_id, exc.code, None))

        a = threading.Thread(target=claim, args=(tasks_a, "agent-a"))
        b = threading.Thread(target=claim, args=(tasks_b, "agent-b"))
        a.start()
        b.start()
        a.join(timeout=15)
        b.join(timeout=15)
        assert not a.is_alive() and not b.is_alive()
        winners = [item for item in outcomes if item[1] == "claimed"]
        losers = [item for item in outcomes if item[1] == "task_leased"]
        assert len(winners) == 1
        assert len(losers) == 1

        owner, _, old_token = winners[0]
        winner_coordinator = tasks_a if owner == "agent-a" else tasks_b
        winner_coordinator.start(task["task_id"], owner, old_token)
        time.sleep(1.05)

        replacement = tasks_b if winner_coordinator is tasks_a else tasks_a
        new_owner = "agent-b" if owner == "agent-a" else "agent-a"
        claimed = replacement.claim(task["task_id"], new_owner, lease_seconds=30)
        assert claimed["fencing_token"] == old_token + 1
        replacement.start(task["task_id"], new_owner, claimed["fencing_token"])

        stale = TaskCoordinator(
            PostgresPersistentWorkspace(
                DATABASE_URL, require_leases=True, organization_id=organization_id
            )._conn
        )
        with pytest.raises(TaskError) as exc:
            stale.complete(task["task_id"], owner, old_token, {"ledger": "stale"}, "stale")
        assert exc.value.code == "task_fenced"

        done = replacement.complete(
            task["task_id"], new_owner, claimed["fencing_token"],
            {"ledger": "reconciled"}, "daily-ledger-result",
        )
        assert done["status"] == "completed"
        assert done["result"] == {"ledger": "reconciled"}
    finally:
        first.close()
        second.close()
        if stale is not None:
            stale._conn.close()
