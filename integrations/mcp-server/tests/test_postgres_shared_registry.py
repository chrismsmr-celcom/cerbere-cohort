"""Independent PostgreSQL MCP clients share the durable REST branch registry."""
import os
from uuid import uuid4

import pytest

DATABASE_URL = os.getenv("COHORT_TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="COHORT_TEST_DATABASE_URL is not configured"
)


def test_independent_mcp_clients_share_branches_and_task_bound_publication(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    from mcp_server import CohortService

    organization_id = str(uuid4())
    first = CohortService(organization_id=organization_id, require_leases=True)
    second = CohortService(organization_id=organization_id, require_leases=True)
    try:
        created = first.create_branch("agent-a", "cross-transport")
        assert created["branch_id"].startswith("mcp-")
        second.acquire_lease("agent-a", "deployment/config", 60)
        first.write_branch(
            "agent-a", "cross-transport", "deployment/config", {"version": "v3"}
        )

        # A second independent connection resolves and reads the same api_branches row.
        assert second.read_branch(
            "agent-a", "cross-transport", "deployment/config"
        )["value"] == {"version": "v3"}

        task = first.create_task("Publish deployment config", {"service": "api"})
        claimed = second.claim_task(task["task_id"], "agent-a", lease_seconds=60)
        second.start_task(task["task_id"], "agent-a", claimed["fencing_token"])

        committed = first.commit_branch(
            "agent-a", "cross-transport",
            task_id=task["task_id"],
            task_fencing_token=claimed["fencing_token"],
            idempotency_key="cross-transport-publish",
        )
        assert committed["committed"] is True
        assert committed["task"]["status"] == "completed"

        second.workspace.refresh()
        assert second.get_state()["state"]["deployment/config"] == {"version": "v3"}
        assert second.get_task(task["task_id"])["status"] == "completed"
    finally:
        first.close()
        second.close()
