"""Critères de conformité v1 (docs/CONCEPTS.md §13) sur le modèle clé-valeur actuel."""
import pytest

from cerbere_cohort import ConflictError, PreferBranch, Workspace


def seeded(**data):
    """Workspace dont HEAD contient `data` (commité via une branche, pas par mutation directe)."""
    ws = Workspace()
    b = ws.create_branch("setup", "seed")
    for k, v in data.items():
        b.set(k, v)
    ws.commit("setup", b)
    return ws


def actions(ws):
    return [e.action for e in ws.get_events()]


def test_same_key_different_content_conflicts_and_head_is_unchanged():
    ws = seeded(spec="v1")
    a, b = ws.create_branch("A", "a"), ws.create_branch("B", "b")
    a.set("spec", "par A")
    b.set("spec", "par B")
    ws.commit("A", a)
    head_before = ws.head.version_id
    with pytest.raises(ConflictError) as exc:
        ws.commit("B", b)
    assert exc.value.conflicting_keys == ["spec"]
    assert ws.head.version_id == head_before
    assert ws.head.data["spec"] == "par A"


def test_disjoint_keys_merge_cleanly():
    ws = seeded(x=1, y=1)
    a, b = ws.create_branch("A", "a"), ws.create_branch("B", "b")
    a.set("x", 2)
    b.set("y", 2)
    ws.commit("A", a)
    ws.commit("B", b)
    assert ws.head.data == {"x": 2, "y": 2}


def test_identical_content_does_not_conflict():
    ws = seeded(k="x")
    a, b = ws.create_branch("A", "a"), ws.create_branch("B", "b")
    a.set("k", "y")
    b.set("k", "y")
    ws.commit("A", a)
    ws.commit("B", b)
    assert ws.head.data["k"] == "y"


def test_delete_vs_modify_conflicts():
    ws = seeded(k="x")
    a, b = ws.create_branch("A", "a"), ws.create_branch("B", "b")
    a.delete("k")
    b.set("k", "z")
    ws.commit("A", a)
    with pytest.raises(ConflictError):
        ws.commit("B", b)


def test_delete_is_applied_and_not_resurrected():
    ws = seeded(k="x", other=1)
    a = ws.create_branch("A", "a")
    a.delete("k")
    ws.commit("A", a)
    assert "k" not in ws.head.data and ws.head.data["other"] == 1


def test_delete_vs_delete_is_not_a_conflict():
    ws = seeded(k="x")
    a, b = ws.create_branch("A", "a"), ws.create_branch("B", "b")
    a.delete("k")
    b.delete("k")
    ws.commit("A", a)
    ws.commit("B", b)
    assert "k" not in ws.head.data


def test_none_value_is_not_confused_with_absent():
    ws = seeded()
    a = ws.create_branch("A", "a")
    a.set("k", None)
    ws.commit("A", a)
    assert "k" in ws.head.data and ws.head.data["k"] is None


def test_branch_is_isolated_until_commit():
    ws = seeded(k=1)
    a = ws.create_branch("A", "a")
    a.set("k", 2)
    assert ws.head.data["k"] == 1


def test_events_for_branch_commit_and_conflict():
    ws = seeded(k=1)
    a, b = ws.create_branch("A", "a"), ws.create_branch("B", "b")
    a.set("k", 2)
    b.set("k", 3)
    ws.commit("A", a)
    with pytest.raises(ConflictError):
        ws.commit("B", b)
    acts = actions(ws)
    assert acts.count("workspace.branch") == 3
    assert acts.count("workspace.commit") == 2  # seed + A
    conflict = [e for e in ws.get_events() if e.action == "coordination.conflict"]
    assert len(conflict) == 1 and conflict[0].payload["conflicting_keys"] == ["k"]


def test_commit_payload_reports_real_modified_keys_and_merge_type():
    ws = seeded(x=1, y=1)
    a, b = ws.create_branch("A", "a"), ws.create_branch("B", "b")
    a.set("x", 2)
    b.set("y", 2)
    ws.commit("A", a)
    ws.commit("B", b)
    commits = [e for e in ws.get_events() if e.action == "workspace.commit"]
    assert commits[-2].payload["merge_type"] == "fast-forward"
    assert commits[-1].payload["merge_type"] == "auto-merge"
    assert commits[-1].payload["keys_modified"] == ["y"]


def test_event_serialization_includes_schema_version():
    import json
    from cerbere_cohort.events import CerbereCohortEvent

    event = CerbereCohortEvent(agent_id="agent-a", action="workspace.commit")
    serialized = json.loads(event.to_jsonl())

    assert serialized["schema_version"] == "1.0.0"


