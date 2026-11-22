import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { API_BASE, api } from "../lib/api";
import { getActiveWorkspaceId } from "../auth";
import { Card, Disconnected, PageHead, Seg, Tag } from "../components/ui";
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
export default function Settings({ ctx, dark, setDark, mode, setMode }: { ctx: Ctx; dark: boolean; setDark: (v: boolean) => void; mode: SidebarMode; setMode: (m: SidebarMode) => void }) {
  const s = ctx.data.settings;
  const [exportConfig, setExportConfig] = useState<ExportConfig | null>(null);
  const [endpoint, setEndpoint] = useState("https://otlp.datadoghq.com/v1/logs");
  const [headerName, setHeaderName] = useState("dd-api-key");
  const [headerValue, setHeaderValue] = useState("");
  const [exportLoading, setExportLoading] = useState(true);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportMessage, setExportMessage] = useState("");
  const [exportError, setExportError] = useState("");

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
  useEffect(() => { void loadExport(); }, []);

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

  return <div className="page">
    <PageHead title="Setting" description="Inspect the configuration and operational limits of this Cohort instance." />
    {ctx.data.error && <Disconnected message={ctx.data.error} onRetry={ctx.refresh} />}
    <div className="grid">
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
      <Card className="c12" title="External observability" description="Forward Cohort coordination events to your OTLP/HTTP logs backend.">
        {exportLoading ? <div className="muted">Loading exporter configuration…</div> : <>
          <div className="row between" style={{ marginBottom: 14 }}>
            <div>
              <strong>OpenTelemetry Protocol (OTLP/HTTP)</strong>
              <div className="muted">Datadog, Grafana, Honeycomb, New Relic, Splunk OTel Collector, Elastic and any OTLP-compatible endpoint.</div>
            </div>
            {exportConfig?.configured && exportConfig.enabled
              ? <Tag tone="ok">Enabled</Tag>
              : <Tag tone="warn">Not configured</Tag>}
          </div>
          <form className="stack" onSubmit={saveExport}>
            <label className="field">OTLP logs endpoint
              <input value={endpoint} onChange={(e) => setEndpoint(e.target.value)} placeholder="https://otlp.datadoghq.com/v1/logs" type="url" required />
              <small>For Datadog, use your region's OTLP endpoint. Cohort appends /v1/logs if omitted.</small>
            </label>
            <div className="grid">
              <label className="field c4">Authentication header
                <input value={headerName} onChange={(e) => setHeaderName(e.target.value)} placeholder="dd-api-key" />
              </label>
              <label className="field c8">Header value / API key
                <input value={headerValue} onChange={(e) => setHeaderValue(e.target.value)} type="password" autoComplete="new-password" placeholder={exportConfig?.has_credentials ? "Saved securely — leave blank to keep it" : "Paste your API key or bearer token"} />
              </label>
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
          <div className="notice" style={{ marginTop: 14 }}>
            <strong>Data minimization</strong>
            <div className="muted">Cohort exports event IDs, agent/action metadata, lease and branch coordination details. It does not export shared-state values, prompts, credentials or full change payloads. Delivery is asynchronous and retried while the endpoint is unavailable.</div>
          </div>
        </>}
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
