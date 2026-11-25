import cerbere_cohort.api as api


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.committed = False

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return type("Cursor", (), {
            "fetchone": lambda self: {
                "client_id": params[1],
                "organization_id": params[2],
                "user_id": params[0],
            }
        })()

    def commit(self):
        self.committed = True


def test_mcp_oauth_grant_binds_client_to_authenticated_workspace(monkeypatch):
    db = FakeConnection()
    monkeypatch.setattr(api, "DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(api, "_membership_connection", db)

    result = api.create_mcp_oauth_grant(
        api.MCPAuthorizationGrantCreate(client_id="mcp-client-123"),
        {
            "user_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "organization_id": "22222222-2222-4222-8222-222222222222",
            "role": "member",
        },
    )

    assert result == {
        "authorized": True,
        "client_id": "mcp-client-123",
        "organization_id": "22222222-2222-4222-8222-222222222222",
    }
    sql, params = db.calls[0]
    assert "ON CONFLICT (user_id, client_id)" in sql
    assert params == (
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "mcp-client-123",
        "22222222-2222-4222-8222-222222222222",
    )
    assert db.committed is True
