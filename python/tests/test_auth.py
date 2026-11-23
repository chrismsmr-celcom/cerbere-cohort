from fastapi import HTTPException

from cerbere_cohort.auth import require_workspace_member, require_workspace_writer


def test_local_development_bypasses_supabase(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    context = require_workspace_member(authorization=None, x_organization_id=None)
    assert context["user_id"] == "local"
    assert context["role"] == "owner"


def test_production_requires_organization_header(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    try:
        require_workspace_member(authorization=None, x_organization_id=None)
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "X-Organization-ID" in exc.detail
    else:
        raise AssertionError("Missing organization header must be rejected")


def test_user_can_access_any_workspace_with_active_membership(monkeypatch):
    import cerbere_cohort.auth as auth

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.delenv("COHORT_ORGANIZATION_ID", raising=False)
    monkeypatch.setattr(auth, "_decode_access_token", lambda token: {
        "sub": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "email": "owner@example.com"
    })
    monkeypatch.setattr(auth, "_membership_cache", {})
    db = _FakeMembershipDB(role="member")
    monkeypatch.setattr(auth, "_membership_db", db)

    result = require_workspace_member(
        authorization="Bearer valid-token",
        x_organization_id="22222222-2222-4222-8222-222222222222",
    )

    assert result["organization_id"] == "22222222-2222-4222-8222-222222222222"
    assert result["role"] == "member"
    assert db.calls[0][1][0] == "22222222-2222-4222-8222-222222222222"


def test_viewer_cannot_write():
    try:
        require_workspace_writer({"user_id": "u", "role": "viewer"})
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("Viewer role must be read-only")


def test_member_can_write():
    context = {"user_id": "u", "role": "member"}
    assert require_workspace_writer(context) == context


class _FakeMembershipDB:
    def __init__(self, role="owner", error=None):
        self.role = role
        self.error = error
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if self.error:
            raise self.error
        return _FakeCursor((self.role,))


class _FakeCursor:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


def test_production_membership_uses_configured_runtime_connection(monkeypatch):
    import cerbere_cohort.auth as auth

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("COHORT_ORGANIZATION_ID", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(auth, "_decode_access_token", lambda token: {
        "sub": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "email": "owner@example.com"
    })
    monkeypatch.setattr(auth, "_membership_cache", {})
    db = _FakeMembershipDB(role="owner")
    monkeypatch.setattr(auth, "_membership_db", db)

    result = require_workspace_member(
        authorization="Bearer valid-token",
        x_organization_id="11111111-1111-4111-8111-111111111111",
    )

    assert result["role"] == "owner"
    assert len(db.calls) == 1
    assert "public.cohort_memberships" in db.calls[0][0]
    assert db.calls[0][1] == (
        "11111111-1111-4111-8111-111111111111",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )


def test_production_membership_db_failure_is_fail_closed(monkeypatch):
    import cerbere_cohort.auth as auth

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("COHORT_ORGANIZATION_ID", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(auth, "_decode_access_token", lambda token: {
        "sub": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    })
    monkeypatch.setattr(auth, "_membership_cache", {})
    monkeypatch.setattr(auth, "_membership_db", _FakeMembershipDB(error=RuntimeError("database unavailable")))

    try:
        require_workspace_member(
            authorization="Bearer valid-token",
            x_organization_id="11111111-1111-4111-8111-111111111111",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert exc.detail == "Unable to verify organization membership"
    else:
        raise AssertionError("Database failures must fail closed")


def test_agent_credential_authentication_is_org_scoped_and_updates_last_used(monkeypatch):
    import cerbere_cohort.auth as auth

    class CredentialDB:
        def __init__(self):
            self.calls = []
            self.committed = False

        def execute(self, sql, params):
            self.calls.append((sql, params))
            return _FakeCursor({
                "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "organization_id": "11111111-1111-4111-8111-111111111111",
                "agent_id": "worker-1",
                "scopes": ["workspace:read", "leases:read"],
            })

        def commit(self):
            self.committed = True

    db = CredentialDB()
    monkeypatch.setattr(auth, "_membership_db", db)
    result = auth.authenticate_agent_token(
        "coh_test-secret",
        "11111111-1111-4111-8111-111111111111",
    )

    assert result["principal_type"] == "agent"
    assert result["agent_id"] == "worker-1"
    assert result["scopes"] == ["workspace:read", "leases:read"]
    assert db.calls[0][1][1] == "11111111-1111-4111-8111-111111111111"
    assert "revoked_at IS NULL" in db.calls[0][0]
    assert "expires_at > now()" in db.calls[0][0]
    assert db.committed is True


def test_agent_credential_authentication_rejects_bad_prefix_before_database(monkeypatch):
    import cerbere_cohort.auth as auth

    db = _FakeMembershipDB()
    monkeypatch.setattr(auth, "_membership_db", db)
    try:
        auth.authenticate_agent_token("not-an-agent-token", "11111111-1111-4111-8111-111111111111")
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("Invalid agent token prefix must be rejected")
    assert db.calls == []


def test_agent_credential_authentication_rejects_unknown_or_revoked_token(monkeypatch):
    import cerbere_cohort.auth as auth

    class EmptyDB:
        def execute(self, sql, params):
            return _FakeCursor(None)

        def commit(self):
            pass

    monkeypatch.setattr(auth, "_membership_db", EmptyDB())
    try:
        auth.authenticate_agent_token("coh_unknown-secret", "11111111-1111-4111-8111-111111111111")
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("Unknown credentials must be rejected")


def test_agent_scope_dependency_accepts_only_requested_scope(monkeypatch):
    import cerbere_cohort.auth as auth

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("COHORT_ORGANIZATION_ID", "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(auth, "authenticate_agent_token", lambda token, org: {
        "principal_type": "agent",
        "agent_id": "worker-a",
        "organization_id": org,
        "scopes": ["workspace:read"],
    })
    reader = auth.require_workspace_scope("workspace:read")
    context = reader(
        authorization="Bearer coh_valid",
        x_organization_id="11111111-1111-4111-8111-111111111111",
    )
    assert context["agent_id"] == "worker-a"

    writer = auth.require_workspace_scope("workspace:write")
    try:
        writer(
            authorization="Bearer coh_valid",
            x_organization_id="11111111-1111-4111-8111-111111111111",
        )
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("An agent without workspace:write must be rejected")


def test_scope_dependency_keeps_supabase_member_auth(monkeypatch):
    import cerbere_cohort.auth as auth

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    expected = {"user_id": "user-1", "organization_id": "org-1", "role": "member"}
    monkeypatch.setattr(auth, "require_workspace_member", lambda authorization, org: expected)
    dependency = auth.require_workspace_scope("workspace:write")
    result = dependency(
        authorization="Bearer supabase-jwt",
        x_organization_id="11111111-1111-4111-8111-111111111111",
    )
    assert result is expected



def test_agent_resource_ownership_rejects_cross_agent_access():
    from cerbere_cohort.api import _agent_owns, _require_agent_identity

    owner = {"principal_type": "agent", "agent_id": "worker-a"}
    assert _agent_owns(owner, "worker-a") is True
    assert _agent_owns(owner, "worker-b") is False

    try:
        _require_agent_identity(owner, "worker-b")
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("An agent must not impersonate another agent")


def test_workspace_member_can_manage_any_agent_resource():
    from cerbere_cohort.api import _agent_owns, _require_agent_identity

    member = {"user_id": "user-1", "role": "member"}
    assert _agent_owns(member, "worker-b") is True
    assert _require_agent_identity(member, "worker-b") is None



def test_agent_credential_authentication_can_resolve_tenant_from_unique_token(monkeypatch):
    import cerbere_cohort.auth as auth

    class CredentialDB:
        def __init__(self):
            self.calls = []
            self.committed = False

        def execute(self, sql, params):
            self.calls.append((sql, params))
            return _FakeCursor({
                "id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "organization_id": "22222222-2222-4222-8222-222222222222",
                "agent_id": "remote-agent",
                "scopes": ["workspace:read", "workspace:write"],
            })

        def commit(self):
            self.committed = True

    db = CredentialDB()
    monkeypatch.setattr(auth, "_membership_db", db)

    result = auth.authenticate_agent_token("coh_global-lookup")

    assert result["organization_id"] == "22222222-2222-4222-8222-222222222222"
    assert result["agent_id"] == "remote-agent"
    assert len(db.calls) == 1
    assert "WHERE token_hash = ?" in db.calls[0][0]
    assert "organization_id = ?" not in db.calls[0][0]
    assert len(db.calls[0][1]) == 1
    assert db.committed is True
