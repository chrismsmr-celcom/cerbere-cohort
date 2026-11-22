# Beta launch checklist

Run this checklist once for each production environment and again after changing Supabase, Render, or Vercel configuration.

## Supabase

- [ ] Apply `supabase/migrations/202611190001_cohort_organizations.sql` in the SQL Editor.
- [ ] Configure Supabase Auth to use asymmetric JWT signing keys (ES256 or RS256). The API verifies access tokens through the project's JWKS endpoint; legacy HS256 tokens are not accepted.
- [ ] Configure Auth → URL Configuration: set the production dashboard origin as the Site URL and add the exact production and preview redirect URLs you use.
- [ ] Use a direct or session-pooler PostgreSQL connection for `DATABASE_URL`. Do not use transaction-pooler mode for this long-lived runtime.
- [ ] Complete onboarding with an Auth user. Onboarding creates the organization and its initial owner membership. If provisioning manually, use trusted SQL/admin tooling; never accept membership or role claims from the browser.

## API (Render)

- [ ] Set `DATABASE_URL`, `SUPABASE_URL`, `COHORT_CORS_ORIGINS` (exact dashboard origin(s), comma-separated, no `*`), and `COHORT_ENV=production`.
- [ ] Set `COHORT_REQUIRE_LEASES` deliberately. The current Blueprint default is `false`; enable it only after confirming every writer acquires the required lease.
- [ ] Keep exactly **one API instance**. Tenant schemas are isolated in PostgreSQL, but the runtime and branch registry are process-local. Do not enable multiple instances or multiple Uvicorn workers.
- [ ] Free Render instances sleep when idle. The dashboard allows a 90-second API response window for cold starts; use a paid always-on plan if beta users need predictable availability.
- [ ] `GET /api/health` reports safe configuration flags and the single-instance constraint.
- [ ] `GET /api/ready` returns 200 only when the database is reachable and required hosted configuration is present.
- [ ] Sign in with an active member and exercise a protected endpoint. A health response alone does not prove that JWT verification and membership lookup work.

## Dashboard (Vercel)

- [ ] Set build-time variables: `VITE_API_BASE_URL`, `VITE_SUPABASE_URL`, and `VITE_SUPABASE_ANON_KEY`.
- [ ] Ensure the API URL has no trailing slash and the Supabase URL points to the same project as `DATABASE_URL`.
- [ ] Never put a Supabase service-role key, database password, or other server secret in a `VITE_*` variable.
- [ ] Optional: set `VITE_API_TIMEOUT_MS` (1,000–180,000 ms). Default: 90,000 ms. The client does not automatically retry timed-out mutations because the server may already have processed them.
- [ ] Sign in with a non-member account (expect 403), then with an active member.
- [ ] Walk the golden path: create branch → write a key → commit → inspect the event; then create conflicting branches and resolve the conflict.

## Known beta limits

- Multiple organizations use separate PostgreSQL schemas, but the API must remain a single instance.
- Remote authenticated MCP is not available. Do not run API and MCP writers concurrently against the same workspace; their in-memory engine caches are not coordinated.
- Agent credentials are bound to their own `agent_id`. Human workspace members can intervene on agent resources, and events record the authenticated human as the actor separately from the target agent.
- Membership changes may take up to `COHORT_MEMBERSHIP_CACHE_SECONDS` (default 30 seconds) to take effect.
- Free Render hosting can sleep when idle; the first request may be slow.
