import threading
import time

import pytest

from cerbere_cohort import DeadlockError, LeaseError, LeaseManager, Workspace


def test_lease_required_only_when_enabled():
    ws = Workspace(require_leases=True)
    b = ws.create_branch("A", "a")
    with pytest.raises(LeaseError):
        b.set("k", 1)
    ws.acquire_lease("A", "k", 5)
    b.set("k", 1)


def test_second_agent_is_refused_then_gets_lease_after_expiry():
    lm = LeaseManager()
    lm.acquire("A", "X", duration_seconds=0.2)
    with pytest.raises(LeaseError):
        lm.acquire("B", "X")
    time.sleep(0.3)
    assert lm.acquire("B", "X").agent_id == "B"


def test_only_holder_can_release():
    lm = LeaseManager()
    lm.acquire("A", "X")
    with pytest.raises(LeaseError):
        lm.release("B", "X")


def test_waiter_gets_resource_when_holder_releases():
    lm = LeaseManager()
    lm.acquire("A", "X", duration_seconds=30)
    got = {}

    def waiter():
        t = time.monotonic()
        got["lease"] = lm.acquire("B", "X", wait=True, timeout_seconds=3)
        got["elapsed"] = time.monotonic() - t

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.2)
    lm.release("A", "X")  # ne doit pas être bloqué par l'attente de B
    th.join(timeout=5)
    assert got["lease"].agent_id == "B"
    assert got["elapsed"] < 1.5


def test_wait_times_out():
    lm = LeaseManager()
    lm.acquire("A", "X", duration_seconds=30)
    with pytest.raises(LeaseError):
        lm.acquire("B", "X", wait=True, timeout_seconds=0.2)


def test_two_agent_deadlock_is_detected_quickly():
    lm = LeaseManager()
    lm.acquire("A", "X", 60)
    lm.acquire("B", "Y", 60)
    outcome = {}

    def a_waits_for_y():
        try:
            lm.acquire("A", "Y", wait=True, timeout_seconds=3)
        except Exception as e:  # noqa: BLE001
            outcome["A"] = e

    th = threading.Thread(target=a_waits_for_y)
    th.start()
    time.sleep(0.2)
    t = time.monotonic()
    with pytest.raises(DeadlockError):
        lm.acquire("B", "X", wait=True, timeout_seconds=3)
    assert time.monotonic() - t < 1.0  # détecté tout de suite, pas après le timeout
    lm.release("B", "Y")  # B abandonne : A peut alors obtenir Y
    th.join(timeout=5)
    assert "A" not in outcome


def test_three_agent_cycle_is_detected():
    lm = LeaseManager()
    for agent, res in (("A", "X"), ("B", "Y"), ("C", "Z")):
        lm.acquire(agent, res, 60)
    threads = []
    for agent, res in (("A", "Y"), ("B", "Z")):
        th = threading.Thread(
            target=lambda a=agent, r=res: _swallow(lambda: lm.acquire(a, r, wait=True, timeout_seconds=2))
        )
        th.start()
        threads.append(th)
        time.sleep(0.15)
    with pytest.raises(DeadlockError):
        lm.acquire("C", "X", wait=True, timeout_seconds=2)
    for th in threads:
        th.join(timeout=5)


def _swallow(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        pass


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan")])
def test_invalid_lease_duration_is_rejected(duration):
    lm = LeaseManager()
    with pytest.raises(LeaseError) as exc:
        lm.acquire("agent-a", "resource", duration_seconds=duration)
    assert exc.value.code == "invalid_lease_duration"


def test_expiry_boundary_is_inclusive():
    from cerbere_cohort.lease import Lease
    lease = Lease("agent-a", "resource", expires_at=time.time(), acquired_at=time.time())
    assert lease.is_expired()


def test_same_owner_renews_lease_without_changing_fencing_token():
    lm = LeaseManager()
    first = lm.acquire("agent-a", "resource", duration_seconds=1)
    original_expiry = first.expires_at
    renewed = lm.acquire("agent-a", "resource", duration_seconds=30)
    assert renewed.fencing_token == first.fencing_token
    assert renewed.expires_at > original_expiry
    assert lm.get_events()[-1].action == "lease.renew"



def test_lease_validation_detects_boolean_write_over_integer():
    ws = Workspace(require_leases=True)
    ws.seed({"k": 1})
    branch = ws.create_branch("agent-a", "type-change-without-lease")
    # Simulate a stale or bypassed client mutating the public working tree.
    # Commit-time validation must not treat True as the unchanged integer 1.
    branch.current_data["k"] = True

    with pytest.raises(LeaseError) as exc:
        ws.commit("agent-a", branch)

    assert exc.value.code == "lease_fenced"