def test_legacy_event_without_schema_version_is_readable():
    from cerbere_cohort.events import CerbereCohortEvent

    legacy = (
        '{"event_id":"00000000-0000-0000-0000-000000000001",'
        '"timestamp":"2026-11-15T10:00:00+00:00",'
        '"agent_id":"agent-a","action":"workspace.commit","payload":{}}'
    )
    event = CerbereCohortEvent.from_jsonl(legacy)

    assert event.schema_version == "1.0.0"


# --- Nested JSON merge ---------------------------------------------------------------

def test_disjoint_nested_object_changes_merge_without_conflict():
    ws = seeded(config={"status": "pending", "owner": "agent-a"})
    a, b = ws.create_branch("A", "nested-a"), ws.create_branch("B", "nested-b")
    a.set("config", {"status": "completed", "owner": "agent-a"})
    b.set("config", {"status": "pending", "owner": "agent-b"})

    ws.commit("A", a)
    ws.commit("B", b)

    assert ws.head.data["config"] == {
        "status": "completed",
        "owner": "agent-b",
    }


def test_same_nested_leaf_conflicts_and_preserves_head():
    ws = seeded(config={"status": "pending", "owner": "agent-a"})
    a, b = ws.create_branch("A", "nested-a"), ws.create_branch("B", "nested-b")
    a.set("config", {"status": "completed", "owner": "agent-a"})
    b.set("config", {"status": "failed", "owner": "agent-a"})

    ws.commit("A", a)
    head_before = ws.head.version_id
    with pytest.raises(ConflictError) as exc:
        ws.commit("B", b)

    assert exc.value.conflicting_keys == ["json-pointer:/config/status"]
    assert ws.head.version_id == head_before
    assert ws.head.data["config"]["status"] == "completed"


def test_concurrent_creation_of_disjoint_object_fields_merges():
    ws = Workspace()
    a, b = ws.create_branch("A", "create-a"), ws.create_branch("B", "create-b")
    a.set("config", {"region": "eu"})
    b.set("config", {"replicas": 3})

    ws.commit("A", a)
    ws.commit("B", b)

    assert ws.head.data["config"] == {"region": "eu", "replicas": 3}


def test_nested_array_is_atomic_and_conflicting_edits_are_not_guessed():
    ws = seeded(config={"steps": ["build", "test"]})
    a, b = ws.create_branch("A", "array-a"), ws.create_branch("B", "array-b")
    a.set("config", {"steps": ["build", "test", "deploy"]})
    b.set("config", {"steps": ["build", "lint", "test"]})

    ws.commit("A", a)
    with pytest.raises(ConflictError) as exc:
        ws.commit("B", b)

    assert exc.value.conflicting_keys == ["json-pointer:/config/steps"]
    assert ws.head.data["config"]["steps"] == ["build", "test", "deploy"]


def test_nested_delete_vs_modify_conflicts():
    ws = seeded(config={"feature": {"enabled": True, "owner": "A"}})
    a, b = ws.create_branch("A", "delete-parent"), ws.create_branch("B", "edit-child")
    a.set("config", {})
    b.set("config", {"feature": {"enabled": False, "owner": "A"}})

    ws.commit("A", a)
    with pytest.raises(ConflictError) as exc:
        ws.commit("B", b)

    assert exc.value.conflicting_keys == ["json-pointer:/config/feature"]
    assert ws.head.data["config"] == {}



def test_nested_json_merge_distinguishes_boolean_from_integer():
    ws = seeded(config={"count": 1})
    branch = ws.create_branch("A", "bool-is-not-int")
    branch.set("config", {"count": True})

    ws.commit("A", branch)

    assert ws.head.data["config"]["count"] is True


def test_root_key_that_looks_like_json_pointer_is_not_parsed_as_nested_path():
    pointer_like_key = "json-pointer:/config/status"
    ws = Workspace()
    ws.seed({pointer_like_key: "base", "config": {"status": "unchanged"}})
    a, b = ws.create_branch("A", "root-pointer-a"), ws.create_branch("B", "root-pointer-b")
    a.set(pointer_like_key, "from-A")
    b.set(pointer_like_key, "from-B")

    ws.commit("A", a)
    ws.commit("B", b, resolver=PreferBranch())

    assert ws.head.data[pointer_like_key] == "from-B"
    assert ws.head.data["config"]["status"] == "unchanged"
    resolution = [
        event for event in ws.get_events()
        if event.action == "coordination.resolved"
    ][0]
    assert resolution.payload["key"].startswith("root-key:")
    replayed = Workspace.replay(ws.get_events(), ws.store)
    assert replayed.head.data == ws.head.data
