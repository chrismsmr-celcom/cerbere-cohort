# Supabase authentication and workspace bootstrap

## Production API environment (Render)

Set these variables in the Cohort API service:

- `DATABASE_URL`: Supabase PostgreSQL connection string. Use a direct or session-pooler connection suitable for a long-running API; do not use transaction-pooler mode.
- `SUPABASE_URL`: Supabase project URL, for example `https://<project-ref>.supabase.co`.
- `COHORT_CORS_ORIGINS`: exact dashboard origin(s), comma-separated, no wildcard.
- `COHORT_ENV=production`.
- `COHORT_REQUIRE_LEASES`: choose `true` or `false` deliberately.

The API refuses to start on hosted deployments without `DATABASE_URL`. When PostgreSQL is configured, it also requires `SUPABASE_URL` and explicit CORS origins. Supabase access tokens are verified with asymmetric ES256/RS256 keys fetched from the project's JWKS endpoint. Membership is checked against `public.cohort_memberships`; the browser cannot supply its own role or trusted organization claim.

## Apply the database migration

Run `supabase/migrations/202611190001_cohort_organizations.sql` in the Supabase SQL Editor.

The normal onboarding flow creates the first organization and owner membership after the user signs in. For manual bootstrap, create an organization:

```sql
insert into public.cohort_organizations (name)
values ('Your organization')
returning id;
```

Then create the owner's membership using the UUID returned above and the user's UUID from Supabase Dashboard → Authentication → Users:

```sql
insert into public.cohort_memberships (organization_id, user_id, role, status)
values ('<ORGANIZATION_UUID>', '<AUTH_USER_UUID>', 'owner', 'active');
```

Provision additional memberships through trusted admin tooling or the SQL Editor, not through the public browser client.

## Frontend environment (Vercel)

Set build-time variables:

- `VITE_API_BASE_URL`: Render API base URL, with no trailing slash.
- `VITE_SUPABASE_URL`: same Supabase project URL.
- `VITE_SUPABASE_ANON_KEY`: Supabase publishable/anon key only.
- `VITE_API_TIMEOUT_MS`: optional request timeout in milliseconds; defaults to 90000 and is capped at 180000.

Never put the Supabase service-role key or PostgreSQL password in a `VITE_*` variable or browser code.

The dashboard supports email magic links and Google sign-in. Enable Google as a provider in Supabase Auth and configure its OAuth credentials and redirect URLs in the Supabase dashboard. Add the Vercel production URL and any preview URLs you actually use to Supabase Auth's allowed redirect URLs.

## Tenant and runtime boundary

Multiple organizations can use the same API deployment. Each organization is routed to a separate PostgreSQL schema, and membership is checked for the requested organization on protected API routes.

The current runtime and branch registry are process-local. Run exactly one API instance and one Uvicorn worker. Do not horizontally scale until branch/runtime state and coordination locks are moved behind a shared process-independent boundary.

Remote MCP over HTTP is deliberately blocked when `DATABASE_URL` is configured because authenticated MCP transport has not been implemented. The MCP server has a separate branch registry and its in-memory engine cache is not coordinated with the REST API. Do not run concurrent MCP and REST writes against the same workspace.
