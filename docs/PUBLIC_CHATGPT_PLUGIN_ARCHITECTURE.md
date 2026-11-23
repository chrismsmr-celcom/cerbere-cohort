# Public ChatGPT / Codex plugin architecture

Status: proposed architecture, not yet implemented.
Branch: `feat/public-chatgpt-plugin`

## Goal

Publish one Cohort integration for ChatGPT and Codex that serves both:
- Cohort workspace owners and team members who operate coordination state.
- Developers who build and debug multi-agent workflows with Cohort.

The plugin must use the existing Cohort product and tenant model. It must not create a parallel coordination engine or claim capabilities that Cohort does not ship.

## Repository facts verified on 2026-11-23

- The hosted REST API is FastAPI in `python/src/cerbere_cohort/api.py`.
- Human API authentication uses Supabase access JWTs, verified against Supabase JWKS. Workspace access is checked against active `cohort_memberships`.
- The API supports listing a user's workspaces, workspace profile/overview, state, branches, diffs, commits, conflict inspection/resolution, leases, and events.
- The existing hosted MCP server is in `integrations/mcp-server`. Its remote Streamable HTTP middleware currently accepts Cohort agent credentials (`coh_...`) and derives organization, agent identity, and scopes from that credential.
- The REST API and MCP service currently have separate branch registries/runtime caches. The deployment docs explicitly warn against concurrent REST and MCP writes to the same workspace.
- The existing MCP service is therefore not ready to be connected directly to a human-facing public ChatGPT plugin.
- Cohort currently provides shared key-value state, branches, conflict-aware commits, leases, fencing tokens, and event history. It does not currently expose a first-class task queue or agent-presence/status API. The public plugin must not advertise those as shipped capabilities.

## Recommended public authentication

Use OAuth 2.1 Authorization Code with PKCE (S256) for the public ChatGPT/Codex plugin.

Supabase remains Cohort's identity provider and source of user identity. The public MCP integration needs an OAuth authorization layer that:
1. Sends the user through Cohort's existing Supabase sign-in experience.
2. Lets the user select one of their active Cohort workspaces.
3. Verifies the Supabase JWT and active membership server-side.
4. Issues short-lived, revocable Cohort OAuth access tokens scoped to that user and selected workspace.
5. Supports refresh-token rotation and revocation.
6. Never exposes Supabase service-role keys, database credentials, or shared agent credentials to ChatGPT clients.

Do not reuse a workspace-wide API key or a single agent credential for all plugin users. Do not treat a user as an agent or silently impersonate a production agent.

The exact OAuth issuer and token storage design must be implemented and tested before the public package declares OAuth. Prefer an issuer under a Cohort-controlled HTTPS origin. Store token hashes where possible; protect any refresh/session material at rest. Enforce PKCE, state, exact redirect URI matching, short authorization-code expiry, one-time code redemption, and audience/scope validation.

## Request and tenant flow

```text
ChatGPT / Codex
    -> OAuth authorization (Cohort sign-in + workspace selection)
    -> Cohort OAuth access token (user + workspace + scopes)
    -> Public Streamable HTTP MCP endpoint
    -> authenticated user/workspace context
    -> existing Cohort REST API / shared runtime
    -> PostgreSQL tenant schema + membership/role checks
```

Every tool invocation must derive the user and workspace from the verified access token. Never trust a workspace ID supplied only as a tool argument. If the user has multiple workspaces, the selected workspace is part of the authorized token context; changing workspace requires a new authorization/selection flow.

## Runtime consistency requirement

Do not keep an independent MCP branch registry for public plugin writes.

Preferred implementation: make the MCP adapter call the existing REST API using a server-verified user/workspace context, or mount the MCP adapter in the same API runtime and reuse the same branch registry and locks. There must be one authoritative branch/lease state path. Until this is true, keep write tools disabled and expose read-only tools only.

The current API is deployed as one instance and one Uvicorn worker because runtime/branch registries are process-local. The plugin must respect that constraint; it must not introduce another writer process that races the API.

## Initial tool scope

### Read tools
- `cohort_list_workspaces` — list workspaces where the authenticated user has active membership.
- `cohort_get_overview` — read the selected workspace overview and HEAD.
- `cohort_get_state` — inspect committed key-value state.
- `cohort_list_branches` — inspect branches.
- `cohort_get_branch_diff` — inspect pending branch changes.
- `cohort_list_leases` — inspect active resource leases.
- `cohort_get_events` — inspect recent coordination events, filtered by workspace authorization.

### Write tools (enable only after shared-runtime consistency and audit checks pass)
- `cohort_create_branch`
- `cohort_update_branch_state`
- `cohort_commit_branch`
- `cohort_resolve_conflict`
- `cohort_acquire_lease`
- `cohort_release_lease`

Write tools must have accurate MCP annotations, strict argument validation, role/scope checks, idempotency where appropriate, and explicit user intent. Conflict errors must be returned as structured outcomes; the plugin must never auto-retry a stale commit or silently select a conflict winner.

Do not include task creation, task scheduling, agent presence/status, or external tool execution in the initial release; those are not currently supported by the inspected Cohort API.

## Public package

- One universal production HTTPS MCP endpoint.
- OAuth authentication only; no embedded bearer headers or shared secrets.
- A concise Cohort skill explaining the coordination model, safe branch workflow, lease handling, and conflict resolution.
- No custom UI in v1 unless testing shows a concrete need.
- Listing name, description, icon, website, support, privacy, and terms URLs must be verified before submission.
- Prepare exactly five positive and three negative MCP review cases, release notes, and a recorded demo.
- Use a dedicated reviewer account and synthetic workspace data; never provide a founder's personal account.

## Delivery phases

1. **Audit and contract:** reconcile current API/MCP behavior and write tests for all public tool contracts.
2. **OAuth:** implement authorization metadata, authorization-code + PKCE, token exchange, refresh rotation, revocation, and workspace selection.
3. **MCP adapter:** implement user-authenticated tools against the authoritative API/runtime; keep read/write scopes separate.
4. **Security and consistency:** tenant-isolation tests, role tests, replay/expiry/revocation tests, concurrent branch/lease tests, audit attribution.
5. **Package and private testing:** build the plugin package and test it in ChatGPT/Codex with synthetic data.
6. **Public review:** complete verified listing information, demo, review cases, domain verification, scans, and submit for review. Publication occurs only after approval and an explicit publish action.

## Open implementation checks

- Confirm the live MCP service URL and its current deployment before changing routing.
- Confirm the actual production OAuth redirect and domain configuration.
- Reconcile stale deployment documentation that says remote MCP auth is not implemented with the current agent-token middleware.
- Confirm whether the public plugin will be published under an individually verified developer identity or a verified business identity.
