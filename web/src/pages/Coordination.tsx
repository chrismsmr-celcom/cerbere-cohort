import { Activity, GitBranch, Lock, RadioTower } from "lucide-react";
import { Card, Disconnected, Empty, Metric, PageHead, TableSkeleton } from "../components/ui";
import { eventTone, short, timeAgo } from "../lib/format";
import type { Ctx } from "./ctx";

export default function Coordination({ ctx }: { ctx: Ctx }) {
  const { data } = ctx;
  const openBranches = data.branches.filter((branch) => branch.status === "open");
  const conflictingBranches = openBranches.filter((branch) => branch.conflicting_keys.length > 0);
  const conflictEvents = data.events.filter((event) => /conflict|stale|fenc|reject/i.test(event.action));
  const agentsSeen = new Set([
    ...data.events.map((event) => event.agent_id),
    ...data.leases.map((lease) => lease.agent_id),
  ].filter(Boolean));
  const eventCount = data.overview?.event_count ?? data.events.length;

  return <div className="page">
    <PageHead title="Coordination" description="A live view of how agents share state, acquire ownership and handle competing writes." />
    {data.error && <Disconnected message={data.error} onRetry={ctx.refresh} />}
    <div className="grid">
      {!data.loaded ? [0, 1, 2, 3].map((i) => <div key={i} className="card metric c3"><TableSkeleton rows={1} /></div>) : <>
        <Metric label="Coordination events" value={eventCount.toLocaleString()} hint="Recorded by this workspace" />
        <Metric label="Agents seen" value={agentsSeen.size.toLocaleString()} hint="Across loaded events and leases" />
        <Metric label="Active leases" value={data.leases.length.toLocaleString()} hint="Current ownership claims" />
        <Metric label="Open branches" value={openBranches.length.toLocaleString()} hint={`${conflictingBranches.length} with unresolved conflicts`} tone={conflictingBranches.length ? "err" : undefined} />
      </>}
      <Card className="c5" title="Coordination health" description="Current workspace state">
        {!data.loaded ? <TableSkeleton rows={3} /> : <>
          <div className="kv"><span><RadioTower size={15} /> API connection</span><strong className="data">{data.connected ? "Connected" : "Disconnected"}</strong></div>
          <div className="kv"><span><Lock size={15} /> Lease enforcement</span><strong className="data">{data.settings ? (data.settings.require_leases ? "Required" : "Advisory") : "Unknown"}</strong></div>
          <div className="kv"><span><GitBranch size={15} /> Branch conflicts</span><strong className="data">{conflictingBranches.length}</strong></div>
          <div className="kv"><span><Activity size={15} /> Conflict-related events</span><strong className="data">{conflictEvents.length}</strong></div>
        </>}
      </Card>
      <Card className="c7" title="Active ownership" description="Leases currently held by agents">
        {!data.loaded ? <TableSkeleton rows={3} /> : data.leases.length === 0 ? <Empty icon={<Lock size={20} />} title="No active leases">Leases appear here when agents claim shared resources.</Empty> :
          <div className="tbl-wrap"><table><thead><tr><th>Resource</th><th>Agent</th><th className="num">Fencing token</th><th className="num">Expires</th></tr></thead><tbody>
            {data.leases.map((lease) => <tr key={`${lease.key}-${lease.agent_id}`}><td><strong className="data">{lease.key}</strong></td><td>{lease.agent_id}</td><td className="num">{lease.fencing_token}</td><td className="num">{timeAgo(new Date(lease.expires_at * 1000).toISOString())}</td></tr>)}
          </tbody></table></div>}
      </Card>
      <Card className="c12" title="Recent coordination activity" description="Latest events recorded by this workspace" actions={<button className="btn btn-secondary" onClick={() => ctx.go("events")}>Open event log</button>}>
        {!data.loaded ? <TableSkeleton /> : data.events.length === 0 ? <Empty icon={<Activity size={20} />} title="No coordination activity yet">Events will appear when agents create branches, acquire leases, write shared state or resolve conflicts.</Empty> :
          <div className="tbl-wrap"><table><thead><tr><th>Event</th><th>Agent</th><th>Event ID</th><th className="num">Recorded</th></tr></thead><tbody>
            {data.events.slice(0, 12).map((event) => <tr key={event.event_id}><td><span className={`dot ${eventTone(event.action)}`} />{event.action}</td><td>{event.agent_id}</td><td><code>{short(event.event_id)}</code></td><td className="num">{timeAgo(event.timestamp)}</td></tr>)}
          </tbody></table></div>}
      </Card>
    </div>
  </div>;
}
