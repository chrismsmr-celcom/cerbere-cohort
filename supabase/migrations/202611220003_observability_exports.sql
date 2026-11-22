-- Workspace-scoped OTLP/HTTP export destinations.
-- Credentials are encrypted by the API before they reach this table.
BEGIN;

CREATE TABLE IF NOT EXISTS public.cohort_observability_exports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL UNIQUE
        REFERENCES public.cohort_organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('otlp_http')),
    endpoint TEXT NOT NULL,
    encrypted_headers BYTEA NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    last_event_id TEXT,
    last_success_at TIMESTAMPTZ,
    last_error TEXT,
    created_by UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cohort_observability_exports_enabled
    ON public.cohort_observability_exports(enabled) WHERE enabled = true;

ALTER TABLE public.cohort_observability_exports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.cohort_observability_exports FROM anon, authenticated;

COMMIT;
