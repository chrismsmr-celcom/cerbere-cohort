# SaaS deployment (early beta)

This guide deploys the hosted REST API and React dashboard. It describes the current beta architecture, not a horizontally scalable production control plane.

## Architecture and constraints

- **Frontend:** Vercel (React/Vite dashboard).
- **API:** one Render web-service instance, one Uvicorn worker.
- **Database:** Supabase PostgreSQL, also used for Supabase Auth membership records.
- **Authentication:** Supabase Auth access tokens verified through the project's asymmetric JWKS keys.
- **Tenant isolation:** each organization is routed to a dedicated PostgreSQL schema.

Multiple organizations can use the same API instance. The tenant runtime, branch registry, and coordination engine caches are process-local, so do not scale the API to multiple Render instances or multiple Uvicorn workers. Cross-process API/MCP writes are not coordinated.

Do not configure an unrelated database for the API: membership checks read `public.cohort_memberships` from the same `DATABASE_URL` used by the runtime.

## 1. Configure Supabase

1. Create a Supabase project and apply `supabase/migrations/202611190001_cohort_organizations.sql`.
2. Configure Supabase Auth to use asymmetric JWT signing keys (ES256 or RS256). The API obtains public keys from `SUPABASE_URL/auth/v1/.well-known/jwks.json`.
3. In Auth → URL Configuration, set the dashboard production origin as Site URL and add the exact production and preview redirect URLs you use.
4. Use the project's PostgreSQL connection string as `DATABASE_URL`. Prefer a direct or session-pooler connection suitable for a long-running API; do not use transaction-pooler mode.

The API expects `public.cohort_memberships` with `organization_id`, `user_id`, `role`, and `status`. Active roles are `owner`, `admin`, and `member`; viewers are read-only. The API does not trust organization or role claims supplied by the browser.

The normal onboarding flow creates the organization and initial owner membership after the user signs in. Additional memberships must be provisioned through trusted admin tooling or the SQL Editor, not through the public browser client.

## 2. Deploy the API on Render

Create a **Web Service** from this repository:

| Setting | Value |
| --- | --- |
| Runtime | Python |
| Root directory | `python` |
| Build command | `pip install ".[postgres]"` |
| Start command | `uvicorn cerbere_cohort.api:app --host 0.0.0.0 --port $PORT --workers 1` |
| Health check path | `/api/health` (liveness) |
| Instances | **One** |
| Uvicorn workers | **One** |

Set these environment variables:

| Variable | Value |
| --- | --- |
| `DATABASE_URL` | Supabase PostgreSQL connection string |
| `SUPABASE_URL` | Supabase project URL |
| `COHORT_CORS_ORIGINS` | Exact dashboard origin(s), comma-separated, no wildcard |
| `COHORT_ENV` | `production` |
| `COHORT_REQUIRE_LEASES` | `true` or `false`, chosen deliberately |
| `COHORT_TELEMETRY_COLLECTOR_ENABLED` | Optional; `true` only if accepting opt-in Community installation pings |

`COHORT_ORGANIZATION_ID` is not required. Organizations are created through onboarding and selected per request after membership verification. Render provides `PORT`; do not hard-code it.

Do not set `COHORT_CORS_ORIGINS=*` in hosted PostgreSQL mode. Keep secrets in Render's environment settings; do not commit them to the repository.

### Free plan behavior

The current Render service may use the Free plan, which suspends after inactivity. A cold start can take longer than a normal API request. The dashboard's default request timeout is 90 seconds to accommodate this; it does not automatically retry timed-out writes because the server may already have applied them. For predictable beta availability, move to an always-on paid plan when budget permits.

## 3. Deploy the dashboard on Vercel

Import the repository and configure the Vite app:

- Root directory: `web`
- Build command: `npm run build`
- Output directory: `dist`

Set build-time environment variables:

| Variable | Value |
| --- | --- |
| `VITE_API_BASE_URL` | Public Render API URL, no trailing slash |
| `VITE_SUPABASE_URL` | Same Supabase project URL used by the API |
| `VITE_SUPABASE_ANON_KEY` | Supabase publishable/anon key only |
| `VITE_API_TIMEOUT_MS` | Optional; default 90000, maximum 180000 |

Vite embeds `VITE_*` values in public browser assets. Never use a service-role key, PostgreSQL password, or any other server secret in a `VITE_*` variable.

## 4. Verify the deployment

1. Open `https://YOUR-API/api/health`. Confirm `persistence=postgresql`, `authentication=supabase`, `configuration.supabase_url_configured=true`, `configuration.cors_origins_configured=true`, and `single_instance_required=true`.
2. Open `https://YOUR-API/api/ready`. It must return HTTP 200 with `database`, `membership_table`, `supabase_url`, `supabase_jwks`, and `cors_origins` checks all true.
3. Open `https://YOUR-API/docs` and inspect the actual OpenAPI schemas.
4. Sign in through the dashboard. Confirm a non-member receives 403 and an active member can read workspace state.
5. Create two branches, make conflicting writes, and confirm the second commit reports a conflict instead of silently overwriting the first.
6. Restart the API and confirm persisted workspace state and open branches are restored.
7. Confirm Render is running exactly one instance and the deployment logs show no database/JWKS or membership errors.

The readiness endpoint verifies database connectivity, the membership table, and the configured Supabase JWKS signing keys. It does not validate a real user's JWT or membership; the authenticated end-to-end test is still required.

## 5. Current operational limits

- One API process/instance only. Do not horizontally scale until the tenant runtime and branch registry are moved behind a shared coordination boundary.
- Multiple organizations are schema-isolated in PostgreSQL, but process-local caches still require a single API instance.
- MCP has a separate branch registry and is not a remote authenticated multi-user service. Do not run MCP and REST writers concurrently against the same workspace.
- Agent credentials are bound to an agent identity and events distinguish the authenticated actor from the target agent. This is not external-tool authorization or sandboxing.
- Free Render hosting can sleep when idle.

Treat this as an early-beta deployment, not an enterprise-ready multi-instance coordination service.
