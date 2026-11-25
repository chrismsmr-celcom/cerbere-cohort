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
    BEGIN
        organization_id := NULLIF(claims->'user_metadata'->>'cohort_mcp_organization_id', '')::UUID;
    EXCEPTION WHEN invalid_text_representation THEN
        organization_id := NULL;
    END;

    IF user_id IS NOT NULL AND organization_id IS NOT NULL THEN
        SELECT membership.role
          INTO membership_role
          FROM public.cohort_memberships AS membership
         WHERE membership.user_id = user_id
           AND membership.organization_id = organization_id
           AND membership.status = 'active'
         LIMIT 1;

        IF membership_role IS NOT NULL THEN
            scopes := jsonb_build_array('workspace:read', 'leases:read');
            IF membership_role IN ('owner', 'admin', 'member') THEN
                scopes := scopes || jsonb_build_array('workspace:write', 'leases:write');
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

REVOKE ALL ON FUNCTION public.cohort_custom_access_token_hook(JSONB) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.cohort_custom_access_token_hook(JSONB) TO supabase_auth_admin;

COMMIT;
