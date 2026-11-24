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

        stale = winner_coordinator
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


def test_postgres_idempotent_commit_is_shared_between_independent_clients():
    from cerbere_cohort.postgres import PostgresPersistentWorkspace

    organization_id = str(uuid4())
    first = PostgresPersistentWorkspace(
        DATABASE_URL, require_leases=False, organization_id=organization_id
    )
    second = PostgresPersistentWorkspace(
        DATABASE_URL, require_leases=False, organization_id=organization_id
    )
    try:
        def commit_once():
            branch = first._workspace.create_branch("agent-a", "idempotent-write")
            branch.set("ledger/invoice-42", {"status": "paid"})
            snapshot = first._workspace.commit("agent-a", branch)
            return {"head": snapshot.version_id, "status": "committed"}

        original = first.execute_idempotent(
            "invoice:42", "commit-key-42", "request-hash-42", commit_once
        )
        retried = second.execute_idempotent(
            "invoice:42", "commit-key-42", "request-hash-42",
            lambda: pytest.fail("a second PostgreSQL client must replay the stored response"),
        )
        assert retried == original
        second.refresh()
        assert second.head.data["ledger/invoice-42"] == {"status": "paid"}
        assert len([
            event for event in second.get_events()
            if event.action == "workspace.commit"
        ]) == 1
    finally:
        first.close()
        second.close()


def test_postgres_independent_clients_serialize_conflicting_workspace_commits():
    from cerbere_cohort import ConflictError
    from cerbere_cohort.postgres import PostgresPersistentWorkspace

    organization_id = str(uuid4())
    first = PostgresPersistentWorkspace(
        DATABASE_URL, require_leases=False, organization_id=organization_id
    )
    second = PostgresPersistentWorkspace(
        DATABASE_URL, require_leases=False, organization_id=organization_id
    )
    barrier = threading.Barrier(2)
    outcomes = []
    try:
        first.seed({"shared-record": "base"})
        branch_a = first.create_branch("agent-a", "write-a")
        branch_b = second.create_branch("agent-b", "write-b")
        branch_a.set("shared-record", "from-a")
        branch_b.set("shared-record", "from-b")

        def commit(workspace, agent, branch):
            barrier.wait(timeout=10)
            try:
                workspace.commit(agent, branch)
                outcomes.append("committed")
            except ConflictError:
                outcomes.append("conflict")

        a = threading.Thread(target=commit, args=(first, "agent-a", branch_a))
        b = threading.Thread(target=commit, args=(second, "agent-b", branch_b))
        a.start()
        b.start()
        a.join(timeout=15)
        b.join(timeout=15)
        assert not a.is_alive() and not b.is_alive()
        assert sorted(outcomes) == ["committed", "conflict"]
        first.refresh()
        assert first.head.data["shared-record"] in {"from-a", "from-b"}
    finally:
        first.close()
        second.close()


def test_postgres_fencing_rejects_old_branch_after_lease_handoff():
    from cerbere_cohort import LeaseError
    from cerbere_cohort.postgres import PostgresPersistentWorkspace

    organization_id = str(uuid4())
    first = PostgresPersistentWorkspace(
        DATABASE_URL, require_leases=True, organization_id=organization_id
    )
    second = PostgresPersistentWorkspace(
        DATABASE_URL, require_leases=True, organization_id=organization_id
    )
    try:
        first.acquire_lease("agent-a", "deployment/config", duration_seconds=60)
        stale_branch = first.create_branch("agent-a", "stale-deploy")
        stale_branch.set("deployment/config", {"version": "old"})
        second.release_lease("agent-a", "deployment/config")
        replacement = second.acquire_lease("agent-b", "deployment/config", duration_seconds=60)
        assert replacement.fencing_token == 2
        with pytest.raises(LeaseError) as exc:
            first.commit("agent-a", stale_branch)
        assert exc.value.code == "lease_fenced"
        second.refresh()
        assert "deployment/config" not in second.head.data
    finally:
        first.close()
        second.close()
