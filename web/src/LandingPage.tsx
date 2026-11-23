import { useEffect, useRef, useState, type CSSProperties, type PointerEvent } from "react";
import {
  ArrowRight, ArrowUpRight, Check, CircleDot, Clock3, GitBranch,
  GitCommitHorizontal, GitMerge, Github, LockKeyhole, Mail,
  Menu, Radio, ShieldCheck, Workflow, X, Zap,
} from "lucide-react";
import cohortLogo from "./cohortLogo";
import { crewaiLogo, langchainLogo, openaiLogo } from "./frameworkLogos";
import "./landing.css";

const steps = [
  { task: "Claim shared task", detail: "lease acquired · token 41" },
  { task: "Read current state", detail: "revision 12 · snapshot pinned" },
  { task: "Take over expired lease", detail: "lease renewed · token 42" },
  { task: "Attempt stale write", detail: "rejected · fencing token 41" },
];

const capabilities = [
  { icon: LockKeyhole, number: "01", title: "Explicit ownership", copy: "Leases make it clear which agent owns a task, and when that ownership expires." },
  { icon: GitCommitHorizontal, number: "02", title: "Fenced writes", copy: "Every ownership change advances a fencing token. Stale workers cannot overwrite newer work." },
  { icon: GitMerge, number: "03", title: "Conflict-aware state", copy: "Agents work against versioned shared state, with conflicts surfaced instead of silently lost." },
  { icon: Radio, number: "04", title: "Observable execution", copy: "Export coordination events and traces to the monitoring stack your team already uses." },
];

const frameworks = [
  { name: "LangChain", note: "LangGraph · graph-based agent workflows", logo: langchainLogo, href: "https://www.langchain.com/langgraph" },
  { name: "CrewAI", note: "Role-based agent teams", logo: crewaiLogo, href: "https://www.crewai.com/" },
  { name: "OpenAI Agents SDK", note: "Agent orchestration SDK", logo: openaiLogo, href: "https://openai.github.io/openai-agents-python/" },
];

const destinations = ["OpenTelemetry · OTLP/HTTP", "Splunk · HEC", "Datadog", "Microsoft Sentinel", "Elasticsearch"];

