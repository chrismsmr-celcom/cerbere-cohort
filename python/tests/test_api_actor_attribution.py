from cerbere_cohort.events import CerbereCohortEvent, event_actor


def test_human_actor_is_recorded_separately_from_target_agent():
    with event_actor({"user_id": "user-123", "role": "member"}):
        event = CerbereCohortEvent(
            agent_id="worker-b",
            action="workspace.commit",
            payload={"branch_name": "feature"},
        )

    assert event.agent_id == "worker-b"
    assert event.metadata["actor"] == {
        "type": "user",
        "id": "user-123",
        "credential_id": None,
    }
    # Attribution is part of the serialized event, so persistence/replay retains it.
    restored = CerbereCohortEvent.from_jsonl(event.to_jsonl())
    assert restored.metadata == event.metadata


def test_agent_actor_metadata_includes_credential_identity():
    with event_actor({
        "principal_type": "agent",
        "agent_id": "worker-a",
        "credential_id": "credential-1",
    }):
        event = CerbereCohortEvent(
            agent_id="worker-a",
            action="workspace.branch",
            payload={"branch_name": "feature"},
        )

    assert event.metadata["actor"] == {
        "type": "agent",
        "id": "worker-a",
        "credential_id": "credential-1",
    }


def test_actor_context_does_not_leak_between_requests():
    with event_actor({"user_id": "user-123"}):
        attributed = CerbereCohortEvent(agent_id="worker-a", action="workspace.branch")

    unattributed = CerbereCohortEvent(agent_id="system", action="workspace.seed")
    assert attributed.metadata["actor"]["id"] == "user-123"
    assert unattributed.metadata is None


def test_existing_event_metadata_is_preserved():
    with event_actor({"user_id": "user-123"}):
        event = CerbereCohortEvent(
            agent_id="worker-a",
            action="workspace.branch",
            metadata={"source": "api"},
        )

    assert event.metadata["source"] == "api"
    assert event.metadata["actor"]["id"] == "user-123"



def test_replayed_event_is_not_rewritten_by_active_request_actor():
    with event_actor({"user_id": "current-user"}):
        historical = CerbereCohortEvent(
            event_id="historical-event-id",
            agent_id="worker-old",
            action="workspace.commit",
            metadata=None,
        )

    assert historical.metadata is None
