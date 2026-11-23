import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { API_BASE, ApiError, api } from "../lib/api";
import { getActiveWorkspaceId } from "../auth";
import { Card, Disconnected, Empty, PageHead, Seg, TableSkeleton, Tag, useToast } from "../components/ui";
import type { SidebarMode } from "../components/Sidebar";
import type { Ctx } from "./ctx";

const Line = ({ label, value }: { label: string; value: string }) => <div className="kv"><span>{label}</span><span className="data">{value}</span></div>;

type ExportConfig = {
  configured: boolean;
  provider?: string;
  endpoint?: string;
  enabled?: boolean;
  has_credentials?: boolean;
  last_success_at?: string | null;
  last_error?: string | null;
};
type Readiness = { ok: boolean; checks: Record<string, boolean> };
type Member = {
  user_id: string;
  email: string | null;
  full_name: string | null;
  job_title: string | null;
  role: string;
  status: string;
  created_at: string;
};
type Invitation = {
  id: string;
  email: string;
  role: string;
  expires_at: string;
  accepted_at: string | null;
  revoked_at: string | null;
  created_at: string;
};
type CreatedInvitation = { id: string; email: string; role: string; expires_at: string; token: string };

export default function Settings({ ctx, dark, setDark, mode, setMode, canManageTeam }: { ctx: Ctx; dark: boolean; setDark: (v: boolean) => void; mode: SidebarMode; setMode: (m: SidebarMode) => void; canManageTeam: boolean }) {
  const s = ctx.data.settings;
  const toast = useToast();
  const [exportConfig, setExportConfig] = useState<ExportConfig | null>(null);
  const [endpoint, setEndpoint] = useState("https://otlp.datadoghq.com/v1/logs");
  const [headerName, setHeaderName] = useState("dd-api-key");
  const [headerValue, setHeaderValue] = useState("");
  const [exportLoading, setExportLoading] = useState(true);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportMessage, setExportMessage] = useState("");
  const [exportError, setExportError] = useState("");

  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(true);
  const [readinessError, setReadinessError] = useState("");

  const [members, setMembers] = useState<Member[]>([]);
  const [membersLoading, setMembersLoading] = useState(true);
  const [membersError, setMembersError] = useState("");
  const [invitations, setInvitations] = useState<Invitation[]>([]);
  const [invitationsLoading, setInvitationsLoading] = useState(true);
  const [invitationsError, setInvitationsError] = useState("");
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"member" | "viewer" | "admin">("member");
  const [inviteExpiry, setInviteExpiry] = useState("7");
  const [inviteBusy, setInviteBusy] = useState(false);
  const [createdInvitation, setCreatedInvitation] = useState<CreatedInvitation | null>(null);
  const [revokingInvitationId, setRevokingInvitationId] = useState("");

  const loadExport = async () => {
    setExportLoading(true);
    try {
      const result = await api<ExportConfig>("/observability/export");
      setExportConfig(result);
      if (result.endpoint) setEndpoint(result.endpoint);
      setExportError("");
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "Unable to load observability settings.");
    } finally {
      setExportLoading(false);
    }
  };

  const loadReadiness = async () => {
    setReadinessLoading(true);
    setReadiness(null);
    setReadinessError("");
    try {
      const result = await api<Readiness>("/ready");
      setReadiness(result);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unable to check API readiness.";
      const detail = error instanceof ApiError ? error.detail : undefined;
      if (detail && typeof detail === "object" && "checks" in detail) {
        setReadiness({ ok: false, checks: (detail as { checks: Record<string, boolean> }).checks });
      }
      setReadinessError(message);
    } finally {
      setReadinessLoading(false);
    }
  };

  const loadMembers = async () => {
    setMembersLoading(true);
    try {
      const result = await api<{ items: Member[] }>("/organizations/members");
      setMembers(result.items || []);
      setMembersError("");
    } catch (error) {
      setMembersError(error instanceof Error ? error.message : "Unable to load workspace members.");
    } finally {
      setMembersLoading(false);
    }
  };

  const loadInvitations = async () => {
    setInvitationsLoading(true);
    try {
      const result = await api<{ items: Invitation[] }>("/organizations/invitations");
      setInvitations(result.items || []);
      setInvitationsError("");
    } catch (error) {
      setInvitationsError(error instanceof Error ? error.message : "Unable to load invitations.");
    } finally {
      setInvitationsLoading(false);
    }
  };

  useEffect(() => {
    void loadExport();
    void loadReadiness();
    if (canManageTeam) {
      void loadMembers();
      void loadInvitations();
    }
  }, [canManageTeam]);

  const saveExport = async (event: FormEvent) => {
    event.preventDefault();
    setExportBusy(true); setExportMessage(""); setExportError("");
    try {
      const headers = headerValue.trim() ? { [headerName.trim()]: headerValue } : undefined;
      const result = await api<ExportConfig>("/observability/export", {
        method: "PUT",
        body: JSON.stringify({ endpoint, ...(headers ? { headers } : {}) }),
      });
      setExportConfig({ ...result, configured: true, has_credentials: Boolean(headers) || exportConfig?.has_credentials });
      setHeaderValue("");
      setExportMessage("OTLP destination saved. New coordination events will be forwarded automatically.");
      await loadExport();
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "Unable to save observability destination.");
    } finally {
      setExportBusy(false);
    }
  };

  const testExport = async () => {
    setExportBusy(true); setExportMessage(""); setExportError("");
    try {
      const result = await api<{ ok: boolean; message: string }>("/observability/export/test", { method: "POST" });
      setExportMessage(result.message || "Test log delivered.");
      await loadExport();
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "OTLP connection test failed.");
    } finally {
      setExportBusy(false);
    }
  };

  const removeExport = async () => {
    if (!window.confirm("Remove the OTLP destination and stop exporting events for this workspace?")) return;
    setExportBusy(true); setExportMessage(""); setExportError("");
    try {
      await api("/observability/export", { method: "DELETE" });
      setExportConfig({ configured: false, enabled: false });
      setHeaderValue("");
      setExportMessage("External observability export removed.");
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "Unable to remove observability destination.");
    } finally {
      setExportBusy(false);
    }
  };

  const createInvitation = async (event: FormEvent) => {
    event.preventDefault();
    setInviteBusy(true);
    setInvitationsError("");
    setCreatedInvitation(null);
    try {
      const result = await api<CreatedInvitation>("/organizations/invitations", {
        method: "POST",
        body: JSON.stringify({
          email: inviteEmail.trim(),
          role: inviteRole,
          expires_in_days: Number(inviteExpiry),
        }),
      });
      setCreatedInvitation(result);
      setInviteEmail("");
      await loadInvitations();
    } catch (error) {
      setInvitationsError(error instanceof Error ? error.message : "Unable to create invitation.");
    } finally {
      setInviteBusy(false);
    }
  };

  const invitationUrl = (invitation: CreatedInvitation) => {
    const url = new URL("/app", window.location.origin);
    url.searchParams.set("invite", invitation.token);
    return url.toString();
  };

  const copyInvitation = async () => {
    if (!createdInvitation) return;
    try {
      await navigator.clipboard.writeText(invitationUrl(createdInvitation));
      toast("Invitation link copied");
    } catch {
      toast("Clipboard access is unavailable in this browser.", "err");
    }
  };

  const revokeInvitation = async (invitation: Invitation) => {
    if (!window.confirm("Revoke the invitation for " + invitation.email + "? The link will stop working.")) return;
    setRevokingInvitationId(invitation.id);
    setInvitationsError("");
    try {
      await api("/organizations/invitations/" + encodeURIComponent(invitation.id), { method: "DELETE" });
      toast("Invitation revoked");
      await loadInvitations();
    } catch (error) {
      setInvitationsError(error instanceof Error ? error.message : "Unable to revoke invitation.");
    } finally {
      setRevokingInvitationId("");
    }
  };

  const readinessEntries = readiness ? Object.entries(readiness.checks) : [];

  return <div className="page">
    <PageHead title="Setting" description="Workspace access, deployment readiness and operational configuration." />
    {ctx.data.error && <Disconnected message={ctx.data.error} onRetry={ctx.refresh} />}
    <div className="grid">
      <Card className="c12" title="Deployment readiness" description="Live checks against the REST API, PostgreSQL schema, Supabase authentication and CORS.">
        <div className="row wrap" style={{ alignItems: "center", justifyContent: "space-between", padding: "0 24px 12px" }}>
          {readinessLoading ? <span className="muted">Checking deployment…</span> : readiness?.ok
            ? <Tag tone="ok">All readiness checks passed</Tag>
            : <Tag tone="err">Not ready</Tag>}
          <button className="btn btn-secondary" type="button" onClick={() => void loadReadiness()} disabled={readinessLoading}>Recheck</button>
        </div>
        {readinessLoading ? <TableSkeleton rows={4} /> : <>
          {readinessError && <div className="notice warn" style={{ margin: 16 }}>{readinessError}</div>}
          {readinessEntries.length ? <div className="tbl-wrap"><table><thead><tr><th>Check</th><th>Status</th></tr></thead><tbody>
            {readinessEntries.map(([name, ok]) => <tr key={name}><td>{name.replace(/_/g, " ")}</td><td>{ok ? <Tag tone="ok">Passed</Tag> : <Tag tone="err">Failed</Tag>}</td></tr>)}
          </tbody></table></div> : !readinessError && <div className="notice warn" style={{ margin: 16 }}>No readiness response was returned.</div>}
        </>}
      </Card>

      {canManageTeam ? <>
      <Card className="c12" title="Workspace members" description="People who currently have access to this workspace. Visible to workspace owners and admins.">
        {membersLoading ? <TableSkeleton rows={3} /> : membersError ? <div className="notice warn" style={{ margin: 16 }}>{membersError}</div> : members.length === 0 ? <Empty title="No members found">This workspace does not have a member record yet.</Empty> : <div className="tbl-wrap"><table><thead><tr><th>Member</th><th>Role</th><th>Status</th><th>Joined</th></tr></thead><tbody>
          {members.map((member) => <tr key={member.user_id}><td><strong>{member.full_name || member.email || member.user_id.slice(0, 8)}</strong><small className="muted" style={{ display: "block" }}>{member.email || "No email available"}{member.job_title ? " · " + member.job_title : ""}</small></td><td>{member.role}</td><td>{member.status}</td><td>{new Date(member.created_at).toLocaleDateString()}</td></tr>)}
        </tbody></table></div>}
        <div className="row wrap" style={{ padding: "0 24px 20px" }}><button className="btn btn-secondary" type="button" onClick={() => void loadMembers()} disabled={membersLoading}>Refresh members</button></div>
      </Card>

      <Card className="c12" title="Invite a teammate" description="Create an expiring, single-use invitation link for a specific email address.">
        <form className="stack" style={{ padding: "0 24px 20px" }} onSubmit={createInvitation}>
          <div className="grid">
            <label className="field c6">Email address<input type="email" required maxLength={320} value={inviteEmail} onChange={(event) => setInviteEmail(event.target.value)} placeholder="teammate@company.com" /></label>
            <label className="field c2">Role<select value={inviteRole} onChange={(event) => setInviteRole(event.target.value as typeof inviteRole)}><option value="member">Member</option><option value="viewer">Viewer</option><option value="admin">Admin</option></select></label>
            <label className="field c2">Expires<select value={inviteExpiry} onChange={(event) => setInviteExpiry(event.target.value)}><option value="7">7 days</option><option value="14">14 days</option><option value="30">30 days</option></select></label>
            <div className="c2" style={{ display: "flex", alignItems: "end" }}><button className="btn btn-primary" type="submit" disabled={inviteBusy || !inviteEmail.trim()}>{inviteBusy ? "Creating…" : "Create invite"}</button></div>
          </div>
          <div className="notice">No invitation email is sent automatically in this beta. Create the link, copy it, and send it to the invited person yourself. They must sign in with the exact invited email address.</div>
          {createdInvitation && <div className="notice success">
            <div className="row wrap" style={{ justifyContent: "space-between" }}><strong>Invitation created for {createdInvitation.email}</strong><Tag tone="warn">Copy now</Tag></div>
            <p className="muted">This token is shown only once. Anyone with the link can attempt to accept it, but the signed-in account email must match.</p>
            <code style={{ display: "block", overflowWrap: "anywhere", padding: 12, background: "var(--surface-2)", borderRadius: 8 }}>{invitationUrl(createdInvitation)}</code>
            <button className="btn btn-secondary" type="button" onClick={() => void copyInvitation()}>Copy invitation link</button>
          </div>}
          {invitationsError && <div className="notice warn" role="alert">{invitationsError}</div>}
        </form>
        {invitationsLoading ? <TableSkeleton rows={3} /> : invitations.length === 0 ? <Empty title="No invitations yet">Invitations you create will appear here.</Empty> : <div className="tbl-wrap"><table><thead><tr><th>Email</th><th>Role</th><th>Expires</th><th>Status</th><th /></tr></thead><tbody>
          {invitations.map((invitation) => {
            const expired = new Date(invitation.expires_at).getTime() <= Date.now();
            const status = invitation.revoked_at ? "Revoked" : invitation.accepted_at ? "Accepted" : expired ? "Expired" : "Pending";
            return <tr key={invitation.id}><td>{invitation.email}</td><td>{invitation.role}</td><td>{new Date(invitation.expires_at).toLocaleDateString()}</td><td>{status === "Pending" ? <Tag tone="ok">Pending</Tag> : <Tag>{status}</Tag>}</td><td>{status === "Pending" && <button className="btn btn-secondary" type="button" disabled={revokingInvitationId === invitation.id} onClick={() => void revokeInvitation(invitation)}>{revokingInvitationId === invitation.id ? "Revoking…" : "Revoke"}</button>}</td></tr>;
          })}
        </tbody></table></div>}
        <div className="row wrap" style={{ padding: "0 24px 20px" }}><button className="btn btn-secondary" type="button" onClick={() => void loadInvitations()} disabled={invitationsLoading}>Refresh invitations</button></div>
      </Card>

      </> : <Card className="c12" title="Workspace access" description="Team management is restricted to workspace owners and admins.">
        <div className="notice" style={{ margin: 16 }}>Your current role does not have permission to view the member directory or manage invitations. Ask a workspace owner or admin to manage access.</div>
      </Card>}
      <Card className="c6" title="Storage and coordination" description="Reported by /api/settings">
        <Line label="Storage backend" value={s?.persistence || "—"} /><Line label="Database path" value={s?.database_path || "—"} />
        <Line label="Lease enforcement" value={s ? (s.require_leases ? "Required for writes" : "Advisory") : "—"} />
        <Line label="Database transactions" value={s ? (s.database_transactions ? "Yes" : "No") : "—"} />
      </Card>
      <Card className="c6" title="Runtime capabilities" description="Features reported by the API adapter">
        <Line label="Multi-process coordination" value={s ? (s.multi_process ? "Enabled" : "Single process") : "—"} />
        <Line label="Authentication" value={s ? (s.authentication === "supabase" ? "Supabase JWT + membership checks" : "Not configured (development)") : "—"} />
        <div style={{ padding: "16px 24px 24px" }}><div className="notice warn"><div className="grow"><strong>Deployment note</strong>Workspace access is selected per signed-in user. Membership is checked by the API on each request.</div></div></div>
      </Card>

      <Card className="c12 observability-card" title="External observability" description="Forward Cohort coordination events to your OTLP/HTTP logs backend.">
        {exportLoading ? <div className="muted">Loading exporter configuration…</div> : <>
          <div className="observability-heading">
            <div className="observability-copy">
              <strong>OpenTelemetry Protocol (OTLP/HTTP)</strong>
              <div className="muted">Datadog, Grafana, Honeycomb, New Relic, Splunk OTel Collector, Elastic and other OTLP-compatible endpoints.</div>
            </div>
            {exportConfig?.configured && exportConfig.enabled ? <Tag tone="ok">Enabled</Tag> : <Tag tone="warn">Not configured</Tag>}
          </div>
          <form className="stack observability-form" onSubmit={saveExport}>
            <label className="field">OTLP logs endpoint
              <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="https://otlp.datadoghq.com/v1/logs" type="url" required />
              <small>For Datadog, use your region's OTLP endpoint. Cohort appends /v1/logs if omitted.</small>
            </label>
            <div className="grid observability-auth-grid">
              <label className="field c4">Authentication header<input value={headerName} onChange={(e) => setHeaderName(e.target.value)} placeholder="dd-api-key" /></label>
              <label className="field c8">Header value / API key<input value={headerValue} onChange={(e) => setHeaderValue(e.target.value)} type="password" autoComplete="new-password" placeholder={exportConfig?.has_credentials ? "Saved securely — leave blank to keep it" : "Paste your API key or bearer token"} /></label>
            </div>
            <div className="row wrap">
              <button className="btn btn-primary" type="submit" disabled={exportBusy}>{exportBusy ? "Saving…" : "Save destination"}</button>
              <button className="btn btn-secondary" type="button" onClick={testExport} disabled={exportBusy || !exportConfig?.configured}>Send test log</button>
              {exportConfig?.configured && <button className="btn btn-secondary" type="button" onClick={removeExport} disabled={exportBusy}>Remove destination</button>}
            </div>
          </form>
          {exportMessage && <div className="notice success" style={{ marginTop: 14 }}>{exportMessage}</div>}
          {exportError && <div className="notice warn" style={{ marginTop: 14 }}>{exportError}</div>}
          {exportConfig?.last_success_at && <Line label="Last successful delivery" value={new Date(exportConfig.last_success_at).toLocaleString()} />}
          {exportConfig?.last_error && <div className="notice warn" style={{ marginTop: 12 }}>Latest delivery status: {exportConfig.last_error}</div>}
          <div className="notice" style={{ marginTop: 14 }}><strong>Data minimization</strong><div className="muted">Cohort exports event IDs, agent/action metadata, lease and branch coordination details. It does not export shared-state values, prompts, credentials or full change payloads. Delivery is asynchronous and retried while the endpoint is unavailable.</div></div>
        </>}
      </Card>

      <Card className="c6" title="Beta support" description="Report reproducible problems and keep production systems out of this beta.">
        <div style={{ padding: "0 24px 20px" }} className="stack">
          <p>Use synthetic data and non-production agents. Do not submit customer data, credentials, personal information or confidential records.</p>
          <div className="row wrap">
            <a className="btn btn-secondary" href="https://github.com/chrismsmr-celcom/cerbere-cohort/issues/new?template=beta-feedback.yml" target="_blank" rel="noreferrer">Report beta feedback</a>
            <a className="btn btn-secondary" href="https://github.com/chrismsmr-celcom/cerbere-cohort/blob/main/SECURITY.md" target="_blank" rel="noreferrer">Security reporting instructions</a>
          </div>
          <small className="muted">Cohort is coordination infrastructure. It is not a code sandbox, tool permission system or production safety boundary.</small>
        </div>
      </Card>

      <Card className="c6" title="Appearance" description="Saved in this browser">
        <div className="kv"><span>Theme</span><Seg label="Theme" value={dark ? "dark" : "light"} onChange={(v) => setDark(v === "dark")} options={[{ id: "light", label: "Light" }, { id: "dark", label: "Dark" }]} /></div>
        <div className="kv"><span>Sidebar</span><Seg label="Sidebar mode" value={mode} onChange={setMode} options={[{ id: "expanded", label: "Expanded" }, { id: "hover", label: "On hover" }, { id: "collapsed", label: "Collapsed" }]} /></div>
      </Card>
      <Card className="c6" title="API connection" description="Set at build time with VITE_* variables">
        <Line label="API base URL" value={API_BASE || "Same origin (proxied to /api)"} />
        <Line label="Active workspace ID" value={getActiveWorkspaceId() ? `${getActiveWorkspaceId().slice(0, 8)}…` : "Not selected"} />
        <div className="kv"><span>Status</span>{ctx.data.connected ? <Tag tone="ok">Connected</Tag> : <Tag tone="err">Offline</Tag>}</div>
      </Card>
    </div>
  </div>;
}
