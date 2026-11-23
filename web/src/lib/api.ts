import { getActiveWorkspaceId, supabase } from "../auth";

const env = ((import.meta as any).env || {}) as Record<string, string | undefined>;
export const API_BASE: string = env.VITE_API_BASE_URL || env.VITE_API_URL || "";
export const REPO_URL = "https://github.com/chrismsmr-celcom/cerbere-cohort";
export const DOCS_URL: string = env.VITE_DOCS_URL || `${REPO_URL}/tree/main/docs`;
export const FEEDBACK_URL: string = env.VITE_FEEDBACK_URL || "";
export const PLANS_URL: string = env.VITE_PLANS_URL || "";
const configuredTimeout = Number(env.VITE_API_TIMEOUT_MS);
export const API_TIMEOUT_MS = Number.isFinite(configuredTimeout) && configuredTimeout >= 1000
  ? Math.min(configuredTimeout, 180000)
  : 90000;

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}
export const isUnavailable = (error: unknown) => error instanceof ApiError && [404, 405, 501].includes(error.status);

/* Every request is timed so the Performance page can show real, client-measured latency. */
export type Sample = { path: string; ms: number; ok: boolean; at: number };
const samples: Sample[] = [];
const listeners = new Set<() => void>();
export const getSamples = () => samples;
export const subscribeSamples = (fn: () => void) => { listeners.add(fn); return () => { listeners.delete(fn); }; };
const normalize = (path: string) => path.split("?")[0]
  .replace(/\/branches\/[^/]+/, "/branches/:id")
  .replace(/\/leases\/.+/, "/leases/:key")
  .replace(/\/gateway\/agents\/[^/]+/, "/gateway/agents/:id");
function record(sample: Sample) {
  samples.push(sample);
  if (samples.length > 600) samples.splice(0, samples.length - 600);
  listeners.forEach((fn) => fn());
}

const WORKSPACE_OPTIONAL_PATHS = new Set([
  "/workspaces",
  "/onboarding",
  // Invite acceptance must work before the invitee belongs to a workspace.
  "/organizations/invitations/accept",
]);

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const workspaceOptional = WORKSPACE_OPTIONAL_PATHS.has(path);
  const activeWorkspaceId = getActiveWorkspaceId();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(!workspaceOptional && activeWorkspaceId ? { "X-Organization-ID": activeWorkspaceId } : {}),
    ...((init?.headers || {}) as Record<string, string>),
  };
  if (supabase) {
    const { data } = await supabase.auth.getSession();
    if (data.session?.access_token) headers.Authorization = `Bearer ${data.session.access_token}`;
  }
  if (!workspaceOptional && !activeWorkspaceId && supabase) {
    throw new ApiError("Select a workspace before using the Cohort runtime.", 400);
  }
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), API_TIMEOUT_MS);
  const started = performance.now();
  let response: Response;
  let body: any;
  try {
    response = await fetch(`${API_BASE}/api${path}`, { ...init, headers, signal: controller.signal });
    // Keep the timeout active until the response body has been consumed.
    body = await response.json().catch(() => ({}));
  } catch (err) {
    record({ path: normalize(path), ms: performance.now() - started, ok: false, at: Date.now() });
    throw new ApiError(err instanceof DOMException && err.name === "AbortError"
      ? `The Cohort API did not respond within ${Math.round(API_TIMEOUT_MS / 1000)} seconds. The request may have reached the server; check the result before retrying.`
      : "Unable to reach the Cohort API.", 0);
  } finally {
    window.clearTimeout(timer);
  }
  record({ path: normalize(path), ms: performance.now() - started, ok: response.ok || response.status === 404, at: Date.now() });
  if (response.status === 401 && supabase) {
    await supabase.auth.signOut();
    throw new ApiError("Your session has expired. Please sign in again.", 401);
  }
  if (!response.ok) {
    const detail = body?.detail;
    throw new ApiError(typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail || body), response.status);
  }
  return body as T;
}
