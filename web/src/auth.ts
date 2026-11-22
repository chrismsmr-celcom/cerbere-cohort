import { createClient } from "@supabase/supabase-js";

const env = (import.meta as any).env || {};
const supabaseUrl = env.VITE_SUPABASE_URL as string | undefined;
const supabaseAnonKey = env.VITE_SUPABASE_ANON_KEY as string | undefined;

export const supabase = supabaseUrl && supabaseAnonKey
  ? createClient(supabaseUrl, supabaseAnonKey, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: true,
      },
    })
  : null;

const ACTIVE_WORKSPACE_KEY = "cohort.active-workspace";

export type CohortWorkspace = { id: string; name: string; role: string; account_type?: string; onboarding_completed?: boolean; logo_url?: string | null };
export function getActiveWorkspaceId(): string {
  try { return localStorage.getItem(ACTIVE_WORKSPACE_KEY) || ""; } catch { return ""; }
}
export function setActiveWorkspaceId(id: string) {
  try { if (id) localStorage.setItem(ACTIVE_WORKSPACE_KEY, id); else localStorage.removeItem(ACTIVE_WORKSPACE_KEY); } catch { /* storage unavailable */ }
}
export const authConfigured = Boolean(supabase);
export const authEnvironmentPresent = Boolean(supabaseUrl || supabaseAnonKey);
