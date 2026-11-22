-- Organization profile fields and workspace-scoped public logo storage.
BEGIN;

ALTER TABLE public.cohort_organizations
    ADD COLUMN IF NOT EXISTS legal_name TEXT,
    ADD COLUMN IF NOT EXISTS description TEXT,
    ADD COLUMN IF NOT EXISTS website TEXT,
    ADD COLUMN IF NOT EXISTS country TEXT,
    ADD COLUMN IF NOT EXISTS contact_email TEXT,
    ADD COLUMN IF NOT EXISTS logo_url TEXT,
    ADD COLUMN IF NOT EXISTS brand_color TEXT
        CHECK (brand_color IS NULL OR brand_color ~ '^#[0-9A-Fa-f]{6}$'),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Organization marks are intentionally public (they are branding assets).
-- Upload, replacement and deletion remain restricted to workspace owners/admins.
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('cohort-organization-logos', 'cohort-organization-logos', true, 2097152,
        ARRAY['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml'])
ON CONFLICT (id) DO UPDATE SET
    public = true,
    file_size_limit = 2097152,
    allowed_mime_types = ARRAY['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml'];

DROP POLICY IF EXISTS cohort_org_logo_insert ON storage.objects;
CREATE POLICY cohort_org_logo_insert ON storage.objects
    FOR INSERT TO authenticated
    WITH CHECK (
        bucket_id = 'cohort-organization-logos'
        AND (storage.foldername(name))[1] ~* '^[0-9a-f-]{36}$'
        AND EXISTS (
            SELECT 1 FROM public.cohort_memberships m
            WHERE m.organization_id = ((storage.foldername(name))[1])::uuid
              AND m.user_id = auth.uid()
              AND m.status = 'active'
              AND m.role IN ('owner', 'admin')
        )
    );

DROP POLICY IF EXISTS cohort_org_logo_update ON storage.objects;
CREATE POLICY cohort_org_logo_update ON storage.objects
    FOR UPDATE TO authenticated
    USING (
        bucket_id = 'cohort-organization-logos'
        AND EXISTS (
            SELECT 1 FROM public.cohort_memberships m
            WHERE m.organization_id = ((storage.foldername(name))[1])::uuid
              AND m.user_id = auth.uid()
              AND m.status = 'active'
              AND m.role IN ('owner', 'admin')
        )
    )
    WITH CHECK (
        bucket_id = 'cohort-organization-logos'
        AND EXISTS (
            SELECT 1 FROM public.cohort_memberships m
            WHERE m.organization_id = ((storage.foldername(name))[1])::uuid
              AND m.user_id = auth.uid()
              AND m.status = 'active'
              AND m.role IN ('owner', 'admin')
        )
    );

DROP POLICY IF EXISTS cohort_org_logo_delete ON storage.objects;
CREATE POLICY cohort_org_logo_delete ON storage.objects
    FOR DELETE TO authenticated
    USING (
        bucket_id = 'cohort-organization-logos'
        AND EXISTS (
            SELECT 1 FROM public.cohort_memberships m
            WHERE m.organization_id = ((storage.foldername(name))[1])::uuid
              AND m.user_id = auth.uid()
              AND m.status = 'active'
              AND m.role IN ('owner', 'admin')
        )
    );

COMMIT;
