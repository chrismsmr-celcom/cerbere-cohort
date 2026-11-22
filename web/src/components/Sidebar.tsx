import { useEffect, useRef, useState } from "react";
import { Cable, Activity, Building2, ChevronsUpDown, Database, Gauge, GitBranch, LayoutDashboard, Lightbulb, LogOut, Lock, Moon, PanelLeft, ScrollText, Settings, Sparkles, Sun } from "lucide-react";
import type { ReactNode } from "react";
import type { Tab } from "../lib/types";
import type { CohortWorkspace } from "../auth";
import cohortLogo from "../cohortLogo";
import cerbereIcon from "../assets/cerbere-ag.png";

export type SidebarMode = "expanded" | "hover" | "collapsed";
const MODES: { id: SidebarMode; label: string; hint: string }[] = [
  { id: "expanded", label: "Expanded", hint: "Sidebar stays open" },
  { id: "hover", label: "Expand on hover", hint: "Collapsed, opens when you point at it" },
  { id: "collapsed", label: "Collapsed", hint: "Icons only" },
];

function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const down = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", down); document.addEventListener("keydown", key);
    return () => { document.removeEventListener("mousedown", down); document.removeEventListener("keydown", key); };
  }, [open]);
  return { open, setOpen, ref };
}

export default function Sidebar(props: {
  tab: Tab; onTab: (t: Tab) => void; mode: SidebarMode; onMode: (m: SidebarMode) => void; mobileOpen: boolean; onCloseMobile: () => void;
  dark: boolean; onToggleDark: () => void; openBranches: number; conflicts: number; blocked: number | null;
  userEmail?: string; onSignOut?: () => void; workspaces?: CohortWorkspace[]; activeWorkspace?: CohortWorkspace; onWorkspaceChange?: (id: string) => void; onEditOrganization: () => void; onCerbere: () => void; onPlans: () => void;
}) {
  const { tab, onTab, mode } = props;
  const modePop = usePopover(); const accountPop = usePopover();
  const item = (id: Tab, label: string, icon: ReactNode, count?: number) =>
    <button key={id} className="nav-item" aria-current={tab === id ? "page" : undefined} title={label} onClick={() => { onTab(id); props.onCloseMobile(); }}>
      {icon}<span className="lbl">{label}</span>{count ? <span className="count lbl">{count}</span> : null}
    </button>;
  return <div className="side-slot" data-mode={mode} data-open={props.mobileOpen}>
    <aside className="side" aria-label="Primary">
      <div className="brand"><img src={cohortLogo} alt="" /><strong className="lbl">Cohort<span className="tag-beta">Beta</span></strong></div>
      <div className="ws-pick"><button type="button" className="ws-profile-trigger" onClick={props.onEditOrganization} title="Edit organization profile"><div className="av">{props.activeWorkspace?.logo_url ? <img src={props.activeWorkspace.logo_url} alt="" /> : (props.activeWorkspace?.name || "W").slice(0, 1).toUpperCase()}</div><span className="lbl ws-profile-copy"><strong>{props.activeWorkspace?.name || "Local workspace"}</strong><small>{props.activeWorkspace?.role || "Local mode"}</small></span><Building2 size={15} className="ws-profile-edit" /></button>{props.workspaces && props.workspaces.length > 1 ? <select aria-label="Switch workspace" value={props.activeWorkspace?.id || ""} onChange={(e) => props.onWorkspaceChange?.(e.target.value)} style={{ width: 18, minWidth: 18, border: 0, background: "transparent", color: "var(--muted)" }}>{props.workspaces.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}</select> : null}</div>
      <nav className="nav-scroll">
        <div className="nav-group">Workspace</div>
        {item("overview", "Overview", <LayoutDashboard size={18} />)}
        {item("branches", "Branches", <GitBranch size={18} />, props.openBranches)}
        {item("state", "Shared state", <Database size={18} />)}
        {item("leases", "Leases", <Lock size={18} />)}
        {item("events", "Event log", <ScrollText size={18} />)}
        <div className="nav-group">Operate</div>
        {item("gateway", "Gateway", <Cable size={18} />)}
        {item("performance", "Performance", <Gauge size={18} />)}
        {item("advisor", "Advisor", <Lightbulb size={18} />)}
        {item("coordination", "Coordination", <Activity size={18} />)}
        {item("settings", "Setting", <Settings size={18} />)}
      </nav>
      <div className="side-foot">
        <button className="cerbere-btn" onClick={props.onCerbere} title="Cerbère AG security">
          <img src={cerbereIcon} alt="" />
          <span className="lbl"><span>Cerbère AG</span><small>Security summary</small></span>
          {props.blocked ? <span className="count lbl">{props.blocked}</span> : null}
        </button>
        <button className="nav-item" onClick={props.onPlans} title="Update plan"><Sparkles size={18} /><span className="lbl">Update Plan</span></button>
        <div className="pop-wrap" ref={accountPop.ref}>
          <button className="account" onClick={() => accountPop.setOpen(!accountPop.open)} aria-haspopup="menu" aria-expanded={accountPop.open} title={props.userEmail || "Local mode"}>
            <span className="av">{(props.userEmail || "L")[0].toUpperCase()}</span>
            <span className="lbl"><strong>{props.userEmail || "Local mode"}</strong><small>{props.userEmail ? "Signed in" : "Authentication disabled"}</small></span>
            <ChevronsUpDown size={14} className="lbl" />
          </button>
          {accountPop.open && <div className="popover" role="menu">
            <div className="head"><strong>{props.userEmail || "Local mode"}</strong><span className="opt-d">{props.userEmail ? "Signed in" : "Authentication disabled"}</span></div>
            {props.onSignOut ? <button role="menuitem" onClick={props.onSignOut}><LogOut size={16} />Sign out</button> : <span className="opt-d" style={{ padding: "8px 12px" }}>No session to end in local mode.</span>}
          </div>}
        </div>
        <button className="nav-item" onClick={props.onToggleDark} role="switch" aria-checked={props.dark} title="Toggle dark mode">
          {props.dark ? <Sun size={18} /> : <Moon size={18} />}<span className="lbl">Dark mode</span><span className="count lbl">{props.dark ? "On" : "Off"}</span>
        </button>
        <div className="pop-wrap" ref={modePop.ref}>
          <button className="nav-item" onClick={() => modePop.setOpen(!modePop.open)} aria-haspopup="menu" aria-expanded={modePop.open} title="Sidebar control"><PanelLeft size={18} /><span className="lbl">Sidebar control</span></button>
          {modePop.open && <div className="popover" role="menu" aria-label="Sidebar mode">
            {MODES.map((m) => <button key={m.id} role="menuitemradio" aria-checked={mode === m.id} onClick={() => { props.onMode(m.id); modePop.setOpen(false); }}>
              <span style={{ width: 16, color: "var(--accent)" }}>{mode === m.id ? "●" : "○"}</span><span><strong style={{ fontWeight: 600 }}>{m.label}</strong><span className="opt-d">{m.hint}</span></span>
            </button>)}
          </div>}
        </div>
      </div>
    </aside>
  </div>;
}
