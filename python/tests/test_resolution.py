"""M2 : résolution pluggable. Invariant : tout conflit finit par une résolution OU un abandon enregistrés."""
import random
import threading

import pytest

from cerbere_cohort import (CerbereCohortEvent, CallbackResolver, ConflictError, PreferAgent, PreferBranch, PreferHead,
                    Resolution, ResolutionError, ResolverChain, ReplayError, Workspace, content_hash)


def collide(ws=None, a_intent=None, b_intent=None):
    """HEAD = {spec: 'par A', note: 'n'} ; la branche de B a changé spec ET ajouté 'extra'."""
    ws = ws or Workspace()
    ws.seed({"spec": "v1", "note": "n"})
    a, b = ws.create_branch("A", "a"), ws.create_branch("B", "b")
    a.set("spec", "par A", intent=a_intent)
    b.set("spec", "par B", intent=b_intent)
    b.set("extra", 1)
    ws.commit("A", a)
    return ws, b


def acts(ws):
    return [e.action for e in ws.get_events()]


def test_without_resolver_conflict_is_recorded_and_aborted():
    ws, b = collide()
    head = ws.head.version_id
    with pytest.raises(ConflictError):
        ws.commit("B", b)
    assert ws.head.version_id == head
    assert acts(ws).count("coordination.conflict") == 1
    abort = [e for e in ws.get_events() if e.action == "coordination.aborted"]
    assert len(abort) == 1 and abort[0].payload["reason"] == "no_resolver"
    assert abort[0].payload["unresolved_keys"] == ["spec"]


def test_prefer_branch_resolves_and_still_merges_clean_keys():
    ws, b = collide()
    ws.commit("B", b, resolver=PreferBranch())
    assert ws.head.data == {"spec": "par B", "note": "n", "extra": 1}
    resolved = [e for e in ws.get_events() if e.action == "coordination.resolved"]
    assert len(resolved) == 1
    p = resolved[0].payload
    assert p["key"] == "spec" and p["resolver"] == "prefer-branch" and p["choice_hash"] == content_hash("par B")
    commit = [e for e in ws.get_events() if e.action == "workspace.commit"][-1]
    assert commit.payload["merge_type"] == "resolved"
    assert commit.payload["resolutions"] == {"spec": content_hash("par B")}


def test_prefer_head_keeps_head_but_applies_the_branch_clean_changes():
    ws, b = collide()
    ws.commit("B", b, resolver=PreferHead())
    assert ws.head.data == {"spec": "par A", "note": "n", "extra": 1}


def test_resolver_sees_base_both_sides_agents_and_intents():
    ws, b = collide(a_intent="durcir la spec", b_intent="alléger la spec")
    seen = []

    def spy(conflict):
        seen.append(conflict)
        return Resolution.take(conflict.ours, "test")

    ws.commit("B", b, resolver=CallbackResolver(spy, "spy"))
    (c,) = seen
    assert c.key == "spec"
    assert (c.base.present, c.base.value) == (True, "v1")
    assert (c.ours.value, c.ours.agent_id, c.ours.intent) == ("par B", "B", "alléger la spec")
    assert (c.theirs.value, c.theirs.agent_id, c.theirs.intent) == ("par A", "A", "durcir la spec")


def test_intents_are_recorded_on_the_commit():
    ws = Workspace()
    b = ws.create_branch("A", "a")
    b.set("k", 1, intent="initialiser k")
    ws.commit("A", b)
    commit = [e for e in ws.get_events() if e.action == "workspace.commit"][-1]
    assert commit.payload["intents"] == {"k": "initialiser k"}


def test_delete_vs_modify_can_be_resolved_by_deleting():
    ws = Workspace()
    ws.seed({"k": "x"})
    a, b = ws.create_branch("A", "a"), ws.create_branch("B", "b")
    a.delete("k")
    b.set("k", "z")
    ws.commit("A", a)
    ws.commit("B", b, resolver=CallbackResolver(lambda c: Resolution.delete("la suppression prime"), "r"))
    assert "k" not in ws.head.data
    resolved = [e for e in ws.get_events() if e.action == "coordination.resolved"][0]
    assert resolved.payload["choice_hash"] is None


