import { useCallback, useEffect, useState } from "react";
import { ToastProvider, useToast } from "./components/ui";
import Sidebar from "./components/Sidebar";
import OrganizationProfileModal from "./components/OrganizationProfileModal";
import type { SidebarMode } from "./components/Sidebar";
import Topbar from "./components/Topbar";
import CommandPalette from "./components/CommandPalette";
import { CerbereDrawer, CreateBranchModal, DiffModal, EventInspector, PlansModal, ResolveModal, WriteModal } from "./components/modals";
import { useCohort, useOptional } from "./lib/useCohort";
import type { CohortWorkspace } from "./auth";
import type { Branch, CerbereSummary, Tab } from "./lib/types";
import type { Ctx } from "./pages/ctx";
import Overview from "./pages/Overview";
import Branches from "./pages/Branches";
import State from "./pages/State";
import Leases from "./pages/Leases";
import Events from "./pages/Events";
import Gateway from "./pages/Gateway";
import Performance from "./pages/Performance";
import Advisor from "./pages/Advisor";
import Settings from "./pages/Settings";
import Traction from "./pages/Traction";
import "./styles/app.css";

const TABS: Record<Tab, string> = { overview: "Overview", branches: "Branches", state: "Shared state", leases: "Leases", events: "Event log", gateway: "Gateway", performance: "Performance", advisor: "Advisor", settings: "Setting", traction: "Traction" };
const readTab = (): Tab => { const h = window.location.hash.replace(/^#\/?/, "") as Tab; return h in TABS ? h : "overview"; };
const stored = <T extends string>(key: string, allowed: T[], fallback: T): T => { try { const v = localStorage.getItem(key) as T | null; return v && allowed.includes(v) ? v : fallback; } catch { return fallback; } };

type Modalish = { kind: "create" } | { kind: "resolve"; branch: Branch } | { kind: "write"; branch: Branch } | { kind: "diff"; branch: Branch } | { kind: "plans" } | { kind: "cerbere" } | { kind: "palette" } | { kind: "inspector"; index: number } | null;

function Shell({ userEmail, onSignOut, workspaces, activeWorkspace, onWorkspaceChange }: { userEmail?: string; onSignOut?: () => void; workspaces?: CohortWorkspace[]; activeWorkspace?: CohortWorkspace; onWorkspaceChange?: (id: string) => void }) {
  const toast = useToast();
  const { data, refresh } = useCohort();
  const cerbere = useOptional<CerbereSummary>("/cerbere/summary?window=24h", 60000);
  const [tab, setTabState] = useState<Tab>(readTab); const [query, setQuery] = useState("");
  const [dark, setDark] = useState(() => { try { return localStorage.getItem("cohort-theme") === "dark"; } catch { return false; } });
  const [mode, setMode] = useState<SidebarMode>(() => stored<SidebarMode>("cohort-sidebar", ["expanded", "hover", "collapsed"], "expanded"));
  const [mobileOpen, setMobileOpen] = useState(false); const [modal, setModal] = useState<Modalish>(null); const [busy, setBusy] = useState(false); const [organizationProfileOpen, setOrganizationProfileOpen] = useState(false);

  useEffect(() => { document.documentElement.dataset.theme = dark ? "dark" : "light"; try { localStorage.setItem("cohort-theme", dark ? "dark" : "light"); } catch { /* storage unavailable */ } }, [dark]);
  useEffect(() => { try { localStorage.setItem("cohort-sidebar", mode); } catch { /* storage unavailable */ } }, [mode]);
  useEffect(() => { const on = () => setTabState(readTab()); window.addEventListener("hashchange", on); return () => window.removeEventListener("hashchange", on); }, []);
  useEffect(() => { document.title = `${TABS[tab]} · Cohort`; }, [tab]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = /INPUT|TEXTAREA|SELECT/.test((e.target as HTMLElement)?.tagName || "");
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setModal({ kind: "palette" }); }
      else if (e.key === "/" && !typing) { e.preventDefault(); setModal({ kind: "palette" }); }
    };
    window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey);
  }, []);

  const go = useCallback((next: Tab, q?: string) => { setQuery(q || ""); window.location.hash = `/${next}`; setTabState(next); setMobileOpen(false); }, []);
  const run = useCallback(async (action: () => Promise<unknown>, success: string) => {
    setBusy(true);
    try { await action(); await refresh(); toast(success); return true; }
    catch (err) { toast(err instanceof Error ? err.message : "Action failed", "err"); return false; }
    finally { setBusy(false); }
  }, [refresh, toast]);
  const close = useCallback(() => setModal(null), []);

  const ctx: Ctx = { data, busy, query, clearQuery: () => setQuery(""), go, refresh: () => void refresh(), run,
    openCreate: () => setModal({ kind: "create" }), openResolve: (branch) => setModal({ kind: "resolve", branch }), openWrite: (branch) => setModal({ kind: "write", branch }),
    openDiff: (branch) => setModal({ kind: "diff", branch }), openInspector: (index) => setModal({ kind: "inspector", index: Math.max(0, index) }) };
  const openBranches = data.branches.filter((b) => b.status === "open");

  return <div className="app">
    <div className="app-bg" aria-hidden /><div className="bg-noise" aria-hidden />
    <Sidebar tab={tab} onTab={(t) => go(t)} mode={mode} onMode={setMode} mobileOpen={mobileOpen} onCloseMobile={() => setMobileOpen(false)} dark={dark} onToggleDark={() => setDark(!dark)}
      openBranches={openBranches.length} conflicts={openBranches.filter((b) => b.conflicting_keys.length).length} blocked={cerbere.status === "ready" ? cerbere.data?.totals.blocked ?? null : null}
      userEmail={userEmail} onSignOut={onSignOut} workspaces={workspaces} activeWorkspace={activeWorkspace} onWorkspaceChange={onWorkspaceChange} onEditOrganization={() => setOrganizationProfileOpen(true)} onCerbere={() => setModal({ kind: "cerbere" })} onPlans={() => setModal({ kind: "plans" })} />
    <div className="main">
      <Topbar crumb={TABS[tab]} connected={data.connected} loaded={data.loaded} onSearch={() => setModal({ kind: "palette" })} onRefresh={() => void refresh()} onMenu={() => setMobileOpen(!mobileOpen)} />
      <main>
        {tab === "overview" && <Overview ctx={ctx} />}
        {tab === "branches" && <Branches ctx={ctx} />}
        {tab === "state" && <State ctx={ctx} />}
        {tab === "leases" && <Leases ctx={ctx} />}
        {tab === "events" && <Events ctx={ctx} />}
        {tab === "gateway" && <Gateway ctx={ctx} />}
        {tab === "performance" && <Performance ctx={ctx} />}
        {tab === "advisor" && <Advisor ctx={ctx} />}
        {tab === "traction" && <Traction />}
        {tab === "settings" && <Settings ctx={ctx} dark={dark} setDark={setDark} mode={mode} setMode={setMode} />}
      </main>
    </div>
    {modal?.kind === "create" && <CreateBranchModal onClose={close} run={run} busy={busy} />}
    {modal?.kind === "write" && <WriteModal branch={modal.branch} onClose={close} run={run} busy={busy} />}
    {modal?.kind === "resolve" && <ResolveModal branch={modal.branch} onClose={close} run={run} busy={busy} />}
    {modal?.kind === "diff" && <DiffModal branch={modal.branch} onClose={close} />}
    {modal?.kind === "plans" && <PlansModal onClose={close} />}
    {modal?.kind === "cerbere" && <CerbereDrawer onClose={close} />}
    {modal?.kind === "palette" && <CommandPalette data={data} onClose={close} onNavigate={go} />}
    {modal?.kind === "inspector" && <EventInspector events={data.events} index={modal.index} setIndex={(i) => setModal({ kind: "inspector", index: i })} onClose={close} />}
    {organizationProfileOpen && activeWorkspace && <OrganizationProfileModal workspace={activeWorkspace} onClose={() => setOrganizationProfileOpen(false)} onSaved={(profile) => onWorkspaceProfileChange?.({ id: profile.id, name: profile.name })} />}
  </div>;
}

export default function Dashboard(props: { userEmail?: string; onSignOut?: () => void; workspaces?: CohortWorkspace[]; activeWorkspace?: CohortWorkspace; onWorkspaceChange?: (id: string) => void; onWorkspaceProfileChange?: (profile: { id: string; name: string }) => void }) {
  return <ToastProvider><Shell {...props} /></ToastProvider>;
}
