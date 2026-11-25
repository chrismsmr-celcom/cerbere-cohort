# Cerbere Cohort MCP Server

Cerbere Cohort exposes shared workspace coordination to MCP-compatible coding
agents. Run **one server process** and connect multiple clients to the same
Streamable HTTP endpoint. The server serializes workspace mutations and stores
committed state, leases, events, open branches, and tasks in SQLite.

## What it provides

- Shared committed workspace state and versioned HEAD.
- Isolated branches per `agent_id` and `branch_name`, stored in the same durable registry as REST API branches.
- Branch reads, writes, and deletes.
- Conflict-aware commits: overlapping edits are rejected rather than silently
  overwriting another agent's changes; disjoint edits can merge.
- SQLite persistence, including unfinished branch state across server restarts.
- Resource leases with renewal, fencing tokens, and an append-only event log.
- Durable task lifecycle (`pending → assigned → running → completed/failed`) with task leases and fencing tokens.
- Atomic task-bound branch commits: stale task owners cannot publish workspace state.
- Streamable HTTP for multiple clients, plus stdio for single-client setups.

## Requirements

- Python 3.10+
- An MCP-compatible client
- One local checkout of this repository

## Install from the repository root

Install the Python library and MCP server in editable mode:

```bash
python -m pip install -e ./python
python -m pip install -e ./integrations/mcp-server
```

## Start the local development server

```bash
cerbere-cohort-mcp --transport streamable-http
```

Default endpoint:

```text
http://127.0.0.1:8765/mcp
```

The default database is `~/.cerbere-cohort/workspace.db`. Configure it with
`COHORT_DB_PATH` or `--db`.

Unauthenticated SQLite HTTP is restricted to loopback. Do not bind it to
`0.0.0.0` or expose it through a public reverse proxy.

Useful options:

```bash
cerbere-cohort-mcp --help
cerbere-cohort-mcp --db ./data/cohort.db --host 127.0.0.1 --port 8765
```

Keep the server running while clients are connected. Point Cursor, Codex,
Mistral Code, or another MCP client at the same endpoint. Client configuration
syntax varies; use its Streamable HTTP MCP transport and the URL above.

**Security boundary:** local Streamable HTTP remains unauthenticated and must stay on loopback. Do not expose it to a LAN, public interface, or the internet.

## Remote Streamable HTTP on Render

For a public MCP endpoint, deploy one MCP Web Service. The server resolves the
organization from each globally unique agent credential and routes that request
to the matching PostgreSQL tenant schema. Multiple organizations and multiple
agents can share the same endpoint without sharing workspace state.

Set these Render environment variables:

- `DATABASE_URL`: the Cohort Supabase PostgreSQL connection string.
- `COHORT_ORGANIZATION_ID`: optional organization UUID restriction. Leave unset to let each credential select its own organization.
- `COHORT_MCP_MAX_TENANTS`: optional in-memory tenant-service cache limit; defaults to `64`. Idle tenant services are evicted and restored from PostgreSQL when needed.
- `COHORT_MCP_TRANSPORT`: `streamable-http`.
- `COHORT_MCP_HOST`: `0.0.0.0`.
- `COHORT_MCP_PORT`: Render's `PORT` value (or leave unset and pass `--port $PORT` in the start command).

Use this service configuration:

| Setting | Value |
| --- | --- |
| Root directory | `integrations/mcp-server` |
| Build command | `pip install -e "../../python[postgres]" -e .` |
| Start command | `cerbere-cohort-mcp --transport streamable-http --host 0.0.0.0 --port $PORT` |

The build command above assumes Render checks out the repository root and then
uses `integrations/mcp-server` as the service root. If Render runs commands
relative to the service root only, use a root-level build script or configure
the service root as the repository root and run the install command from there.

### Connect from Mistral Vibe CLI (PowerShell)

Cohort uses static Bearer authentication; it does not implement OAuth. Do not
add the URL with `/mcp add <url>`, because that flow starts browser-based OAuth.
Instead, provide the credential through Vibe's static-auth environment variable:

```powershell
$secure = Read-Host "Cohort Agent Credential" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $env:COHORT_AGENT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
vibe mcp add cohort --url https://cerbere-cohort.onrender.com/mcp --transport streamable-http --api-key-env COHORT_AGENT_TOKEN
vibe
```

Run these commands in the same PowerShell window. Paste the raw `coh_...`
credential when prompted; do not include the `Bearer ` prefix. Vibe stores the
environment-variable name in `~/.vibe/config.toml` and sends
`Authorization: Bearer <credential>` on MCP HTTP requests. Never put the
credential itself in `config.toml` or commit it to source control.

Each HTTP request must include `Authorization: Bearer coh_...`. The middleware
validates the credential against PostgreSQL on every request, including expiry
and revocation, derives its organization, and binds the authenticated
`organization_id`, `agent_id`, and scopes to that request's execution context.
The optional `X-Organization-ID` header is accepted only when it matches the
organization encoded by the credential. Never put an agent credential in a URL,
query parameter, browser bundle, or public repository.

For local stdio production use, continue setting `COHORT_MCP_AGENT_TOKEN` and
optionally `COHORT_MCP_AGENT_ID`; stdio remains bound to that one credential
for the lifetime of its process.

