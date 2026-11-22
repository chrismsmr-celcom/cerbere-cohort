from cerbere_cohort import api
from cerbere_cohort.events import CerbereCohortEvent


def test_human_actor_is_recorded_separately_from_target_agent(monkeypatch):
    event = CerbereCohortEvent(
        agent_id="worker-b",
        action="workspace.commit",
        payload={"branch_name": "feature"},
    )

    class Runtime:
        _events = [event]

    class Connection:
        def __init__(self):
            self.updates = []
            self.committed = False

        def execute(self, sql, params):
            self.updates.append((sql, params))

        def commit(self):
            self.committed = True

    connection = Connection()

    class WorkspaceProxy:
        _workspace = Runtime()
        _conn = connection

    monkeypatch.setattr(api, "workspace", WorkspaceProxy())
    api._stamp_event_actor(
        {"user_id": "user-123", "role": "member"},
        previous_ids=set(),
    )

    stamped = WorkspaceProxy._workspace._events[0]
    assert stamped.agent_id == "worker-b"
    assert stamped.metadata["actor"] == {
        "type": "user",
        "id": "user-123",
        "credential_id": None,
    }
    assert connection.committed is True
    assert len(connection.updates) == 1
    assert connection.updates[0][1][1] == event.event_id


def test_agent_actor_metadata_includes_credential_identity(monkeypatch):
    event = CerbereCohortEvent(
        agent_id="worker-a",
        action="workspace.branch",
        payload={"branch_name": "feature"},
    )

    class Runtime:
        _events = [event]

    class Connection:
        def __init__(self):
            self.updates = []

        def execute(self, sql, params):
            self.updates.append(params)

        def commit(self):
            pass

    connection = Connection()

    class WorkspaceProxy:
        _workspace = Runtime()
        _conn = connection

    monkeypatch.setattr(api, "workspace", WorkspaceProxy())
    api._stamp_event_actor(
        {
            "principal_type": "agent",
            "agent_id": "worker-a",
            "credential_id": "credential-1",
        },
        previous_ids=set(),
    )

    assert WorkspaceProxy._workspace._events[0].metadata["actor"] == {
        "type": "agent",
        "id": "worker-a",
        "credential_id": "credential-1",
    }
