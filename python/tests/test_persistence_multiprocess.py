"""Regression tests for SQLite commits from independent OS processes."""

import multiprocessing

from cerbere_cohort import ConflictError
from cerbere_cohort.persistence import PersistentWorkspace


def _commit_from_process(db_path, agent_id, key, value, barrier, results):
    with PersistentWorkspace(db_path) as workspace:
        branch = workspace.create_branch(agent_id, agent_id)
        branch.set(key, value)
        # Both processes must hold branches based on the same persisted HEAD.
        barrier.wait(timeout=15)
        try:
            workspace.commit(agent_id, branch)
        except ConflictError:
            results.put((agent_id, "conflict"))
        else:
            results.put((agent_id, "committed"))


def _run_workers(db_path, writes):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(len(writes))
    results = ctx.Queue()
    workers = [
        ctx.Process(
            target=_commit_from_process,
            args=(str(db_path), agent_id, key, value, barrier, results),
        )
        for agent_id, key, value in writes
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    for worker in workers:
        assert not worker.is_alive(), "worker did not finish"
        assert worker.exitcode == 0
    return sorted(results.get(timeout=5) for _ in workers)


def test_concurrent_disjoint_commits_are_merged_without_lost_updates(tmp_path):
    db = tmp_path / "multi-process-disjoint.db"
    with PersistentWorkspace(db) as workspace:
        workspace.seed({"initial": True})

    outcomes = _run_workers(
        db,
        [("agent-a", "result/a", 1), ("agent-b", "result/b", 2)],
    )
    assert outcomes == [("agent-a", "committed"), ("agent-b", "committed")]

    with PersistentWorkspace(db) as workspace:
        assert workspace.head.data == {"initial": True, "result/a": 1, "result/b": 2}


def test_concurrent_conflicting_commits_reject_exactly_one_writer(tmp_path):
    db = tmp_path / "multi-process-conflict.db"
    with PersistentWorkspace(db) as workspace:
        workspace.seed({"shared": "initial"})

    outcomes = _run_workers(
        db,
        [("agent-a", "shared", "from-a"), ("agent-b", "shared", "from-b")],
    )
    assert sorted(result for _, result in outcomes) == ["committed", "conflict"]

    with PersistentWorkspace(db) as workspace:
        assert workspace.head.data["shared"] in {"from-a", "from-b"}
        actions = [event.action for event in workspace.get_events()]
        assert actions.count("workspace.commit") == 2  # seed + exactly one writer
        assert actions.count("coordination.conflict") == 1


def test_compare_and_swap_serializes_across_two_persistent_instances(tmp_path):
    from cerbere_cohort import ConflictError

    db = tmp_path / "cas-multiprocess.db"
    first = PersistentWorkspace(db)
    second = PersistentWorkspace(db)
    base = first.head.version_id
    branch_a = first.create_branch("agent-a", "cas-a")
    branch_b = second.create_branch("agent-b", "cas-b")
    branch_a.set("record", {"owner": "A"})
    branch_b.set("record", {"owner": "B"})

    first.commit("agent-a", branch_a, expected_head_version=base)
    with pytest.raises(ConflictError) as exc:
        second.commit("agent-b", branch_b, expected_head_version=base)

    assert exc.value.code == "cas_mismatch"
    assert second.head.data == {"record": {"owner": "A"}}
    assert any(event.action == "coordination.cas_rejected" for event in second.get_events())
    first.close()
    second.close()
