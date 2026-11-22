"""Critère 6 du §13 : le replay du journal reconstruit des ids de snapshot identiques."""
import dataclasses
import threading

import pytest

from cerbere_cohort import (CerbereCohortEvent, ConflictError, ContentError, ReplayError, Workspace,
                    canonical_json, content_hash, snapshot_id)


def scenario(ws):
    """Un scénario riche : fast-forward, auto-merge, conflit, suppression."""
    ws.seed({"spec": "v1", "budget": 100, "tags": ["a"]})
    a, b, c = (ws.create_branch(n, n) for n in ("A", "B", "C"))
    a.set("spec", "par A")
    b.set("budget", 250)
    b.delete("tags")
    c.set("spec", "par C")
    ws.commit("A", a)
    ws.commit("B", b)                 # auto-merge
    with pytest.raises(ConflictError):
        ws.commit("C", c)             # conflit : HEAD inchangé
    d = ws.create_branch("D", "d")
    d.set("nouveau", {"x": [1, 2, {"y": None}]})
    ws.commit("D", d)
    return ws


# --- Identité par le contenu -----------------------------------------------------

def test_golden_values_pin_the_canonical_format():
    # Si ces valeurs changent, TOUS les ids existants changent : c'est un changement de format.
    assert canonical_json({"b": 1, "a": [1, 2.5, "é", None, True]}) == '{"a":[1,2.5,"é",null,true],"b":1}'
    assert content_hash("x") == "ba2df4903a2c14e86dc3bcca58911b44ac1d2514b7227bf6eb08cfb978f55a1b"
    assert snapshot_id({}, ()) == Workspace().head.version_id
    assert Workspace().head.version_id == "a5bb60da77e0c26a06bd080b6253a2ddf48f046856d5f4122f9e68b03008ffeb"


def test_same_operations_give_identical_ids_in_two_workspaces():
    w1, w2 = scenario(Workspace()), scenario(Workspace())
    assert w1.head.version_id == w2.head.version_id
    assert set(w1._snapshots) == set(w2._snapshots)


def test_key_insertion_order_does_not_change_the_id():
    w1, w2 = Workspace(), Workspace()
    w1.seed({"a": 1, "b": 2})
    w2.seed({"b": 2, "a": 1})
    assert w1.head.version_id == w2.head.version_id


def test_same_content_but_different_history_gives_different_id():
    w1, w2 = Workspace(), Workspace()
    w1.seed({"a": 1})
    w2.seed({"a": 0})
    b = w2.create_branch("x", "x")
    b.set("a", 1)
    w2.commit("x", b)
    assert w1.head.data == w2.head.data
    assert w1.head.version_id != w2.head.version_id


def test_value_types_are_strict_and_normalised():
    assert content_hash(1) != content_hash(1.0)
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})
    for bad in ({1, 2}, (1, 2), float("nan"), float("inf"), {1: "x"}, object(), "\ud800"):
        with pytest.raises(ContentError):
            content_hash(bad)
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(ContentError):
        content_hash(cyclic)


def test_branch_rejects_unhashable_values_at_write_time():
    b = Workspace().create_branch("A", "a")
    with pytest.raises(ContentError):
        b.set("k", {1, 2})
    with pytest.raises(ContentError):
        b.set(3, "x")


# --- Immutabilité ------------------------------------------------------------------

def test_snapshots_are_really_immutable():
    ws = Workspace()
    ws.seed({"k": [1, 2]})
    before = ws.head.version_id
    ws.head.data["k"].append(3)          # copie : sans effet
    ws.head.data["autre"] = 1
    assert ws.head.data == {"k": [1, 2]} and ws.head.version_id == before
    with pytest.raises(TypeError):
        ws.head.entries["k"] = "x"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ws.head.version_id = "x"


def test_branch_value_is_isolated_from_caller_mutation():
    ws = Workspace()
    value = {"items": [1]}
    b = ws.create_branch("A", "a")
    b.set("k", value)
    value["items"].append(2)             # après set : ne doit pas fuiter dans la branche
    ws.commit("A", b)
    assert ws.head.data["k"] == {"items": [1]}


# --- Replay ---------------------------------------------------------------------------

def test_replay_reconstructs_identical_snapshot_ids():
    ws = scenario(Workspace())
    replayed = Workspace.replay(ws.get_events(), ws.store)
    assert replayed.head.version_id == ws.head.version_id
    assert set(replayed._snapshots) == set(ws._snapshots)
    assert replayed.head.data == ws.head.data
    for vid, snap in ws._snapshots.items():
        assert replayed._snapshots[vid].parents == snap.parents
        assert dict(replayed._snapshots[vid].entries) == dict(snap.entries)


def test_replay_keeps_the_log_and_can_continue_the_chain():
    ws = scenario(Workspace())
    replayed = Workspace.replay(ws.get_events(), ws.store)
    assert [e.event_id for e in replayed.get_events()] == [e.event_id for e in ws.get_events()]
    # même opération des deux côtés => même id
    for w in (ws, replayed):
        b = w.create_branch("Z", "z")
        b.set("suite", 1)
        w.commit("Z", b)
    assert replayed.head.version_id == ws.head.version_id