def test_arbiter_can_produce_new_content():
    ws, b = collide()
    merge_text = CallbackResolver(
        lambda c: Resolution.with_value(f"{c.theirs.value} + {c.ours.value}", "fusion par l'arbitre"), "arbiter")
    ws.commit("B", b, resolver=merge_text)
    assert ws.head.data["spec"] == "par A + par B"
    assert ws.store.has(content_hash("par A + par B"))


def test_unresolved_aborts_everything_and_writes_nothing():
    ws, b = collide()
    head = ws.head.version_id
    with pytest.raises(ConflictError) as exc:
        ws.commit("B", b, resolver=CallbackResolver(lambda c: None, "indécis"))
    assert exc.value.conflicting_keys == ["spec"]
    assert ws.head.version_id == head and "extra" not in ws.head.data
    assert "coordination.resolved" not in acts(ws)
    abort = [e for e in ws.get_events() if e.action == "coordination.aborted"][0]
    assert abort.payload["reason"] == "unresolved" and abort.payload["resolver"] == "indécis"


def test_chain_falls_through_to_the_next_resolver():
    ws, b = collide()
    chain = ResolverChain(CallbackResolver(lambda c: None, "muet"), PreferBranch())
    ws.commit("B", b, resolver=chain)
    assert ws.head.data["spec"] == "par B"
    resolved = [e for e in ws.get_events() if e.action == "coordination.resolved"][0]
    assert resolved.payload["resolver"] == "chain(muet,prefer-branch)"


def test_prefer_agent_uses_priority_and_refuses_when_unknown():
    ws, b = collide()
    ws.commit("B", b, resolver=PreferAgent(["A", "B"]))
    assert ws.head.data["spec"] == "par A"
    ws2, b2 = collide()
    with pytest.raises(ConflictError):
        ws2.commit("B", b2, resolver=PreferAgent(["X", "Y"]))


def test_resolver_exception_aborts_cleanly():
    ws, b = collide()
    head = ws.head.version_id

    def boom(c):
        raise RuntimeError("modèle indisponible")

    with pytest.raises(ResolutionError, match="modèle indisponible"):
        ws.commit("B", b, resolver=CallbackResolver(boom, "llm"))
    assert ws.head.version_id == head
    abort = [e for e in ws.get_events() if e.action == "coordination.aborted"][0]
    assert abort.payload["reason"] == "resolver_error"


def test_invalid_decision_is_rejected():
    ws, b = collide()
    with pytest.raises(ResolutionError):
        ws.commit("B", b, resolver=CallbackResolver(lambda c: Resolution.with_value({1, 2}), "mauvais"))
    with pytest.raises(ResolutionError, match="Resolution"):
        ws.commit("B", b, resolver=CallbackResolver(lambda c: "ours", "mauvais2"))


# --- Règle de déterminisme --------------------------------------------------------------------

def test_replay_applies_recorded_decision_and_never_calls_a_resolver():
    # Arbitre NON déterministe : il tire au hasard. Le replay doit quand même retrouver les mêmes ids.
    rng = random.Random()
    arbiter = CallbackResolver(lambda c: Resolution.take(rng.choice([c.ours, c.theirs]), "tirage"), "dice")
    outcomes = set()
    for _ in range(30):
        ws, b = collide()
        ws.commit("B", b, resolver=arbiter)
        replayed = Workspace.replay(ws.get_events(), ws.store)
        assert replayed.head.version_id == ws.head.version_id
        assert replayed.head.data == ws.head.data
        outcomes.add(ws.head.data["spec"])
    assert outcomes == {"par A", "par B"}  # l'arbitre a bien varié, le replay jamais