export default function LandingPage() {
  const [activeStep, setActiveStep] = useState(-1);
  const [running, setRunning] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    root.classList.add("is-ready");
    const io = new IntersectionObserver((entries) => entries.forEach((e) => {
      if (e.isIntersecting) { e.target.classList.add("is-in"); io.unobserve(e.target); }
    }), { threshold: 0.15 });
    root.querySelectorAll("[data-reveal]").forEach((el) => (reduce ? el.classList.add("is-in") : io.observe(el)));
    const demo = root.querySelector(".cs-demo-shell");
    const dio = new IntersectionObserver(([e]) => {
      if (e.isIntersecting) { dio.disconnect(); if (!reduce) runDemo(); }
    }, { threshold: 0.45 });
    if (demo) dio.observe(demo);
    let raf = 0;
    const onScroll = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const h = document.documentElement;
        root.style.setProperty("--scroll", String(h.scrollTop / Math.max(1, h.scrollHeight - h.clientHeight)));
      });
    };
    const onKey = (ev: KeyboardEvent) => ev.key === "Escape" && setMenuOpen(false);
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("keydown", onKey);
    return () => { io.disconnect(); dio.disconnect(); cancelAnimationFrame(raf); window.removeEventListener("scroll", onScroll); window.removeEventListener("keydown", onKey); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onMonumentMove(e: PointerEvent<HTMLAnchorElement>) {
    e.currentTarget.querySelectorAll<HTMLElement>("b").forEach((b) => {
      const r = b.getBoundingClientRect();
      const d = Math.abs(e.clientX - (r.left + r.width / 2)) / (r.width * 1.6);
      b.style.setProperty("--p", String(Math.max(0, 1 - d)));
    });
  }
  function onMonumentLeave(e: PointerEvent<HTMLAnchorElement>) {
    e.currentTarget.querySelectorAll<HTMLElement>("b").forEach((b) => b.style.setProperty("--p", "0"));
  }

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
    <div className="cohort-site" id="top" ref={rootRef}>
      <a className="cs-skip" href="#main">Skip to content</a>
      <header className="cs-nav">
        <div className="cs-nav-inner">
          <a className="cs-brand" href="#top" aria-label="Cohort home">
            <img className="cs-brand-logo" src={cohortLogo} alt="" />
            <span>cohort</span><span className="cs-by">by Cerbère</span>
          </a>
          <nav className="cs-links" aria-label="Main navigation">
            <a href="#how">How it works</a>
            <a href="#capabilities">Capabilities</a>
            <a href="#frameworks">Frameworks</a>
            <a href="#pricing">Pricing</a>
          </nav>
          <div className="cs-nav-actions">
            <a className="cs-login" href="/app">Sign in</a>
            <a className="cs-button cs-button-small" href="/app">Get started <ArrowUpRight size={15} /></a>
            <button className="cs-burger" type="button" aria-label={menuOpen ? "Close menu" : "Open menu"} aria-expanded={menuOpen} aria-controls="cs-mobile-menu" onClick={() => setMenuOpen((v) => !v)}>{menuOpen ? <X size={20} /> : <Menu size={20} />}</button>
          </div>
        </div>
        <nav id="cs-mobile-menu" className={`cs-mobile-menu ${menuOpen ? "is-open" : ""}`} aria-label="Mobile navigation" hidden={!menuOpen}>
          {[["#how", "How it works"], ["#capabilities", "Capabilities"], ["#frameworks", "Frameworks"], ["#pricing", "Pricing"]].map(([h, l]) => <a key={h} href={h} onClick={() => setMenuOpen(false)}>{l}</a>)}
          <a href="/app" onClick={() => setMenuOpen(false)}>Sign in</a>
        </nav>
        <span className="cs-progress" aria-hidden="true" />
      </header>

      <main id="main">
        <section className="cs-hero">
          <div className="cs-hero-grid" aria-hidden="true" />
          <div className="cs-hero-inner">
            <div className="cs-eyebrow"><span className="cs-live-dot" /> MULTI-AGENT COORDINATION INFRASTRUCTURE</div>
            <h1><span className="cs-line">Independent agents.</span><span className="cs-line"><span className="cs-highlight">One coordinated</span> system.</span></h1>
            <p className="cs-hero-copy">Give AI agents shared state, clear ownership, and safe ways to work together. Without silent overwrites or coordination guesswork.</p>
            <div className="cs-hero-actions">
              <a className="cs-button" href="/app">Start coordinating <ArrowRight size={17} /></a>
              <a className="cs-button cs-button-light" href="#how">See how it works <ArrowUpRight size={16} /></a>
            </div>
            <div className="cs-hero-note"><span className="cs-note-icon"><ShieldCheck size={15} /></span> Built for agents that share work, state, and responsibility.</div>
          </div>

          <div className={`cs-demo-shell ${running ? "is-running" : ""}`} id="how">
            <div className="cs-preview-badge"><span /> INTERACTIVE PREVIEW · NOT THE LIVE DASHBOARD</div>
            <div className="cs-window-bar">
              <div className="cs-window-dots"><i /><i /><i /></div>
              <span className="cs-window-title">cohort / coordination trace</span>
              <span className="cs-window-status" role="status" aria-live="polite"><span /> {running ? "SIMULATING" : activeStep === 3 ? "STALE WRITE BLOCKED" : "READY TO SIMULATE"}</span>
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
          <div className="cs-marquee"><div className="cs-marquee-track"><span><LockKeyhole size={16} /> Ownership</span><span><GitBranch size={16} /> Shared state</span><span><Clock3 size={16} /> Leases</span><span><ShieldCheck size={16} /> Fencing</span><span><Workflow size={16} /> Conflict handling</span><span className="cs-marquee-copy" aria-hidden="true"><em><LockKeyhole size={16} /> Ownership</em><em><GitBranch size={16} /> Shared state</em><em><Clock3 size={16} /> Leases</em><em><ShieldCheck size={16} /> Fencing</em><em><Workflow size={16} /> Conflict handling</em></span></div></div>
        </section>

        <section className="cs-section cs-capabilities" id="capabilities">
          <div className="cs-section-heading" data-reveal>
            <span className="cs-section-kicker">THE COORDINATION LAYER</span>
            <h2>Agents can work in parallel.<br /><em>They still need shared rules.</em></h2>
            <p>Parallel execution is easy to start. Keeping agents from stepping on each other's work is the hard part. Cohort gives that shared work a coordination model.</p>
          </div>
          <div className="cs-cap-grid">
            {capabilities.map(({ icon: Icon, number, title, copy }) => (
              <article className="cs-cap-card" data-reveal key={number}>
                <div className="cs-cap-top"><span>{number}</span><Icon size={21} strokeWidth={1.8} /></div>
                <h3>{title}</h3><p>{copy}</p><span className="cs-cap-arrow"><ArrowUpRight size={16} /></span>
              </article>
            ))}
          </div>
        </section>

        <section className="cs-quote-section" data-reveal>
          <div className="cs-quote-mark">“</div>
          <blockquote>A lease can expire. A worker can wake up late. <strong>Your shared state should still be correct.</strong></blockquote>
          <p>COHORT'S CORE PROMISE</p>
        </section>

        <section className="cs-section cs-frameworks" id="frameworks">
          <div className="cs-section-heading" data-reveal>
            <span className="cs-section-kicker">BUILT TO FIT YOUR AGENT STACK</span>
            <h2>Keep your framework.<br /><em>Add coordination.</em></h2>
            <p>Cohort is a coordination layer, not another agent framework. Connect your agents through the Python SDK, REST API, or MCP instead of replacing the orchestration stack you already use.</p>
          </div>
          <div className="cs-framework-grid">
            {frameworks.map((framework) => (
              <a className="cs-framework-card" data-reveal href={framework.href} target="_blank" rel="noreferrer" key={framework.name}>
                <div className="cs-framework-logo"><img src={framework.logo} alt={framework.name} loading="lazy" /></div>
                <span>{framework.note}</span>
                <ArrowUpRight size={16} />
              </a>
            ))}
          </div>
          <div className="cs-frameworks-secondary">
            <span>ALSO SUPPORTED</span>
            <a href="https://github.com/microsoft/autogen" target="_blank" rel="noreferrer">AutoGen <small>· archived by Microsoft</small> <ArrowUpRight size={12} /></a>
          </div>
          <p className="cs-framework-footnote">Framework logos are trademarks of their respective owners. Connections are made through Cohort's public interfaces; these are not vendor-certified integrations.</p>
          <div className="cs-connection-methods"><span>CONNECT THROUGH</span><strong>Python SDK</strong><i /><strong>REST API</strong><i /><strong>MCP</strong></div>
          <div className="cs-destinations">
            <span>OBSERVABILITY DESTINATIONS</span>
            {destinations.map((destination) => <i key={destination}>{destination}</i>)}
          </div>
        </section>

        <section className="cs-pricing" id="pricing">
          <div className="cs-pricing-inner" data-reveal>
            <div>
              <span className="cs-section-kicker">CURRENT ACCESS</span>
              <h2>Free while we're<br /><em>building the beta.</em></h2>
              <p>The current Cohort version is free to use. Paid plans and subscription checkout are not available yet.</p>
            </div>
            <div className="cs-price-card">
              <span className="cs-price-label">PUBLIC BETA</span>
              <div className="cs-price">$0 <small>/ currently</small></div>
              <ul><li><Check size={15} /> Access to the current version</li><li><Check size={15} /> Open-source core under MIT</li><li><Check size={15} /> No subscription or checkout</li></ul>
              <a className="cs-button" href="/app">Try Cohort <ArrowRight size={16} /></a>
            </div>
          </div>
        </section>

        <section className="cs-blog" data-reveal id="blog">
          <div><span className="cs-section-kicker">FROM THE BUILD LOG</span><h2>Notes from the coordination layer.</h2><p>Engineering articles and product updates are being prepared.</p></div>
          <a className="cs-text-link" href="https://github.com/chrismsmr-celcom/cerbere-cohort/commits/main/" target="_blank" rel="noreferrer">Follow repository updates <ArrowUpRight size={15} /></a>
        </section>

        <section className="cs-final-cta" data-reveal>
          <div className="cs-final-grid" aria-hidden="true" />
          <span className="cs-section-kicker">MAKE MULTI-AGENT WORK RELIABLE</span>
          <h2>Coordinate the work.<br /><em>Keep control of the state.</em></h2>
          <p>Start with the coordination primitives. Add agents when you're ready.</p>
          <a className="cs-button cs-button-cream" href="/app">Get started with Cohort <ArrowRight size={17} /></a>
          <span className="cs-final-foot">OPEN SOURCE CORE <i /> BUILT BY CERBÈRE</span>
        </section>
      </main>

      <footer className="cs-footer cf">
        <div className="cf-inner">
          <div className="cf-brand-col">
            <a className="cf-brand" href="#top" aria-label="Back to Cohort home"><img src={cohortLogo} alt="" /><span>cohort</span></a>
            <p>Coordination infrastructure for AI agents. Independent agents, shared rules, reliable work.</p>
            <a className="cs-button cs-button-small" href="/app">Get started <ArrowUpRight size={15} /></a>
          </div>
          <nav className="cf-cols" aria-label="Footer">
          <div className="cf-col"><strong>Product</strong><a href="#capabilities">Capabilities</a><a href="#frameworks">Frameworks</a><a href="#pricing">Pricing</a><a href="/app">Dashboard</a></div>
          <div className="cf-col"><strong>Resources</strong><a href="https://github.com/chrismsmr-celcom/cerbere-cohort" target="_blank" rel="noreferrer">GitHub</a><a href="https://github.com/chrismsmr-celcom/cerbere-cohort/blob/main/CONTRIBUTING.md" target="_blank" rel="noreferrer">Contributing</a><a href="#blog">Blog & updates</a><a href="https://www.cerbereag.site" target="_blank" rel="noreferrer">Cerbere AG</a></div>
          <div className="cf-col"><strong>Legal</strong><a href="https://github.com/chrismsmr-celcom/cerbere-cohort/blob/main/docs/TERMS.md" target="_blank" rel="noreferrer">Terms of use <span className="cs-draft-tag">DRAFT</span></a><a href="https://github.com/chrismsmr-celcom/cerbere-cohort/blob/main/docs/PRIVACY.md" target="_blank" rel="noreferrer">Privacy policy <span className="cs-draft-tag">DRAFT</span></a><a href="https://github.com/chrismsmr-celcom/cerbere-cohort/blob/main/LICENSE" target="_blank" rel="noreferrer">MIT license</a></div>
          <div className="cf-col"><strong>Contact</strong><a href="mailto:christopher-ag@cerbereag.site"><Mail size={14} /> christopher-ag@cerbereag.site</a><a href="https://github.com/chrismsmr-celcom/cerbere-cohort" target="_blank" rel="noreferrer"><Github size={14} /> Open-source project</a></div>
          </nav>
        </div>
        <div className="cf-bottom">
          <span>© {new Date().getFullYear()} Cohort by Cerbère.</span>
          <span className="cf-status"><i /> Public beta · open-source core (MIT)</span>
          <a href="#top">Back to top ↑</a>
        </div>
        <a className="cf-monument" href="#top" aria-label="Cohort — back to top" data-reveal onPointerMove={onMonumentMove} onPointerLeave={onMonumentLeave}>
          <span aria-hidden="true">{"Cohort".split("").map((c, i) => <b key={i} style={{ "--i": i } as CSSProperties}>{c}</b>)}</span>
        </a>
      </footer>
    </div>
  );
}
