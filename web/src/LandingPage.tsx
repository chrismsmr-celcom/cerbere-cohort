import { useState } from "react";
import cohortLogo from "./cohortLogo";
import {
  ArrowRight, ArrowUpRight, Check, CircleDot, Clock3, GitBranch,
  GitCommitHorizontal, GitMerge, Github, Layers3, LockKeyhole,
  Radio, ShieldCheck, Workflow, Zap,
} from "lucide-react";
import "./landing.css";

const steps = [
  { agent: "Agent A", task: "Claim shared task", detail: "lease acquired · token 41", tone: "violet" },
  { agent: "Agent B", task: "Read current state", detail: "revision 12 · snapshot pinned", tone: "blue" },
  { agent: "Agent B", task: "Take over expired lease", detail: "lease renewed · token 42", tone: "mint" },
  { agent: "Agent A", task: "Attempt stale write", detail: "rejected · fencing token 41", tone: "rose" },
];

const capabilities = [
  { icon: LockKeyhole, number: "01", title: "Explicit ownership", copy: "Leases make it clear which agent owns a task, and when that ownership expires." },
  { icon: GitCommitHorizontal, number: "02", title: "Fenced writes", copy: "Every ownership change advances a fencing token. Stale workers cannot overwrite newer work." },
  { icon: GitMerge, number: "03", title: "Conflict-aware state", copy: "Agents work against versioned shared state, with conflicts surfaced instead of silently lost." },
  { icon: Radio, number: "04", title: "Observable execution", copy: "Export coordination events and traces to the monitoring stack your team already uses." },
];

const integrations = ["OpenTelemetry", "Splunk", "Datadog", "Microsoft Sentinel", "Elasticsearch", "MCP"];
const frameworks = [
  { name: "LangChain / LangGraph", logo: "https://raw.githubusercontent.com/ag-ui-protocol/ag-ui/main/.github/readme/logos/langchain.svg", alt: "LangChain logo" },
  { name: "CrewAI", logo: "https://raw.githubusercontent.com/ag-ui-protocol/ag-ui/main/.github/readme/logos/crewai.svg", alt: "CrewAI logo" },
  { name: "Microsoft Agent Framework", logo: "https://raw.githubusercontent.com/ag-ui-protocol/ag-ui/main/.github/readme/logos/microsoft-agent-framework.svg", alt: "Microsoft Agent Framework logo" },
  { name: "OpenAI Agents SDK", logo: "https://cdn.simpleicons.org/openai", alt: "OpenAI logo" },
  { name: "Google ADK", logo: "https://raw.githubusercontent.com/ag-ui-protocol/ag-ui/main/.github/readme/logos/google-adk.svg", alt: "Google ADK logo" },
];