def test_replay_rejects_a_log_whose_recorded_decision_disagrees():
    ws, b = collide()
    ws.commit("B", b, resolver=PreferBranch())
    events = []
    for e in ws.get_events():
        if e.action == "workspace.commit" and e.payload["merge_type"] == "resolved":
            e = CerbereCohortEvent(**{**e.model_dump(), "payload": {**e.payload, "resolutions": {"spec": content_hash("autre")}}})
        events.append(e)
    with pytest.raises(ReplayError, match="décision enregistrée incohérente"):
        Workspace.replay(events, ws.store)


def test_logs_from_before_m2_still_replay():
    ws = Workspace()
    ws.seed({"k": 1})
    old = []
    for e in ws.get_events():
        if e.action == "workspace.commit":
            payload = {k: v for k, v in e.payload.items() if k not in ("resolutions", "intents")}
            e = CerbereCohortEvent(**{**e.model_dump(), "payload": payload})
        old.append(e)
    assert Workspace.replay(old, ws.store).head.version_id == ws.head.version_id


# --- Concurrence : le résolveur est appelé HORS du verrou ------------------------------------

def test_slow_resolver_does_not_block_other_writers_and_head_move_triggers_a_retry():
    ws, b = collide()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(conflict):
        calls.append(conflict.theirs.value)
        entered.set()
        assert release.wait(timeout=5)
        return Resolution.take(conflict.ours, "lent")

    result = {}
    t = threading.Thread(target=lambda: result.update(snap=ws.commit("B", b, resolver=CallbackResolver(slow, "slow"))))
    t.start()
    assert entered.wait(timeout=5)

    # pendant que l'arbitre « réfléchit », un autre agent commit : ça ne doit PAS bloquer
    c = ws.create_branch("C", "c")
    c.set("autre", "valeur de C")
    done = threading.Event()
    threading.Thread(target=lambda: (ws.commit("C", c), done.set())).start()
    assert done.wait(timeout=2), "le résolveur lent bloque les autres écrivains"

    release.set()
    t.join(timeout=10)
    assert ws.head.data == {"spec": "par B", "note": "n", "extra": 1, "autre": "valeur de C"}
    assert len(calls) == 2  # HEAD a bougé : fusion refaite, arbitre rappelé sur le nouveau HEAD
    assert Workspace.replay(ws.get_events(), ws.store).head.version_id == ws.head.version_id


def test_gives_up_without_overwriting_if_head_keeps_moving():
    ws, b = collide()
    counter = {"n": 0}

    def moving(conflict):
        counter["n"] += 1
        other = ws.create_branch("M", f"m{counter['n']}")
        other.set(f"k{counter['n']}", counter["n"])
        ws.commit("M", other)  # HEAD bouge à chaque décision
        return Resolution.take(conflict.ours, "x")

    with pytest.raises(ConflictError, match="rien n'a été écrasé"):
        ws.commit("B", b, resolver=CallbackResolver(moving, "mobile"), max_retries=2)
    assert ws.head.data["spec"] == "par A" and "extra" not in ws.head.data
    abort = [e for e in ws.get_events() if e.action == "coordination.aborted"][-1]
    assert abort.payload["reason"] == "head_kept_moving"


def test_nested_path_resolution_is_applied_and_replayed():
    ws = Workspace()
    ws.seed({"config": {"status": "pending", "owner": "A"}})
    a, b = ws.create_branch("A", "nested-a"), ws.create_branch("B", "nested-b")
    a.set("config", {"status": "completed", "owner": "A"})
    b.set("config", {"status": "failed", "owner": "B"})
    ws.commit("A", a)

    ws.commit("B", b, resolver=PreferBranch())

    assert ws.head.data["config"] == {"status": "failed", "owner": "B"}
    resolution = [
        event for event in ws.get_events()
        if event.action == "coordination.resolved"
    ][0]
    assert resolution.payload["key"] == "json-pointer:/config/status"
    replayed = Workspace.replay(ws.get_events(), ws.store)
    assert replayed.head.version_id == ws.head.version_id
    assert replayed.head.data == ws.head.data
