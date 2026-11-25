-- MCP OAuth 2.1 claims for Supabase Auth.
-- Enable this function as the Custom Access Token Hook in Supabase Auth.
BEGIN;

CREATE TABLE IF NOT EXISTS public.cohort_mcp_oauth_config (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    resource_url TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO public.cohort_mcp_oauth_config(singleton, resource_url)
VALUES (TRUE, 'https://cerbere-cohort.onrender.com/mcp')
ON CONFLICT (singleton) DO NOTHING;

ALTER TABLE public.cohort_mcp_oauth_config ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.cohort_mcp_oauth_config FROM anon, authenticated;

CREATE TABLE IF NOT EXISTS public.cohort_mcp_oauth_grants (
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL CHECK (length(client_id) BETWEEN 1 AND 256),
    organization_id UUID NOT NULL REFERENCES public.cohort_organizations(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, client_id)
);
CREATE INDEX IF NOT EXISTS idx_cohort_mcp_oauth_grants_active
    ON public.cohort_mcp_oauth_grants(user_id, client_id)
    WHERE revoked_at IS NULL;
ALTER TABLE public.cohort_mcp_oauth_grants ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.cohort_mcp_oauth_grants FROM anon, authenticated;

INSERT INTO public.cohort_permissions(key, description) VALUES
    ('workspace:write', 'Write shared workspace state and branches'),
    ('leases:write', 'Acquire and release shared resource leases')
ON CONFLICT (key) DO NOTHING;

CREATE OR REPLACE FUNCTION public.cohort_custom_access_token_hook(event JSONB)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    claims JSONB := COALESCE(event->'claims', '{}'::jsonb);
    user_id UUID;
    organization_id UUID;
    membership_role TEXT;
    resource_url TEXT;
    scopes JSONB;
    client_id TEXT;
BEGIN
    -- Only OAuth-issued client tokens are scoped to the MCP resource.
    client_id := NULLIF(claims->>'client_id', '');
    IF client_id IS NULL THEN
        RETURN jsonb_build_object('claims', claims);
    END IF;

    SELECT config.resource_url
      INTO resource_url
      FROM public.cohort_mcp_oauth_config AS config
     WHERE config.singleton = TRUE;

    IF resource_url IS NULL THEN
        RAISE EXCEPTION 'Cohort MCP OAuth resource URL is not configured';
    END IF;

    user_id := NULLIF(event->>'user_id', '')::UUID;

    IF user_id IS NOT NULL THEN
        SELECT
            grant_row.organization_id,
            membership.role,
            (
                membership.role IN ('owner', 'admin', 'member')
                OR (
                    membership.role = 'custom'
                    AND EXISTS (
                        SELECT 1
                          FROM public.cohort_role_permissions AS permission
                         WHERE permission.role_id = membership.custom_role_id
                           AND permission.permission_key = 'workspace:write'
                    )
                )
            ),
            (
                membership.role IN ('owner', 'admin', 'member')
                OR (
                    membership.role = 'custom'
                    AND EXISTS (
                        SELECT 1
                          FROM public.cohort_role_permissions AS permission
                         WHERE permission.role_id = membership.custom_role_id
                           AND permission.permission_key = 'leases:write'
                    )
                )
            )
          INTO organization_id, membership_role, can_workspace_write, can_leases_write
          FROM public.cohort_mcp_oauth_grants AS grant_row
          JOIN public.cohort_memberships AS membership
            ON membership.organization_id = grant_row.organization_id
           AND membership.user_id = grant_row.user_id
           AND membership.status = 'active'
         WHERE grant_row.user_id = user_id
           AND grant_row.client_id = client_id
           AND grant_row.revoked_at IS NULL
         LIMIT 1;

        IF organization_id IS NOT NULL AND membership_role IS NOT NULL THEN
            scopes := jsonb_build_array('workspace:read', 'leases:read');
            IF can_workspace_write THEN
                scopes := scopes || jsonb_build_array('workspace:write');
            END IF;
            IF can_leases_write THEN
                scopes := scopes || jsonb_build_array('leases:write');
            END IF;
            claims := claims || jsonb_build_object(
                'organization_id', organization_id::TEXT,
                'cohort_scopes', scopes
            );
        END IF;
    END IF;

    -- RFC 8707 resource audience: tokens are valid only for the Cohort MCP server.
    claims := jsonb_set(claims, '{aud}', to_jsonb(resource_url), TRUE);
    RETURN jsonb_build_object('claims', claims);
END;
$$;

DO $
BEGIN
    CREATE ROLE supabase_auth_admin NOLOGIN;
EXCEPTION WHEN duplicate_object THEN
    NULL;
END;
$;

REVOKE ALL ON FUNCTION public.cohort_custom_access_token_hook(JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cohort_custom_access_token_hook(JSONB) TO supabase_auth_admin;

COMMIT;
