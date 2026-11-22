# Beta launch checklist

Run this checklist once for each production environment and again after changing Supabase, Render, or Vercel configuration.

## Supabase

- [ ] Apply every SQL migration in `supabase/migrations/` in filename order, from `202611190001` through `202611220002`. Do not stop after the organization bootstrap migration.
- [ ] Confirm `/api/ready` reports `application_schema=true` and `onboarding_trigger=true`; a successful database ping alone is insufficient.
- [ ] Configure Supabase Auth to use asymmetric JWT signing keys (ES256 or RS256). The API verifies access tokens through the project's JWKS endpoint; legacy HS256 tokens are not accepted.
- [ ] Configure Auth → URL Configuration: set the production dashboard origin as the Site URL and add the exact production and preview redirect URLs you use.
- [ ] Use a direct or session-pooler PostgreSQL connection for `DATABASE_URL`. Do not use transaction-pooler mode for this long-lived runtime.
- [ ] Complete onboarding with an Auth user. Onboarding creates the organization and its initial owner membership. If provisioning manually, use trusted SQL/admin tooling; never accept membership or role claims from the browser.

## API (Render)

- [ ] Set `DATABASE_URL`, `SUPABASE_URL`, `COHORT_CORS_ORIGINS` (exact dashboard origin(s), comma-separated, no `*`), and `COHORT_ENV=production`.
- [ ] Keep `COHORT_REQUIRE_LEASES=true` for hosted beta. Every agent must acquire a lease for each key before writing it. Do not disable lease enforcement to make an integration pass.
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
- [ ] Walk the full golden path with two agent identities: create branch → acquire lease → write → commit; then create competing branches, confirm the second commit reports a conflict, inspect it, resolve it, and verify the final shared state.
- [ ] Open the Coordination, Branches, Leases, Shared state, and Event log pages with real workspace data. Confirm counts and ownership match the API; confirm empty states work in a fresh workspace.
- [ ] Use only synthetic test data. Do not enter secrets, credentials, personal data, customer data, or production workloads.

## Known beta limits

- Multiple organizations use separate PostgreSQL schemas, but the API must remain a single instance.
- The hosted beta enforces leases. A branch write without a current lease for the target key must be rejected.
- Remote authenticated MCP is not available. Do not run API and MCP writers concurrently against the same workspace; their in-memory engine caches are not coordinated.
- Agent credentials are bound to their own `agent_id`. Human workspace members can intervene on agent resources, and events record the authenticated human as the actor separately from the target agent.
- Membership changes may take up to `COHORT_MEMBERSHIP_CACHE_SECONDS` (default 30 seconds) to take effect.
- Free Render hosting can sleep when idle; the first request may be slow.
