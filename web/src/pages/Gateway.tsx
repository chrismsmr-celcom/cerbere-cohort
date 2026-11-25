import { useCallback, useEffect, useMemo, useState } from "react";
import { Cable, Copy, KeyRound, Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { API_BASE, api } from "../lib/api";
import { Card, Disconnected, Empty, PageHead, Seg, TableSkeleton, Tag, useToast } from "../components/ui";
import { timeAgo } from "../lib/format";
import type { Ctx } from "./ctx";

type Row = { id: string; last: string | null; events: number; branches: number; leases: number };
type AgentCredential = {
  id: string;
  agent_id: string;
  name: string;
  scopes: string[];
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
};
type CreatedCredential = AgentCredential & { token: string };

// Keep this fallback aligned with the public Render MCP service. Override it
// at build time with VITE_MCP_URL when a custom domain is configured.
const MCP_URL = (import.meta as any).env?.VITE_MCP_URL || "https://cerbere-cohort.onrender.com/mcp";
const ALL_SCOPES = ["workspace:read", "workspace:write", "leases:read", "leases:write"];

export default function Gateway({ ctx }: { ctx: Ctx }) {
  const { data } = ctx;
  const toast = useToast();
  const [snippet, setSnippet] = useState<"mcp" | "vibe" | "http" | "python">("mcp");
  const [credentials, setCredentials] = useState<AgentCredential[]>([]);
  const [credentialsLoading, setCredentialsLoading] = useState(true);
  const [credentialsError, setCredentialsError] = useState("");
  const [credentialName, setCredentialName] = useState("");
  const [agentId, setAgentId] = useState("");
  const [expiryDays, setExpiryDays] = useState("30");
  const [creating, setCreating] = useState(false);
  const [createdCredential, setCreatedCredential] = useState<CreatedCredential | null>(null);
  const [revokingId, setRevokingId] = useState("");

  const base = API_BASE || window.location.origin;
  const rows = useMemo<Row[]>(() => {
    const map = new Map<string, Row>();
    const get = (id: string): Row => {
      let row = map.get(id);
      if (!row) {
        row = { id, last: null, events: 0, branches: 0, leases: 0 };
        map.set(id, row);
      }
      return row;
    };
    data.events.forEach((event) => {
      const row = get(event.agent_id);
      row.events += 1;
      if (!row.last || event.timestamp > row.last) row.last = event.timestamp;
    });
    data.branches.forEach((branch) => { get(branch.agent_id).branches += 1; });
    data.leases.forEach((lease) => { get(lease.agent_id).leases += 1; });
    return [...map.values()].sort((a, b) => (b.last || "").localeCompare(a.last || ""));
  }, [data.events, data.branches, data.leases]);

  const loadCredentials = useCallback(async () => {
    setCredentialsLoading(true);
    setCredentialsError("");
    try {
      const result = await api<{ items: AgentCredential[] }>("/agents/credentials");
      setCredentials(result.items || []);
    } catch (error) {
      setCredentialsError(error instanceof Error ? error.message : "Unable to load agent credentials.");
    } finally {
      setCredentialsLoading(false);
    }
  }, []);

  useEffect(() => { void loadCredentials(); }, [loadCredentials]);

  const snippets = {
    mcp: [
      "{",
      '  "mcpServers": {',
      '    "cohort": {',
      '      "url": "' + MCP_URL + '",',
      '      "headers": { "Authorization": "Bearer <agent-credential>" }',
      "    }",
      "  }",
      "}",
    ].join("\n"),
    vibe: [
      "# PowerShell — paste the credential when prompted",
      '$env:COHORT_AGENT_TOKEN = Read-Host "Cohort Agent Credential"',
      "vibe mcp add cohort --url " + MCP_URL + " --transport streamable-http --api-key-env COHORT_AGENT_TOKEN",
      "vibe",
    ].join("\n"),
    http: [
      "curl -X POST " + base + "/api/branches \\",
      '  -H "Content-Type: application/json" \\',
      '  -H "X-Organization-ID: <workspace-id>" \\',
      '  -H "Authorization: Bearer <agent-credential>" \\',
      '  -d \'{"agent_id":"<agent-id>","name":"first-task"}\'',
    ].join("\n"),
    python: [
      "# Local-only SDK example (not connected to the hosted workspace)",
      "from cerbere_cohort import Workspace",
      "",
      "workspace = Workspace(require_leases=True)",
      'branch = workspace.create_branch("agent-a", "first-task")',
      'workspace.acquire_lease("agent-a", "task/status", duration_seconds=60)',
      'branch.set("task/status", "started")',
      'workspace.commit("agent-a", branch)',
    ].join("\n"),
  };

  const createCredential = async () => {
    if (!credentialName.trim() || !agentId.trim()) return;
    setCreating(true);
    setCredentialsError("");
    setCreatedCredential(null);
    try {
      const result = await api<CreatedCredential>("/agents/credentials", {
        method: "POST",
        body: JSON.stringify({
          agent_id: agentId.trim(),
          name: credentialName.trim(),
          scopes: ALL_SCOPES,
          expires_in_days: Number(expiryDays),
        }),
      });
      setCreatedCredential(result);
      setCredentialName("");
      setAgentId("");
      toast("Agent credential created. Copy it now; it will not be shown again.");
      await loadCredentials();
    } catch (error) {
      setCredentialsError(error instanceof Error ? error.message : "Unable to create agent credential.");
    } finally {
      setCreating(false);
    }
  };

  const revokeCredential = async (credential: AgentCredential) => {
    if (!window.confirm("Revoke the credential for " + credential.agent_id + "? Existing clients using it will lose access.")) return;
    setRevokingId(credential.id);
    setCredentialsError("");
    try {
      await api("/agents/credentials/" + encodeURIComponent(credential.id), { method: "DELETE" });
      toast("Agent credential revoked");
      await loadCredentials();
    } catch (error) {
      setCredentialsError(error instanceof Error ? error.message : "Unable to revoke agent credential.");
    } finally {
      setRevokingId("");
    }
  };

  const copy = async (value: string, label: string) => {
    try {
      await navigator.clipboard.writeText(value);
      toast(label + " copied");
    } catch {
      toast("Clipboard access is unavailable in this browser.", "err");
    }
  };

  return <div className="page">
    <PageHead title="Gateway" description="Create scoped credentials and connect agents to this workspace." />
    {data.error && <Disconnected message={data.error} onRetry={ctx.refresh} />}
    <div className="grid">
      <Card className="c8" title="Observed agents" description="Agents seen in workspace events, branches and active leases">
        {!data.loaded ? <TableSkeleton /> : <div className="tbl-wrap"><table><thead><tr><th>Agent</th><th className="num">Events</th><th className="num">Branches</th><th className="num">Leases</th><th className="num">Last activity</th></tr></thead><tbody>
          {rows.map((row) => <tr key={row.id}><td><strong>{row.id}</strong></td><td className="num">{row.events}</td><td className="num">{row.branches}</td><td className="num">{row.leases}</td><td className="num">{row.last ? timeAgo(row.last) : "—"}</td></tr>)}
          {!rows.length && <tr><td colSpan={5}><Empty icon={<Cable size={20} />} title="No agents connected yet">Create a credential below, then connect an agent using MCP or the REST API.</Empty></td></tr>}
        </tbody></table></div>}
      </Card>
      <Card className="c4" title="Connect an agent" description="Use the hosted MCP endpoint or the REST API" flush={false}>
        <div className="stack">
          <Seg label="Connection type" value={snippet} onChange={setSnippet} options={[{ id: "mcp", label: "Generic MCP" }, { id: "vibe", label: "Mistral Vibe CLI" }, { id: "http", label: "REST API" }, { id: "python", label: "Local Python" }]} />
          <pre className="code">{snippets[snippet]}</pre>
          <button className="btn btn-secondary" onClick={() => void copy(snippets[snippet], "Connection snippet")}><Copy size={14} />Copy snippet</button>
          {snippet === "mcp" && <p className="muted">Generic MCP configuration example. Your client must support Streamable HTTP and custom request headers.</p>}
          {snippet === "vibe" && <p className="muted">Run these commands in PowerShell, outside an active Vibe session. This registers static Bearer authentication and avoids the browser-based OAuth flow. Keep the same terminal open when launching Vibe.</p>}
          {snippet === "http" && <p className="muted">Use the workspace ID shown in Setting. The credential must belong to the agent_id in the request.</p>}
          {snippet === "python" && <p className="muted">This example runs locally in-process. It does not connect to the hosted Cohort API.</p>}
        </div>
      </Card>

      <Card className="c12" title="Agent credentials" description="Credentials authenticate one agent and enforce its workspace and lease scopes." flush={false}>
        <div className="grid">
          <div className="c5 stack">
            <label className="field">Credential name<input value={credentialName} onChange={(event) => setCredentialName(event.target.value)} placeholder="Backend worker" maxLength={120} /></label>
            <label className="field">Agent ID<input value={agentId} onChange={(event) => setAgentId(event.target.value)} placeholder="backend-worker-01" maxLength={128} /></label>
            <label className="field">Expires after<select value={expiryDays} onChange={(event) => setExpiryDays(event.target.value)}><option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option><option value="180">180 days</option><option value="365">365 days</option></select></label>
            <div className="notice"><strong>Granted scopes</strong><div className="muted">workspace:read · workspace:write · leases:read · leases:write</div></div>
            <button className="btn btn-primary" disabled={creating || !credentialName.trim() || !agentId.trim()} onClick={() => void createCredential()}><Plus size={15} />{creating ? "Creating…" : "Create agent credential"}</button>
          </div>
          <div className="c7 stack">
            {createdCredential && <div className="notice success">
              <div className="row between"><strong>Copy this credential now</strong><Tag tone="warn">Shown once</Tag></div>
              <p className="muted">Cohort stores only its hash. You cannot retrieve this token later. If you lose it, revoke it and create another.</p>
              <code style={{ display: "block", overflowWrap: "anywhere", padding: 12, background: "var(--surface-2)", borderRadius: 8 }}>{createdCredential.token}</code>
              <button className="btn btn-secondary" onClick={() => void copy(createdCredential.token, "Agent credential")}><Copy size={14} />Copy credential</button>
            </div>}
            {credentialsError && <div className="notice warn" role="alert">{credentialsError}</div>}
            {credentialsLoading ? <TableSkeleton rows={3} /> : credentials.length === 0 ? <Empty icon={<KeyRound size={20} />} title="No agent credentials">Create one credential per agent. Use short expiry periods for temporary testers.</Empty> :
              <div className="tbl-wrap"><table><thead><tr><th>Agent</th><th>Credential</th><th>Expires</th><th>Last used</th><th>Status</th><th /></tr></thead><tbody>
                {credentials.map((credential) => {
                  const expired = credential.expires_at ? new Date(credential.expires_at).getTime() <= Date.now() : false;
                  const status = credential.revoked_at ? "Revoked" : expired ? "Expired" : "Active";
                  return <tr key={credential.id}>
                    <td><strong>{credential.agent_id}</strong><small className="muted" style={{ display: "block" }}>{credential.name}</small></td>
                    <td><small>{credential.scopes.join(", ")}</small></td>
                    <td>{credential.expires_at ? new Date(credential.expires_at).toLocaleDateString() : "Never"}</td>
                    <td>{credential.last_used_at ? timeAgo(credential.last_used_at) : "Never"}</td>
                    <td>{status === "Active" ? <Tag tone="ok">Active</Tag> : <Tag>{status}</Tag>}</td>
                    <td>{status === "Active" && <button className="btn btn-secondary" disabled={revokingId === credential.id} onClick={() => void revokeCredential(credential)}><Trash2 size={14} />{revokingId === credential.id ? "Revoking…" : "Revoke"}</button>}</td>
                  </tr>;
                })}
              </tbody></table></div>}
            <button className="btn btn-secondary" onClick={() => void loadCredentials()} disabled={credentialsLoading}><RefreshCw size={14} />Refresh credentials</button>
          </div>
        </div>
      </Card>
      <Card className="c12" title="Access model" description="Keep each agent identity separate">
        <div className="row wrap">
          <div className="grow"><ShieldCheck size={17} /><strong>One credential per agent</strong><p className="muted">Credentials are bound to one agent ID. An agent cannot write as another agent.</p></div>
          <div className="grow"><KeyRound size={17} /><strong>Scoped access</strong><p className="muted">Read, write, and lease operations are checked against the credential's scopes.</p></div>
          <div className="grow"><Trash2 size={17} /><strong>Revocable</strong><p className="muted">Revoke a credential immediately when a tester or agent no longer needs access.</p></div>
        </div>
      </Card>
    </div>
  </div>;
}
