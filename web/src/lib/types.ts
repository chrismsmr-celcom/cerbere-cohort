export type Tab = "overview" | "branches" | "state" | "leases" | "events" | "gateway" | "performance" | "advisor" | "coordination" | "settings";

/* ---- Contracts of endpoints that exist today (python/src/cerbere_cohort/api.py) ---- */
export type EventItem = { event_id: string; timestamp: string; agent_id: string; action: string; payload: Record<string, unknown>; schema_version: string };
export type Branch = { id: string; name: string; agent_id: string; base_version: string; status: string; conflicting_keys: string[]; created_at: string };
export type StateItem = { key: string; value_hash: string; value: unknown };
export type StateSnapshot = { head: string; parent: string | null; items: StateItem[] };
export type Lease = { key: string; agent_id: string; expires_at: number; acquired_at: number; fencing_token: number };
export type Overview = { head: string; key_count: number; snapshot_count: number; event_count: number; active_lease_count: number; commit_count: number; conflict_count: number; branches: number; sample_data: boolean };
export type Settings = { require_leases: boolean; persistence: string; database_path: string; multi_process: boolean; authentication: string; database_transactions?: boolean };
export type Health = { ok: boolean; service: string; runtime: string; persistence: string; branch_registry: string; authentication: string };
export type ConflictDetail = { key: string; base: unknown; head: unknown; branch: unknown; base_present: boolean; head_present: boolean; branch_present: boolean };
export type DiffChange = { key: string; base: unknown; proposed: unknown; head: unknown; base_present: boolean; proposed_present: boolean; head_present: boolean; head_changed_since_base: boolean; intent: string | null };
export type BranchDiff = { branch: Branch; base_version: string; head_version: string; change_count: number; changes: DiffChange[] };

/* ---- Contracts of endpoints NOT implemented yet. The UI handles 404 gracefully. ---- */
// GET /api/gateway/agents · POST /api/gateway/agents · DELETE /api/gateway/agents/{id}
export type GatewayAgent = { id: string; name: string; kind: string; status: "connected" | "idle" | "error" | "disconnected"; last_seen: string | null; endpoint?: string | null; version?: string | null };
// GET /api/performance
export type PerformanceReport = {
  window_seconds: number;
  api: { p50_ms: number; p95_ms: number; p99_ms: number; error_rate: number; requests_per_min: number };
  series: { t: string; p50_ms: number; p95_ms: number; rps: number; errors: number }[];
  endpoints: { path: string; p50_ms: number; p95_ms: number; error_rate: number; count: number }[];
  agents: { agent_id: string; latency_ms: number; status: "ok" | "slow" | "down" }[];
  incidents: { id: string; title: string; severity: "info" | "warning" | "critical"; started_at: string; resolved_at: string | null }[];
};
// GET /api/advisor
export type AdvisorItem = { id: string; severity: "info" | "warning" | "critical"; title: string; detail: string; action?: { label: string; tab?: Tab; href?: string }; source: "local" | "server" };
// GET /api/cerbere/summary?window=24h|7d|30d
export type CerbereDecision = { id: string; timestamp: string; agent_id: string; decision: "blocked" | "flagged" | "allowed"; rule: string; target: string; reason: string };
export type CerbereSummary = { window: string; totals: { blocked: number; flagged: number; allowed: number }; by_rule: { rule: string; count: number }[]; items: CerbereDecision[] };
// GET /api/billing/plans · POST /api/billing/checkout
export type Plan = { id: "pro" | "team" | "enterprise" | string; name: string; description: string; price?: string | null; period?: string | null; features?: string[]; cta?: string };
export type PlansResponse = { current?: string | null; plans: Plan[] };

export type Data = {
  overview: Overview | null; branches: Branch[]; state: StateSnapshot; events: EventItem[]; leases: Lease[];
  settings: Settings | null; health: Health | null; connected: boolean; loaded: boolean; error: string; lastUpdated: number | null;
};