def test_replay_does_not_need_the_original_objects_in_memory():
    ws = scenario(Workspace())
    from cerbere_cohort import ContentStore
    fresh = ContentStore()
    for digest, text in ws.store.dump().items():
        fresh.load_text(digest, text)
    assert Workspace.replay(ws.get_events(), fresh).head.version_id == ws.head.version_id


def _tamper(events, predicate, **payload_changes):
    out = []
    for e in events:
        if e.action == "workspace.commit" and predicate(e):
            e = CerbereCohortEvent(**{**e.model_dump(), "payload": {**e.payload, **payload_changes}})
        out.append(e)
    return out


def test_replay_detects_a_tampered_version_id():
    ws = scenario(Workspace())
    events = _tamper(ws.get_events(), lambda e: e.payload["merge_type"] == "auto-merge", version_id="0" * 64)
    with pytest.raises(ReplayError, match="id divergent"):
        Workspace.replay(events, ws.store)


def test_replay_detects_tampered_content():
    ws = scenario(Workspace())
    first = next(e for e in ws.get_events() if e.action == "workspace.commit")
    key = next(iter(first.payload["changes"]))
    events = _tamper(ws.get_events(), lambda e: e is first or e.event_id == first.event_id,
                     changes={**first.payload["changes"], key: content_hash("falsifié")})
    with pytest.raises(ReplayError):
        Workspace.replay(events, ws.store)  # objet absent, ou id divergent


def test_replay_detects_missing_object_and_broken_chain():
    ws = scenario(Workspace())
    from cerbere_cohort import ContentStore
    with pytest.raises(ReplayError, match="manquant"):
        Workspace.replay(ws.get_events(), ContentStore())
    commits = [e for e in ws.get_events() if e.action == "workspace.commit"]
    missing_one = [e for e in ws.get_events() if e.event_id != commits[1].event_id]
    with pytest.raises(ReplayError, match="chaîne rompue"):
        Workspace.replay(missing_one, ws.store)


def test_replay_rejects_logs_from_before_the_replayable_format():
    old = CerbereCohortEvent(agent_id="A", action="workspace.commit",
                      payload={"version_id": "x", "parent_version": None, "merge_type": "fast-forward"})
    with pytest.raises(ReplayError, match="format antérieur"):
        Workspace.replay([old], Workspace().store)


# --- Critère 7 : jamais d'écrasement sous concurrence ------------------------------------

def test_concurrent_commits_on_same_key_exactly_one_wins_and_nothing_is_overwritten():
    ws = Workspace()
    ws.seed({"k": "base"})
    branches = [ws.create_branch(f"a{i}", f"b{i}") for i in range(12)]
    for i, b in enumerate(branches):
        b.set("k", f"valeur-{i}")
    results, barrier = [], threading.Barrier(len(branches))

    def run(i, b):
        barrier.wait()
        try:
            ws.commit(f"a{i}", b)
            results.append(("ok", i))
        except ConflictError:
            results.append(("conflict", i))

    threads = [threading.Thread(target=run, args=(i, b)) for i, b in enumerate(branches)]
    [t.start() for t in threads]
    [t.join(timeout=10) for t in threads]
    winners = [i for r, i in results if r == "ok"]
    assert len(winners) == 1 and len(results) == 12
    assert ws.head.data["k"] == f"valeur-{winners[0]}"
    assert Workspace.replay(ws.get_events(), ws.store).head.version_id == ws.head.version_id


def test_concurrent_commits_on_disjoint_keys_all_land():
    ws = Workspace()
    branches = [ws.create_branch(f"a{i}", f"b{i}") for i in range(12)]
    for i, b in enumerate(branches):
        b.set(f"k{i}", i)
    threads = [threading.Thread(target=lambda i=i, b=b: ws.commit(f"a{i}", b)) for i, b in enumerate(branches)]
    [t.start() for t in threads]
    [t.join(timeout=10) for t in threads]
    assert ws.head.data == {f"k{i}": i for i in range(12)}
    assert Workspace.replay(ws.get_events(), ws.store).head.version_id == ws.head.version_id


def test_seed_works_even_when_leases_are_required():
    ws = Workspace(require_leases=True)
    ws.seed({"k": 1})
    assert ws.head.data == {"k": 1}



def test_replay_keeps_legacy_root_key_starting_with_root_key_prefix_literal():
    from cerbere_cohort import PreferBranch

    ws = Workspace()
    key = "root-key:aw"
    ws.seed({key: "base"})
    a, b = ws.create_branch("A", "legacy-key-a"), ws.create_branch("B", "legacy-key-b")
    a.set(key, "head")
    b.set(key, "branch")
    ws.commit("A", a)
    ws.commit("B", b, resolver=PreferBranch())

    # Simulate a journal produced before the v1 encoded root-key format:
    # the old conflict identifier was the literal root key.
    events = []
    encoded = "root-key:v1:cm9vdC1rZXk6YXc"
    for event in ws.get_events():
        payload = dict(event.payload)
        if event.action == "coordination.resolved" and payload.get("key") == encoded:
            payload["key"] = key
        if event.action == "workspace.commit" and encoded in payload.get("resolutions", {}):
            resolutions = dict(payload["resolutions"])
            resolutions[key] = resolutions.pop(encoded)
            payload["resolutions"] = resolutions
        events.append(CerbereCohortEvent(**{**event.model_dump(), "payload": payload}))

    replayed = Workspace.replay(events, ws.store)
    assert replayed.head.data[key] == "branch"
