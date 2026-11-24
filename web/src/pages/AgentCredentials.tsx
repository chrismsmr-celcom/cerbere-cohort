import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Copy, KeyRound, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { api } from "../lib/api";
import { Card, Empty, TableSkeleton, Tag, useToast } from "../components/ui";

type Credential = {
  id: string;
  agent_id: string;
  name: string;
  scopes: string[];
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
};
type CreatedCredential = Credential & { token: string };

const SCOPE_OPTIONS = [
  { id: "workspace:read", label: "Read shared workspace", description: "Read branches and shared state." },
  { id: "workspace:write", label: "Write shared workspace", description: "Create branches and commit workspace changes." },
  { id: "leases:read", label: "Read leases", description: "Inspect task and branch ownership." },
  { id: "leases:write", label: "Manage leases", description: "Acquire, renew and release leases." },
] as const;

function credentialStatus(item: Credential): "Active" | "Expired" | "Revoked" {
  if (item.revoked_at) return "Revoked";
  if (item.expires_at && new Date(item.expires_at).getTime() <= Date.now()) return "Expired";
  return "Active";
}

function dateLabel(value: string | null) {
  return value ? new Date(value).toLocaleString() : "Never";
}

export default function AgentCredentials() {
  const toast = useToast();
  const [items, setItems] = useState<Credential[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(SCOPE_OPTIONS.map((scope) => scope.id));
  const [expiryDate, setExpiryDate] = useState("");
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState("");
  const [created, setCreated] = useState<CreatedCredential | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const result = await api<{ items: Credential[] }>("/agents/credentials");
      setItems(result.items || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load agent credentials.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const toggleScope = (scope: string) => {
    setScopes((current) => current.includes(scope)
      ? current.filter((value) => value !== scope)
      : [...current, scope]);
  };

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!name.trim() || scopes.length === 0) return;
    setCreating(true);
    setError("");
    setCreated(null);
    try {
      // The runtime requires a stable agent_id as well as a display name.
      // Generate a unique ID from the name so customers do not need to invent one.
      const slug = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "agent";
      const agentId = `${slug}-${crypto.randomUUID().slice(0, 8)}`;
      const expiresInDays = expiryDate
        ? Math.max(1, Math.ceil((new Date(`${expiryDate}T23:59:59.999Z`).getTime() - Date.now()) / 86400000))
        : null;
      const result = await api<CreatedCredential>("/agents/credentials", {
        method: "POST",
        body: JSON.stringify({
          agent_id: agentId,
          name: name.trim(),
          scopes,
          ...(expiresInDays ? { expires_in_days: Math.min(365, expiresInDays) } : {}),
        }),
      });
      setCreated(result);
      setName("");
      setExpiryDate("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to create agent credential.");
    } finally {
      setCreating(false);
    }
  };

  const copyToken = async () => {
    if (!created?.token) return;
    try {
      await navigator.clipboard.writeText(created.token);
      toast("Credential copied");
    } catch {
      toast("Clipboard access is unavailable. Select and copy the token manually.", "err");
    }
  };

  const revoke = async (item: Credential) => {
    if (credentialStatus(item) !== "Active") return;
    if (!window.confirm(`Revoke access for "${item.name}"? This agent will no longer be able to authenticate.`)) return;
    setRevoking(item.id);
    setError("");
    try {
      await api(`/agents/credentials/${encodeURIComponent(item.id)}`, { method: "DELETE" });
      toast("Credential revoked");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to revoke credential.");
    } finally {
      setRevoking("");
    }
  };

  return <Card id="settings-agent-credentials" className="c12" title="Agent credentials" description="Create and manage credentials that let external agents connect to this Cohort workspace." actions={
    <button className="btn btn-secondary" type="button" onClick={() => void load()} disabled={loading}><RefreshCw size={14} /> Refresh</button>
  }>
    <div style={{ padding: "0 24px 20px" }}>
      <div className="notice" style={{ marginBottom: 18 }}>
        <strong>Workspace-scoped access.</strong> Each credential belongs to this workspace and has its own permissions. The full token is shown only once when created; Cohort stores only its hash.
      </div>

      <form className="stack" onSubmit={create} style={{ gap: 16 }}>
        <div className="grid">
          <label className="field c6">Agent name
            <input value={name} onChange={(event) => setName(event.target.value)} required maxLength={120} placeholder="e.g. Mistral Vibe" />
          </label>
          <label className="field c6">Expiration date <span className="muted">(optional)</span>
            <input type="date" value={expiryDate} min={new Date().toISOString().slice(0, 10)} onChange={(event) => setExpiryDate(event.target.value)} />
            <small>Leave empty for a credential that does not expire. Maximum 365 days.</small>
          </label>
        </div>

        <div className="stack" style={{ gap: 10 }}>
          <div><strong style={{ fontSize: 13 }}>Permissions</strong><p className="muted" style={{ marginTop: 3 }}>Grant only the access this agent needs.</p></div>
          <div className="grid">
            {SCOPE_OPTIONS.map((scope) => <label key={scope.id} className="field c6" style={{ display: "flex", flexDirection: "row", alignItems: "flex-start", gap: 10, padding: 12, border: "1px solid var(--line)", borderRadius: 10 }}>
              <input type="checkbox" checked={scopes.includes(scope.id)} onChange={() => toggleScope(scope.id)} style={{ width: 16, minWidth: 16, marginTop: 2 }} />
              <span><strong>{scope.label}</strong><small style={{ display: "block", color: "var(--muted)", marginTop: 3 }}>{scope.description}</small><code style={{ display: "block", marginTop: 5 }}>{scope.id}</code></span>
            </label>)}
          </div>
        </div>

        <div className="row wrap" style={{ justifyContent: "flex-start" }}>
          <button className="btn btn-primary" type="submit" disabled={creating || !name.trim() || scopes.length === 0}>
            <KeyRound size={14} /> {creating ? "Creating credential…" : "Create credential"}
          </button>
          {scopes.length === 0 && <small className="muted">Select at least one permission.</small>}
        </div>
      </form>

      {created && <section className="notice success" aria-live="polite" style={{ marginTop: 20 }}>
        <div className="row wrap" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <strong>Credential created. Copy this token now.</strong>
          <Tag tone="warn">Shown once</Tag>
        </div>
        <p className="muted">The token will not be available again after you close this panel. Store it in your agent's secret manager or environment variables.</p>
        <div className="row wrap" style={{ alignItems: "stretch" }}>
          <code style={{ flex: "1 1 320px", minWidth: 0, padding: 12, overflowWrap: "anywhere", userSelect: "all", background: "var(--surface-2)", borderRadius: 8 }}>{created.token}</code>
          <button className="btn btn-secondary" type="button" onClick={() => void copyToken()}><Copy size={14} /> Copy token</button>
        </div>
        <div className="row wrap" style={{ marginTop: 10, justifyContent: "space-between" }}>
          <small className="muted">Agent ID: <code>{created.agent_id}</code></small>
          <button className="btn btn-secondary" type="button" onClick={() => setCreated(null)}>I've saved the token</button>
        </div>
      </section>}

      {error && <div className="notice warn" role="alert" style={{ marginTop: 16 }}>{error}</div>}
    </div>

    <div style={{ borderTop: "1px solid var(--line)", paddingTop: 18 }}>
      <div className="row wrap" style={{ justifyContent: "space-between", padding: "0 24px 12px" }}>
        <strong>Issued credentials</strong>
        <span className="muted">{items.length} total</span>
      </div>
      {loading ? <TableSkeleton rows={3} /> : items.length === 0 ? <Empty icon={<ShieldCheck size={20} />} title="No agent credentials yet">Create a credential above to connect an agent to this workspace.</Empty> : <div className="tbl-wrap">
        <table>
          <thead><tr><th>Agent</th><th>Permissions</th><th>Created</th><th>Expires</th><th>Last used</th><th>Status</th><th /></tr></thead>
          <tbody>{items.map((item) => {
            const status = credentialStatus(item);
            return <tr key={item.id}>
              <td><strong>{item.name}</strong><small className="muted" style={{ display: "block" }}>{item.agent_id}</small></td>
              <td>{item.scopes.map((scope) => <code key={scope} style={{ display: "block", marginBottom: 3 }}>{scope}</code>)}</td>
              <td>{dateLabel(item.created_at)}</td>
              <td>{item.expires_at ? dateLabel(item.expires_at) : "Never"}</td>
              <td>{dateLabel(item.last_used_at)}</td>
              <td>{status === "Active" ? <Tag tone="ok">Active</Tag> : status === "Expired" ? <Tag tone="warn">Expired</Tag> : <Tag>Revoked</Tag>}</td>
              <td>{status === "Active" && <button className="btn btn-secondary" type="button" onClick={() => void revoke(item)} disabled={revoking === item.id}><Trash2 size={13} /> {revoking === item.id ? "Revoking…" : "Revoke"}</button>}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>}
    </div>
  </Card>;
}
