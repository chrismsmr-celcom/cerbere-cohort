# Cohort closed beta tester guide

Cohort is accepting a small number of technical testers. This is a controlled beta, not a production-critical coordination service.

## What to test

The most useful test is two or more agents working from the same workspace:

1. Sign in to the hosted dashboard and select your workspace.
2. Create separate agent credentials for each agent. Keep the credentials private; never paste them into an issue or event payload.
3. Connect through the REST API. Use the workspace ID in `X-Organization-ID` and the agent credential as a Bearer token.
4. Create two branches from the same workspace head before either branch is committed.
5. Agent A acquires a lease for the shared key, writes its value, commits, and releases the lease.
6. Agent B acquires the same key's lease and writes a different value through its older branch.
7. Agent B commits. Cohort should report a conflict, not silently overwrite Agent A's value.
8. Inspect the conflict, resolve it explicitly, and verify the resulting value in Shared state and the Event log.
9. Open the Coordination page and confirm its agent, lease, branch, and event information matches the workspace.

The exact request schemas are available from the API's `/docs` endpoint. Do not infer payload fields from examples in old posts.

## What is in scope

- Signup, login, onboarding, workspace selection, and membership boundaries.
- Agent credential creation, scope enforcement, and revocation.
- Lease acquisition, expiry, handoff, and stale fencing-token rejection.
- Branch creation, writes, commits, conflict inspection, and resolution.
- Event history, dashboard consistency, and recovery after an API restart.
- Isolation between two test organizations.

## Beta limitations

- One hosted API instance and one Uvicorn worker. Do not load-test horizontal scaling.
- Render Free may sleep when idle; the first request can be slow.
- Remote authenticated MCP is not available. Use the REST API for hosted multi-agent tests. Do not run REST and local MCP writers concurrently against the same workspace.
- Cohort coordinates shared state; it does not sandbox code or authorize external tools.
- Do not use Cohort as the sole protection for production workloads or irreversible actions.

## Data and safety rules

Use synthetic test records only. Do not submit passwords, API keys, agent credentials, personal information, customer data, confidential business information, or production records. Do not point test agents at production tools or accounts.

If you find a security vulnerability, follow [SECURITY.md](../SECURITY.md) rather than opening a public issue.

## Report a problem

Open a [Beta tester feedback issue](https://github.com/chrismsmr-celcom/cerbere-cohort/issues/new?template=beta-feedback.yml) and include:

- What you were trying to do.
- Number of agents and framework/runtime.
- The sequence of operations and approximate time.
- Expected behavior versus actual behavior.
- HTTP status and sanitized error message.
- Whether the issue is reproducible.

Remove workspace IDs, emails, tokens, URLs containing secrets, and all sensitive values before submitting logs. A minimal reproducible example with synthetic values is more useful than a full production trace.

## Optional: export events to your observability platform

Workspace owners and admins can connect one OTLP/HTTP logs destination from **Setting → External observability**.

1. In Datadog, create or use an API key and select the OTLP logs intake endpoint for your Datadog site. A common US1 endpoint is `https://otlp.datadoghq.com/v1/logs`; use the matching endpoint for your region.
2. In Cohort, paste the endpoint and set the authentication header name to `dd-api-key`; paste the API key as its value. For another OTLP backend, use the authentication header required by that backend (for example, `Authorization` with a Bearer token).
3. Save the destination and select **Send test log**. Confirm that `cohort.export_test` appears in your backend.
4. Run the two-agent coordination test. New Cohort events should arrive asynchronously in the same logs stream.

Cohort exports OTLP **logs** in this beta, not distributed traces or metrics. The exporter includes event IDs, action/agent metadata and selected coordination metadata. It deliberately excludes shared-state values, prompts, full change payloads and credentials. Credentials are encrypted at rest by the Cohort API. Delivery retries after endpoint failures; the export is at-least-once, so consumers should deduplicate using `cohort.event_id`.

Do not paste API keys into feedback issues, screenshots, or event payloads. Remove the destination from Setting when you finish testing.
