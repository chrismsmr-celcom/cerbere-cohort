# Cohort closed beta tester guide

Cohort is accepting a small number of technical testers. This is a controlled beta, not a production-critical coordination service.

## 1. Create your account and workspace

1. Open the Cohort dashboard and sign in with email magic link, Google, GitHub, or GitLab. If you received a workspace invitation, open its link first and use the exact invited email address.
2. If you are creating a new workspace, complete the onboarding form. If you accepted an invitation, your existing workspace should appear automatically; do not create a second workspace.
3. Open **Gateway → Agent credentials**.
4. Create one credential for each agent. Use a distinct agent ID for every worker and a short expiry (7 or 30 days for a test).
5. Copy each credential immediately. It is shown only once. Cohort stores its hash, not the original token.

Do not share a credential between agents. Revoke it from Gateway when the test ends.

## 2. Invite a teammate (workspace owners and admins)

1. Open **Setting → Invite a teammate**.
2. Enter the exact email address the tester will use to sign in.
3. Choose a role and expiry (7, 14, or 30 days).
4. Create the invitation and copy the link immediately.
5. Send the link through your own email or messaging channel. Cohort does not send invitation emails automatically in this beta.
6. The invitee opens the link, signs in with the invited email address, and Cohort adds the account to the workspace.
7. Review the member and invitation status in **Setting**. Revoke a pending link if it was sent to the wrong person or exposed.

Invitation tokens are displayed only once and stored as hashes. The invitation is single-use, expires automatically, and is bound to the invited email address. The invited user must have a confirmed email address in Supabase.

## 3. Choose one connection transport

### Hosted MCP

Use the remote Streamable HTTP endpoint:

```text
https://mcp.cohort.cerbereag.site/mcp
```

Configure your MCP client with the credential in the HTTP Authorization header:

```text
Authorization: Bearer <agent-credential>
```

Your MCP client must support Streamable HTTP and custom request headers.

### Hosted REST API

Use:

- API base: `https://api.cohort.cerbereag.site`
- Workspace header: `X-Organization-ID: <workspace-id>`
- Auth header: `Authorization: Bearer <agent-credential>`

The agent ID in each request must match the identity bound to that credential. The API schemas are available at `https://api.cohort.cerbereag.site/docs`.

**Important:** the REST API and MCP service currently maintain separate branch registries. Do not write to the same workspace through both transports at the same time. Choose one transport for the main test.

## 4. Run the coordination test

The recommended REST API test is:

1. Create two distinct agent credentials, Agent A and Agent B.
2. Create two branches from the same workspace head before either branch is committed.
3. Agent A acquires a lease for a shared key, writes a value, commits, then releases the lease.
4. Agent B acquires a lease for that same key and writes a different value through its older branch.
5. Agent B attempts to commit. Cohort should report a conflict rather than silently overwrite Agent A's value.
6. Inspect the conflict, resolve it explicitly, and verify the final value in Shared state and the Event log.
7. Open Coordination and confirm its agent, lease, branch, and event information matches the workspace.

Also test lease expiry, revocation of an agent credential, and access from a second organization if you have a second test account.

## 5. What to test

- Signup, login, onboarding, workspace selection, and membership boundaries.
- Agent credential creation, scope enforcement, expiry, and revocation.
- Lease acquisition, expiry, handoff, and stale fencing-token rejection.
- Branch creation, writes, commits, conflict inspection, and resolution.
- Event history, dashboard consistency, and recovery after an API restart.
- Isolation between two test organizations.
- Remote MCP authentication and tool behavior, if using MCP.

## 6. Beta limitations

- The hosted REST API must run with one instance and one Uvicorn worker.
- Owners and admins can create and revoke invitations; invitation email delivery is manual in this beta.
- The Setting page exposes `/api/ready` checks, but a green dashboard connection indicator alone does not prove every readiness check has passed.
- Render Free may sleep when idle; the first request after inactivity can be slow.
- The REST API and MCP service have separate branch registries. Do not mix writes across them for the same workspace.
- Cohort coordinates shared state; it does not sandbox code or authorize external tools.
- Do not use Cohort as the sole protection for production workloads or irreversible actions.
- The Performance page currently reports browser-measured request latency; do not treat it as a complete server-side monitoring system.

## 7. Data and safety rules

Use synthetic test records only. Do not submit passwords, API keys, agent credentials, personal information, customer data, confidential business information, or production records. Do not point test agents at production tools or accounts.

If you find a security vulnerability, follow [SECURITY.md](../SECURITY.md) rather than opening a public issue.

## 8. Report a problem

Open a [Beta tester feedback issue](https://github.com/chrismsmr-celcom/cerbere-cohort/issues/new?template=beta-feedback.yml) and include:

- What you were trying to do.
- Number of agents and framework/runtime.
- The sequence of operations and approximate time.
- Expected behavior versus actual behavior.
- HTTP status and sanitized error message.
- Whether the issue is reproducible.

Remove workspace IDs, emails, tokens, URLs containing secrets, and all sensitive values before submitting logs. A minimal reproducible example with synthetic values is more useful than a full production trace.

## 9. Optional: export events to your observability platform

Workspace owners and admins can connect one OTLP/HTTP logs destination from **Setting → External observability**.

1. Configure the OTLP logs endpoint for your provider and region.
2. Set the authentication header name and value required by that provider.
3. Save the destination and select **Send test log**.
4. Run the two-agent coordination test. New Cohort events should arrive asynchronously in the same logs stream.

Cohort exports OTLP logs in this beta, not distributed traces or metrics. The exporter omits shared-state values, prompts, full change payloads, and credentials. Delivery is at-least-once; consumers should deduplicate using `cohort.event_id`.

Do not paste API keys into feedback issues, screenshots, or event payloads. Remove the destination from Setting when you finish testing.
