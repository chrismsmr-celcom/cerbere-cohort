import { useEffect, useMemo, useRef, useState } from "react";
import { Activity, Cable, Database, GitBranch, Lock, ScrollText, User } from "lucide-react";
import type { ReactNode } from "react";
import type { Data, Tab } from "../lib/types";

type Result = { id: string; group: string; label: string; hint?: string; icon: ReactNode; run: () => void };
const PAGES: { tab: Tab; label: string }[] = [
  { tab: "overview", label: "Overview" }, { tab: "branches", label: "Branches" }, { tab: "state", label: "Shared state" }, { tab: "leases", label: "Leases" },
  { tab: "events", label: "Event log" }, { tab: "gateway", label: "Gateway" }, { tab: "performance", label: "Performance" }, { tab: "advisor", label: "Advisor" },
  { tab: "coordination", label: "Coordination" }, { tab: "settings", label: "Setting" },
];

export default function CommandPalette({ data, onClose, onNavigate }: { data: Data; onClose: () => void; onNavigate: (tab: Tab, query?: string) => void }) {
  const [q, setQ] = useState(""); const [index, setIndex] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => { input.current?.focus(); }, []);
  const go = (tab: Tab, query?: string) => () => { onNavigate(tab, query); onClose(); };
  const results = useMemo<Result[]>(() => {
    const n = q.trim().toLowerCase(); const out: Result[] = [];
    const has = (s: string) => !n || s.toLowerCase().includes(n);
    PAGES.filter((p) => has(p.label)).forEach((p) => out.push({ id: `p-${p.tab}`, group: "Pages", label: p.label, icon: p.tab === "coordination" ? <Activity size={16} /> : <Cable size={16} />, run: go(p.tab) }));
    if (!n) return out;
    data.branches.filter((b) => has(`${b.name} ${b.agent_id}`)).slice(0, 5).forEach((b) => out.push({ id: `b-${b.id}`, group: "Branches", label: b.name, hint: b.agent_id, icon: <GitBranch size={16} />, run: go("branches", b.name) }));
    data.state.items.filter((s) => has(s.key)).slice(0, 5).forEach((s) => out.push({ id: `s-${s.key}`, group: "Shared state", label: s.key, icon: <Database size={16} />, run: go("state", s.key) }));
    data.leases.filter((l) => has(`${l.key} ${l.agent_id}`)).slice(0, 3).forEach((l) => out.push({ id: `l-${l.key}`, group: "Leases", label: l.key, hint: l.agent_id, icon: <Lock size={16} />, run: go("leases") }));
    data.events.filter((e) => has(`${e.action} ${e.agent_id} ${e.event_id}`)).slice(0, 5).forEach((e) => out.push({ id: `e-${e.event_id}`, group: "Events", label: e.action, hint: e.agent_id, icon: <ScrollText size={16} />, run: go("events", e.event_id) }));
    Array.from(new Set(data.events.map((e) => e.agent_id))).filter(has).slice(0, 4).forEach((a) => out.push({ id: `a-${a}`, group: "Agents", label: a, icon: <User size={16} />, run: go("gateway") }));
    return out;
  }, [q, data]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => setIndex(0), [q]);
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setIndex((i) => Math.min(results.length - 1, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setIndex((i) => Math.max(0, i - 1)); }
    else if (e.key === "Enter") { e.preventDefault(); results[index]?.run(); }
    else if (e.key === "Escape") onClose();
  };
  let last = "";
  return <div className="backdrop palette" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <div className="modal" role="dialog" aria-modal="true" aria-label="Search">
      <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={onKey} placeholder="Search pages, branches, keys, events and agents…" aria-label="Search" />
      <ul role="listbox">
        {results.map((r, i) => { const head = r.group !== last ? <div className="grp" key={`g-${r.group}`}>{r.group}</div> : null; last = r.group;
          return <li key={r.id}>{head}<button role="option" aria-selected={i === index} onMouseEnter={() => setIndex(i)} onClick={r.run}>{r.icon}<span>{r.label}</span>{r.hint && <span className="muted" style={{ marginLeft: "auto" }}>{r.hint}</span>}</button></li>; })}
        {!results.length && <li className="empty" style={{ padding: 24 }}>No match for “{q}”.</li>}
      </ul>
    </div>
  </div>;
}
