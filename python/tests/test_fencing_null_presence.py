"""Regression tests for lease fencing when JSON null is involved."""

import pytest

from cerbere_cohort import Workspace
from cerbere_cohort.lease import LeaseError


def _replace_lease_after_branch_write(workspace, branch, resource_key):
    original = workspace.acquire_lease(
        "agent-a", resource_key, duration_seconds=30
    )
    workspace.release_lease("agent-a", resource_key)
    replacement = workspace.acquire_lease(
        "agent-b", resource_key, duration_seconds=30
    )
    assert replacement.fencing_token > original.fencing_token


def test_stale_fencing_token_rejects_null_write_to_absent_key():
    workspace = Workspace(require_leases=True)
    branch = workspace.create_branch("agent-a", "write-null")
    workspace.acquire_lease("agent-a", "shared/document", duration_seconds=30)
    branch.set("shared/document", None)

    _replace_lease_after_branch_write(workspace, branch, "shared/document")

    with pytest.raises(LeaseError) as exc_info:
        workspace.commit("agent-a", branch)

    assert exc_info.value.code == "lease_fenced"
    assert "shared/document" not in workspace.head.data


def test_stale_fencing_token_rejects_deleting_existing_null_value():
    workspace = Workspace(require_leases=True)
    workspace.seed({"shared/document": None})
    workspace.acquire_lease("agent-a", "shared/document", duration_seconds=30)
    branch = workspace.create_branch("agent-a", "delete-null")
    branch.delete("shared/document")

    _replace_lease_after_branch_write(workspace, branch, "shared/document")

    with pytest.raises(LeaseError) as exc_info:
        workspace.commit("agent-a", branch)

    assert exc_info.value.code == "lease_fenced"
    assert "shared/document" in workspace.head.data
    assert workspace.head.data["shared/document"] is None