export default function LandingPage() {
  const [activeStep, setActiveStep] = useState(-1);
  const [running, setRunning] = useState(false);

  async function runDemo() {
    if (running) return;
    setRunning(true);
    setActiveStep(-1);
    for (let i = 0; i < steps.length; i += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 720));
      setActiveStep(i);
    }
    setRunning(false);
  }

  return (
    <div className="cohort-site" id="top">
      <a className="cs-skip" href="#main">Skip to content</a>
      <header className="cs-nav">
        <div className="cs-nav-inner">
          <a className="cs-brand" href="#top" aria-label="Cohort home">
            <span className="cs-brand-mark"><img src={cohortLogo} alt="" /></span>
            <span>cohort</span><span className="cs-by">by Cerbère</span>
          </a>
          <nav className="cs-links" aria-label="Main navigation">
            <a href="#how">How it works</a>
            <a href="#capabilities">Capabilities</a>
            <a href="#integrations">Integrations</a>
            <a href="#pricing">Pricing</a>
          </nav>
          <div className="cs-nav-actions">
            <a className="cs-login" href="/app">Sign in</a>
            <a className="cs-button cs-button-small" href="/app">Get started <ArrowUpRight size={15} /></a>
          </div>
        </div>
      </header>

      <main id="main">
        <section className="cs-hero">
          <div className="cs-hero-grid" aria-hidden="true" />
          <div className="cs-hero-inner">
            <div className="cs-eyebrow"><span className="cs-live-dot" /> MULTI-AGENT COORDINATION INFRASTRUCTURE</div>
            <h1>Independent agents.<br /><span className="cs-highlight">One coordinated</span> system.</h1>
            <p className="cs-hero-copy">Give AI agents shared state, clear ownership, and safe ways to work together. Without silent overwrites or coordination guesswork.</p>
            <div className="cs-hero-actions">
              <a className="cs-button" href="/app">Start coordinating <ArrowRight size={17} /></a>
              <a className="cs-button cs-button-light" href="#how">See how it works <ArrowUpRight size={16} /></a>
            </div>
            <div className="cs-hero-note"><span className="cs-note-icon"><ShieldCheck size={15} /></span> Built for agents that share work, state, and responsibility.</div>
          </div>

          <div className="cs-demo-shell" id="how">
            <div className="cs-window-bar">
              <div className="cs-window-dots"><i /><i /><i /></div>
              <span className="cs-window-title">cohort / coordination trace</span>
              <span className="cs-window-status"><span /> {running ? "SIMULATING" : activeStep === 3 ? "STALE WRITE BLOCKED" : "SYSTEM READY"}</span>
            </div>
            <div className="cs-demo-top">
              <div><span className="cs-mono-label">SCENARIO 001</span><h2>A worker wakes up late.</h2><p>See what happens when a lease expires and another agent takes over.</p></div>
              <button className="cs-run-button" onClick={runDemo} disabled={running}><Zap size={15} /> {running ? "Running…" : "Run scenario"}</button>
            </div>
            <div className="cs-demo-content">
              <div className="cs-flow">
                <div className="cs-flow-lane">
                  <div className="cs-lane-label"><span className="cs-agent-dot violet" /> AGENT A</div>
                  <div className={`cs-event-card ${activeStep >= 0 ? "is-active" : ""} violet-card`}>
                    <div className="cs-event-head"><span>Lease acquired</span><span className="cs-event-time">09:41:02</span></div>
                    <strong>Task: reconcile-ledger</strong><small>owner: agent_a · fencing token: 41</small>
                    {activeStep >= 0 && <span className="cs-event-check"><Check size={13} /></span>}
                  </div>
                  <div className={`cs-event-card cs-stale-card ${activeStep >= 3 ? "is-blocked" : ""}`}>
                    <div className="cs-event-head"><span>Write attempt</span><span className="cs-event-time">09:41:19</span></div>
                    <strong>Update shared state</strong><small>{activeStep >= 3 ? "Rejected: stale fencing token 41" : "Waiting for worker to resume…"}</small>
                    {activeStep >= 3 && <span className="cs-blocked-label">REJECTED</span>}
                  </div>
                </div>
                <div className="cs-flow-connector"><span /><span /><span /></div>
                <div className="cs-flow-lane">
                  <div className="cs-lane-label"><span className="cs-agent-dot blue" /> AGENT B</div>
                  <div className={`cs-event-card blue-card ${activeStep >= 1 ? "is-active" : ""}`}>
                    <div className="cs-event-head"><span>State read</span><span className="cs-event-time">09:41:11</span></div>
                    <strong>Revision 12 loaded</strong><small>Snapshot pinned before execution</small>
                    {activeStep >= 1 && <span className="cs-event-check"><Check size={13} /></span>}
                  </div>
                  <div className={`cs-event-card mint-card ${activeStep >= 2 ? "is-active" : ""}`}>
                    <div className="cs-event-head"><span>Lease takeover</span><span className="cs-event-time">09:41:15</span></div>
                    <strong>Ownership transferred</strong><small>new fencing token: 42</small>
                    {activeStep >= 2 && <span className="cs-event-check"><Check size={13} /></span>}
                  </div>
                </div>
              </div>
              <aside className="cs-inspector">
                <div className="cs-inspector-title"><CircleDot size={14} /> EVENT LOG</div>
                {steps.map((step, index) => (
                  <div className={`cs-log-row ${activeStep >= index ? "is-visible" : ""} ${index === 3 && activeStep >= 3 ? "is-danger" : ""}`} key={step.task}>
                    <span className="cs-log-index">0{index + 1}</span>
                    <div><strong>{step.task}</strong><small>{step.detail}</small></div>
                    {activeStep >= index && <Check size={14} />}
                  </div>
                ))}
                <div className="cs-inspector-foot"><span className={activeStep >= 3 ? "cs-foot-danger" : ""} /> {activeStep >= 3 ? "No stale write committed" : "Waiting for scenario"}</div>
              </aside>
            </div>
            <div className="cs-demo-caption"><span>SIMULATED COORDINATION TRACE</span><span>Illustrative example · not live customer data</span></div>
          </div>
        </section>

        <section className="cs-proof-strip">
          <p>COORDINATION PRIMITIVES FOR REAL-WORLD AGENT SYSTEMS</p>
          <div><span><LockKeyhole size={16} /> Ownership</span><span><GitBranch size={16} /> Shared state</span><span><Clock3 size={16} /> Leases</span><span><ShieldCheck size={16} /> Fencing</span><span><Workflow size={16} /> Conflict handling</span></div>
        </section>

        <section className="cs-section cs-capabilities" id="capabilities">
          <div className="cs-section-heading">
            <span className="cs-section-kicker">THE COORDINATION LAYER</span>
            <h2>Agents can work in parallel.<br /><em>They still need shared rules.</em></h2>
            <p>Parallel execution is easy to start. Keeping agents from stepping on each other's work is the hard part. Cohort gives that shared work a coordination model.</p>
          </div>
          <div className="cs-cap-grid">
            {capabilities.map(({ icon: Icon, number, title, copy }) => (
              <article className="cs-cap-card" key={number}>
                <div className="cs-cap-top"><span>{number}</span><Icon size={21} strokeWidth={1.8} /></div>
                <h3>{title}</h3><p>{copy}</p><span className="cs-cap-arrow"><ArrowUpRight size={16} /></span>
              </article>
            ))}
          </div>
        </section>

        <section className="cs-quote-section">
          <div className="cs-quote-mark">“</div>
          <blockquote>A lease can expire. A worker can wake up late. <strong>Your shared state should still be correct.</strong></blockquote>
          <p>COHORT'S CORE PROMISE</p>
        </section>

        <section className="cs-section cs-dashboard-section" id="dashboard">
          <div className="cs-dashboard-copy">
            <span className="cs-section-kicker">THE COHORT WORKSPACE</span>
            <h2>See the work.<br /><em>Not just the output.</em></h2>
            <p>The Cohort dashboard brings branches, shared state, leases, event history, gateway activity and performance into one operational workspace.</p>
            <a className="cs-button" href="/app">Open the actual dashboard <ArrowUpRight size={16} /></a>
          </div>
          <div className="cs-dashboard-window" aria-label="Cohort dashboard sections">
            <div className="cs-dashboard-top"><img src={cohortLogo} alt="" /><strong>Cohort</strong><span>WORKSPACE</span><i /></div>
            <div className="cs-dashboard-body">
              <aside className="cs-dashboard-sidebar">
                <b>WORKSPACE</b><span className="selected">Overview</span><span>Branches</span><span>Shared state</span><span>Leases</span><span>Event log</span>
                <b>OPERATE</b><span>Gateway</span><span>Performance</span><span>Advisor</span><span>Settings</span>
              </aside>
              <div className="cs-dashboard-main">
                <div className="cs-dashboard-heading"><div><small>WORKSPACE / OVERVIEW</small><strong>Coordination overview</strong></div><span>Workspace view</span></div>
                <div className="cs-dashboard-cards"><div><small>SHARED STATE</small><b>Versioned</b><i>Snapshot-backed</i></div><div><small>OWNERSHIP</small><b>Leases</b><i>Time-bound claims</i></div><div><small>HISTORY</small><b>Event log</b><i>Traceable changes</i></div></div>
                <div className="cs-dashboard-panel"><div><strong>Coordination activity</strong><small>Inspect agent operations and state changes</small></div><div className="cs-dashboard-row"><i className="mint" /><span>Branch commits</span><small>branches</small></div><div className="cs-dashboard-row"><i className="violet" /><span>Lease activity</span><small>leases</small></div><div className="cs-dashboard-row"><i className="blue" /><span>State snapshots</span><small>shared state</small></div></div>
                <div className="cs-dashboard-note">Interface preview · Open the dashboard for live workspace data.</div>
              </div>
            </div>
          </div>
        </section>

        <section className="cs-section cs-frameworks" id="frameworks">
          <div className="cs-section-heading">
            <span className="cs-section-kicker">BUILT AROUND YOUR STACK</span>
            <h2>Keep your framework.<br /><em>Add coordination.</em></h2>
            <p>Connect through Cohort’s Python library, REST API or MCP server. Keep your agent runtime and add a shared coordination layer.</p>
          </div>
          <div className="cs-framework-grid">
            {frameworks.map((framework) => <div className="cs-framework-card" key={framework.name}>
              <span className="cs-framework-logo"><img src={framework.logo} alt={framework.alt} loading="lazy" /></span>
              <strong>{framework.name}</strong><ArrowUpRight size={14} />
            </div>)}
          </div>
          <p className="cs-framework-note">Connection is through shared interfaces (Python, REST and MCP); dedicated native adapters are not implied.</p>
        </section>

        <section className="cs-section cs-integrations" id="integrations">
          <div className="cs-integration-copy">
            <span className="cs-section-kicker">FITS YOUR STACK</span>
            <h2>Keep your tools.<br /><em>Connect the signals.</em></h2>
            <p>Send coordination events to the observability and security systems your team already operates.</p>
            <a className="cs-text-link" href="https://github.com/chrismsmr-celcom/cerbere-cohort">Explore the project <ArrowUpRight size={15} /></a>
          </div>
          <div className="cs-integration-board">
            <div className="cs-board-core"><Layers3 size={25} /><strong>Cohort</strong><small>coordination events</small></div>
            <div className="cs-board-lines" aria-hidden="true"><i /><i /><i /><i /><i /><i /></div>
            <div className="cs-integration-list">{integrations.map((name, i) => <div className="cs-integration-item" key={name}><span className={`cs-integration-icon i${i}`}>{i === 0 ? "OT" : i === 1 ? "S" : i === 2 ? "D" : i === 3 ? "M" : i === 4 ? "E" : "M"}</span><strong>{name}</strong><ArrowUpRight size={13} /></div>)}</div>
          </div>
        </section>

        <section className="cs-pricing-section" id="pricing">
          <div className="cs-pricing-heading">
            <span className="cs-section-kicker">SIMPLE, WHILE WE BUILD</span>
            <h2>Start free.<br /><em>Build with Cohort.</em></h2>
            <p>The current public beta is free to use. Paid plans may be introduced later as the product matures.</p>
          </div>
          <article className="cs-price-card">
            <div className="cs-price-top"><span>PUBLIC BETA</span><span className="cs-price-status"><i /> Available now</span></div>
            <div className="cs-price-value">$0 <small>/ currently free</small></div>
            <p>Access the current Cohort experience while we develop the platform.</p>
            <ul><li><Check size={15} /> Cohort dashboard</li><li><Check size={15} /> Shared state and branches</li><li><Check size={15} /> Leases and coordination history</li><li><Check size={15} /> Python, REST and MCP interfaces</li></ul>
            <a className="cs-button" href="/app">Get started <ArrowRight size={16} /></a>
            <small className="cs-price-foot">No payment details. No subscription checkout is available today.</small>
          </article>
        </section>

        <section className="cs-final-cta">
          <div className="cs-final-grid" aria-hidden="true" />
          <span className="cs-section-kicker">MAKE MULTI-AGENT WORK RELIABLE</span>
          <h2>Coordinate the work.<br /><em>Keep control of the state.</em></h2>
          <p>Start with the coordination primitives. Add agents when you're ready.</p>
          <a className="cs-button cs-button-cream" href="/app">Get started with Cohort <ArrowRight size={17} /></a>
          <span className="cs-final-foot">OPEN SOURCE CORE <i /> BUILT BY CERBÈRE</span>
        </section>
      </main>

      <footer className="cs-footer" id="footer">
        <div className="cs-footer-top">
          <div className="cs-footer-brand">
            <a className="cs-footer-mark" href="#top" aria-label="Back to Cohort home"><img src={cohortLogo} alt="Cohort" /></a>
            <div><strong>cohort</strong><p>Coordination infrastructure for AI agents.</p><a className="cs-footer-parent" href="https://www.cerbereag.site" target="_blank" rel="noreferrer">Part of Cerbère AG <ArrowUpRight size={13} /></a></div>
          </div>
          <div className="cs-footer-column"><strong>PRODUCT</strong><a href="#capabilities">Capabilities</a><a href="#frameworks">Frameworks</a><a href="#integrations">Integrations</a><a href="#pricing">Pricing</a><a href="/app">Dashboard</a></div>
          <div className="cs-footer-column"><strong>RESOURCES</strong><a href="https://github.com/chrismsmr-celcom/cerbere-cohort" target="_blank" rel="noreferrer">GitHub <ArrowUpRight size={12} /></a><a href="https://github.com/chrismsmr-celcom/cerbere-cohort/blob/main/CONTRIBUTING.md" target="_blank" rel="noreferrer">Contributing <ArrowUpRight size={12} /></a><a href="https://github.com/chrismsmr-celcom/cerbere-cohort/blob/main/README.md" target="_blank" rel="noreferrer">Documentation <ArrowUpRight size={12} /></a><a href="/blog">Blog <ArrowUpRight size={12} /></a></div>
          <div className="cs-footer-column"><strong>COMPANY & LEGAL</strong><a href="https://www.cerbereag.site" target="_blank" rel="noreferrer">Cerbère AG <ArrowUpRight size={12} /></a><a href="mailto:christopher-ag@cerbereag.site">Contact</a><a href="/terms">Terms</a><a href="/privacy">Privacy policy</a><a href="/privacy#confidentiality">Confidentiality</a></div>
        </div>
        <div className="cs-footer-bottom"><span>© {new Date().getFullYear()} Cerbère AG. All rights reserved.</span><span>Currently free during public beta.</span><a href="mailto:christopher-ag@cerbereag.site">christopher-ag@cerbereag.site</a></div>
      </footer>
    </div>
  );
}