The server enforces `workspace:read`, `workspace:write`, and
`leases:write` scopes per tool. Each branch write must hold a lease for the
key being changed. `lease_acquire` returns a monotonically increasing
`fencing_token`; same-owner renewal extends expiry without changing the token,
while takeover after expiry receives a higher token. Commits reject expired or
superseded tokens. Event reads are filtered to the authenticated agent.
`workspace_get_state` intentionally exposes the shared committed state within
the configured organization.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `workspace_get_state` | Read committed state and HEAD version |
| `workspace_create_branch` | Create an isolated branch |
| `workspace_read_in_branch` | Read a key from a branch |
| `workspace_set_in_branch` | Set a JSON value in a branch |
| `workspace_delete_in_branch` | Delete a key in a branch |
| `workspace_commit` | Commit a branch with conflict detection; optionally complete its task atomically |
| `task_create` | Create a durable task |
| `task_list` | List pending and assigned tasks visible to the agent |
| `task_get` | Read a visible task |
| `task_events` | Read task lifecycle events |
| `task_claim` | Claim a task and receive its fencing token |
| `task_start` | Start an assigned task |
| `task_heartbeat` | Renew a task lease |
| `task_complete` | Complete a task idempotently |
| `task_fail` | Fail an active task |
| `task_retry` | Requeue a failed task; requires `tasks:admin` scope |
| `lease_acquire` | Acquire a time-limited resource lease |
| `lease_release` | Release a lease owned by an agent |
| `get_events` | Read recent coordination events |

### Recommended agent workflow

1. Use the `agent_id` bound to your credential.
2. Claim a task and keep its fencing token while working.
3. Create a branch for the task and read the keys you intend to change.
4. Acquire resource leases before changing protected keys.
5. Write changes to that branch; do not write directly to HEAD.
6. Commit with `task_id`, `task_fencing_token`, and an `idempotency_key` to publish the branch and complete the task atomically.
7. If the commit reports a conflict, read the latest HEAD and reconcile explicitly. Do not blindly retry with stale values.

Example tool sequence:

```text
task_claim(task_id="...", agent_id="cursor", lease_seconds=120)
task_start(task_id="...", agent_id="cursor", fencing_token=1)
workspace_create_branch(agent_id="cursor", branch_name="auth-refactor")
workspace_read_in_branch(agent_id="cursor", branch_name="auth-refactor", key="architecture")
workspace_set_in_branch(agent_id="cursor", branch_name="auth-refactor", key="architecture", value={"owner": "cursor"})
workspace_commit(
  agent_id="cursor",
  branch_name="auth-refactor",
  task_id="...",
  task_fencing_token=1,
  idempotency_key="auth-refactor-completion"
)
```

## Stdio mode

Stdio starts an independent server process and is suitable for one MCP client.
It does **not** provide shared state between separate client-launched processes.

```bash
cerbere-cohort-mcp --transport stdio
```

For coordination between multiple agents, use one shared Streamable HTTP server.

## Current scope and limitations

- Local SQLite mode is intended for one server process on one machine.
- Production PostgreSQL MCP supports multiple organizations on one service. Each organization is routed to its own PostgreSQL schema and cached independently; idle tenant services are evicted and rehydrated from PostgreSQL.
- Each agent must use its own credential. Authentication, organization routing, agent identity, and scopes are resolved per HTTP request.
- Stdio remains process-bound to one agent credential.
- It is not a multi-region coordination service.
- Local unauthenticated HTTP is for loopback development only.


## Hosted OAuth 2.1 authentication (recommended)

The hosted MCP endpoint supports the MCP authorization discovery flow using
Supabase Auth as its OAuth 2.1 authorization server. Clients that implement
MCP OAuth can connect with only the MCP URL; no Cohort API credential or
provider-specific header is required.

```json
{
  "mcpServers": {
    "cohort": {
      "url": "https://cerbere-cohort.onrender.com/mcp"
    }
  }
}
```

The MCP server publishes RFC 9728 Protected Resource Metadata at
`/.well-known/oauth-protected-resource` and advertises the Supabase Auth
issuer. Supabase handles Authorization Code + PKCE, token issuance and refresh.
Legacy `coh_...` Bearer credentials remain accepted during migration.

### Required Supabase project setup

1. Apply migration `202611250001_mcp_oauth21_authorization.sql`.
2. In Supabase Dashboard, open **Authentication → OAuth Server** and enable
   OAuth 2.1. Set the authorization path to `/oauth/consent`.
3. Enable Dynamic Client Registration for MCP clients that require it. Newer
   clients may prefer Client ID Metadata Documents; Supabase's registration
   endpoint is the compatibility fallback.
4. In **Authentication → Hooks**, enable the **Custom Access Token** hook and
   select `public.cohort_custom_access_token_hook`.
5. Configure the `cohort_mcp_oauth_config.resource_url` value to the exact
   public MCP resource URL. Set the same value in
   `COHORT_MCP_RESOURCE_URL` on the MCP service.
6. Set `COHORT_MCP_RESOURCE_METADATA_URL` to the public URL of the protected
   resource metadata document if the default domain is changed.
7. Use an asymmetric Supabase JWT signing key (ES256 or RS256).

The Cohort dashboard's selected workspace is stored in the authenticated user's
`cohort_mcp_organization_id` metadata. The token hook copies it into a signed
`organization_id` claim only after checking active membership. The MCP server
checks that membership again on every request and derives its internal
`workspace:read/write` and `leases:read/write` permissions from the member's
current role. A token without a valid workspace claim is rejected.

Supabase currently supports the standard OIDC scopes (`openid`, `email`,
`profile`, `phone`) but not custom OAuth scopes. Cohort therefore does not
pretend that its tool permissions are OAuth scopes: they are signed
Cohort-specific claims added by the Custom Access Token Hook and rechecked
against live workspace membership on every MCP request.

**Important:** OAuth is not active in production until the Supabase OAuth
Server and Custom Access Token Hook are enabled in the project dashboard and
the MCP service has the matching resource URL environment variable. Do not
disable authentication to work around a failed discovery or token exchange.
