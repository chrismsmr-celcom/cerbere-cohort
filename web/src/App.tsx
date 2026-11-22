import { useCallback, useEffect, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import type { Session } from "@supabase/supabase-js";
import Dashboard from "./Dashboard";
import "./styles/app.css";
import cohortLogo from "./cohortLogo";
import { GitHubLogo, GitLabLogo, GoogleLogo, SsoLogo } from "./components/ProviderLogos";
import { authConfigured, authEnvironmentPresent, getActiveWorkspaceId, setActiveWorkspaceId, supabase, type CohortWorkspace } from "./auth";
import { api } from "./lib/api";

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState(false);
  const [authMode, setAuthMode] = useState<"signin" | "signup">("signin");
  const [message, setMessage] = useState<{ text: string; tone: "info" | "error" } | null>(null);
  const [workspaces, setWorkspaces] = useState<CohortWorkspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState(getActiveWorkspaceId);
  const [workspaceLoading, setWorkspaceLoading] = useState(false);
  const apiConfigured = Boolean((import.meta as any).env?.VITE_API_BASE_URL || (import.meta as any).env?.VITE_API_URL);

  useEffect(() => {
    if (!supabase) { setReady(true); return; }
    void supabase.auth.getSession().then(({ data }) => { setSession(data.session); setReady(true); });
    const { data } = supabase.auth.onAuthStateChange((_event, next) => { setSession(next); setReady(true); });
    return () => data.subscription.unsubscribe();
  }, []);

  const loadWorkspaces = useCallback(async () => {
    if (!session) return;
    setWorkspaceLoading(true); setMessage(null);
    try {
      const result = await api<{ items: CohortWorkspace[] }>("/workspaces");
      const items = result.items || [];
      setWorkspaces(items);
      const saved = getActiveWorkspaceId();
      const selected = items.some((w) => w.id === saved) ? saved : items[0]?.id || "";
      setWorkspaceId(selected);
      setActiveWorkspaceId(selected);
    } catch (err) {
      setWorkspaces([]); setWorkspaceId(""); setActiveWorkspaceId("");
      setMessage({ tone: "error", text: err instanceof Error ? err.message : "Unable to load workspace memberships." });
    } finally { setWorkspaceLoading(false); }
  }, [session]);

  useEffect(() => { if (session) void loadWorkspaces(); else { setWorkspaces([]); setWorkspaceId(""); } }, [session, loadWorkspaces]);

  async function completeOnboarding(payload: OnboardingPayload) {
    if (!supabase || !session) return;
    setBusy(true); setMessage(null);
    try {
      const { error } = await supabase.auth.updateUser({
        data: { full_name: payload.full_name, name: payload.full_name },
      });
      if (error) throw error;
      await api("/onboarding", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      await loadWorkspaces();
    } catch (err) {
      setMessage({ tone: "error", text: err instanceof Error ? err.message : "Unable to create your workspace." });
    } finally {
      setBusy(false);
    }
  }

  // Local development remains passwordless. Hosted deployments never fall back
  // to an unauthenticated dashboard.
  if (!authEnvironmentPresent && !apiConfigured) return <Dashboard />;
  if (!authConfigured || !supabase) {
    return <AuthScreen title="Configuration required" message={{ tone: "error", text: "Set VITE_SUPABASE_URL and VITE_SUPABASE_ANON_KEY, then redeploy. No global organization ID is required." }} />;
  }
  if (!ready) return <AuthScreen title="Loading your account…" />;
  if (!session) {
    async function sendMagicLink(event: FormEvent) {
      event.preventDefault();
      if (!supabase) return;
      setBusy(true); setMessage(null);
      const { error } = await supabase.auth.signInWithOtp({
        email: email.trim(),
        options: { emailRedirectTo: `${window.location.origin}/app` },
      });
      if (error) setMessage({ tone: "error", text: error.message });
      else { setSent(true); setMessage({ tone: "info", text: `We sent a secure link to ${email.trim()}. Check your inbox.` }); }
      setBusy(false);
    }
    async function signInWithProvider(provider: "google" | "github" | "gitlab") {
      if (!supabase) return;
      setBusy(true); setMessage(null);
      const { error } = await supabase.auth.signInWithOAuth({ provider, options: { redirectTo: `${window.location.origin}/app` } });
      if (error) { setMessage({ tone: "error", text: error.message }); setBusy(false); }
    }
    return <SplitAuthScreen mode={authMode} email={email} busy={busy} sent={sent} message={message}
      onEmailChange={(value) => { setEmail(value); setSent(false); }}
      onSubmit={sendMagicLink}
      onProvider={(provider) => void signInWithProvider(provider)}
      onModeChange={(mode) => { setAuthMode(mode); setMessage(null); setSent(false); }}
    />;
  }

  if (workspaceLoading) return <AuthScreen title="Loading your workspaces…" />;
  if (message?.tone === "error" && workspaces.length === 0) {
    return <AuthScreen title="Unable to load workspaces" message={message}>
      <button className="btn btn-primary btn-lg btn-block" onClick={() => void loadWorkspaces()}>Retry</button>
      <button className="btn btn-secondary btn-lg btn-block" onClick={() => void supabase?.auth.signOut()}>Sign out</button>
    </AuthScreen>;
  }
  const needsOnboarding = workspaces.length === 0
    || (workspaces[0]?.role === "owner" && workspaces[0]?.onboarding_completed === false);
  if (needsOnboarding) return <OnboardingScreen
    initialName={String(session.user.user_metadata?.full_name || session.user.user_metadata?.name || "")}
    email={session.user.email ?? ""}
    busy={busy}
    message={message}
    onSubmit={completeOnboarding}
    onSignOut={() => void supabase?.auth.signOut()}
  />;

  const activeWorkspace = workspaces.find((w) => w.id === workspaceId) || workspaces[0];
  return <Dashboard
    key={activeWorkspace.id}
    userEmail={session.user.email ?? undefined}
    workspaces={workspaces}
    activeWorkspace={activeWorkspace}
    onWorkspaceChange={(id) => { setWorkspaceId(id); setActiveWorkspaceId(id); }}
    onWorkspaceProfileChange={({ id, name, logo_url }) => setWorkspaces((current) => current.map((workspace) => workspace.id === id ? { ...workspace, name, logo_url } : workspace))}
    onSignOut={() => { setActiveWorkspaceId(""); void supabase?.auth.signOut(); }}
  />;
}

function AuthScreen({ title, subtitle, message, children }: { title: string; subtitle?: string; message?: { text: string; tone: "info" | "error" } | null; children?: ReactNode }) {
  return (
    <main className="auth-page">
      <div className="app-bg" aria-hidden /><div className="bg-noise" aria-hidden />
      <section className="auth-card">
        <div className="row"><img src={cohortLogo} alt="" width={32} height={32} /><strong style={{ fontSize: 18, color: "var(--ink)" }}>Cohort</strong><span className="tag-beta">Beta</span></div>
        <div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>
        {children}
        {message && <p role={message.tone === "error" ? "alert" : "status"} className={`notice ${message.tone === "error" ? "err" : ""}`}>{message.text}</p>}
      </section>
      <small className="muted" style={{ position: "relative", zIndex: 1 }}>Cohort by Cerbère · Coordination for AI agents</small>
    </main>
  );
}



type AuthMode = "signin" | "signup";
type OAuthProvider = "google" | "github" | "gitlab";

function SplitAuthScreen({ mode, email, busy, sent, message, onEmailChange, onSubmit, onProvider, onModeChange }: {
  mode: AuthMode; email: string; busy: boolean; sent: boolean;
  message: { text: string; tone: "info" | "error" } | null;
  onEmailChange: (value: string) => void; onSubmit: (event: FormEvent) => void;
  onProvider: (provider: OAuthProvider) => void; onModeChange: (mode: AuthMode) => void;
}) {
  const isSignup = mode === "signup";
  return (
    <main className="split-auth-page">
      <section className="split-auth-form"><div className="split-auth-inner">
        <a className="auth-brand" href="/" aria-label="Cohort home">
          <img src={cohortLogo} alt="" width={34} height={34} /><span>Cohort</span><span className="tag-beta">Beta</span>
        </a>
        <div className="auth-heading">
          <p className="auth-eyebrow">{isSignup ? "GET STARTED" : "WELCOME BACK"}</p>
          <h1>{isSignup ? "Create your account" : "Sign in to Cohort"}</h1>
          <p>{isSignup ? "Start coordinating your AI agents." : "Access your workspace and keep your agents in sync."}</p>
        </div>
        <form onSubmit={onSubmit} className="split-auth-stack">
          <label className="field">Work email
            <input type="email" required autoComplete="email" autoFocus value={email}
              onChange={(event) => onEmailChange(event.target.value)} placeholder="you@company.com" />
          </label>
          <button className="btn btn-primary btn-lg btn-block auth-email-submit" disabled={busy || !email.trim()}>
            {busy ? "Please wait…" : sent ? "Resend secure link" : "Continue with email"}<span aria-hidden="true">→</span>
          </button>
        </form>
        <div className="auth-divider"><span>OR CONTINUE WITH</span></div>
        <div className="auth-provider-list">
          <button type="button" className="auth-provider" aria-label="Continue with Google" disabled={busy} onClick={() => onProvider("google")}><GoogleLogo className="provider-logo" /><span>Google</span></button>
          <button type="button" className="auth-provider" aria-label="Continue with GitHub" disabled={busy} onClick={() => onProvider("github")}><GitHubLogo className="provider-logo" /><span>GitHub</span></button>
          <button type="button" className="auth-provider" aria-label="Continue with GitLab" disabled={busy} onClick={() => onProvider("gitlab")}><GitLabLogo className="provider-logo" /><span>GitLab</span></button>
          <button type="button" className="auth-provider auth-provider-disabled" aria-label="Continue with SSO (coming soon)" disabled aria-disabled="true" title="Azure SSO will be available soon">
            <SsoLogo className="provider-logo" /><span>SSO</span><span className="coming-soon">Soon</span>
          </button>
        </div>
        {message && <p role={message.tone === "error" ? "alert" : "status"} className={`auth-feedback ${message.tone}`}>{message.text}</p>}
        <p className="auth-switch">{isSignup ? "Already have an account?" : "Don't have an account?"}{" "}
          <button type="button" onClick={() => onModeChange(isSignup ? "signin" : "signup")}>{isSignup ? "Sign in" : "Sign up"}</button>
        </p>
        <p className="auth-legal">By continuing, you agree to Cohort’s Terms of Service and Privacy Policy.</p>
      </div></section>
      <aside className="auth-art-panel" aria-label="How Cohort coordinates agents">
        <div className="auth-art-copy">
          <a className="auth-art-brand" href="/" aria-label="Cohort home">
            <img src={cohortLogo} alt="" width="30" height="30" />
            <span>cohort</span><small>by Cerbère</small>
          </a>
          <div className="auth-art-kicker"><span /> MULTI-AGENT COORDINATION INFRASTRUCTURE</div>
          <h2>Independent agents.<br /><em>One coordinated</em> system.</h2>
          <p>Shared state. Clear ownership. Reliable execution.</p>
        </div>
        <div className="coord-visual" aria-label="Agent A and Agent B coordinate through shared state">
          <div className="coord-agents">
            <div className="coord-agent coord-agent-a">
              <span className="coord-agent-label">WORKER 01</span>
              <strong>Agent A</strong>
              <small>Lease expired</small>
              <span className="coord-token">TOKEN 41</span>
            </div>
            <div className="coord-link"><span /><span /><span /></div>
            <div className="coord-agent coord-agent-b">
              <span className="coord-agent-label">WORKER 02</span>
              <strong>Agent B</strong>
              <small>Owns current task</small>
              <span className="coord-token">TOKEN 42</span>
            </div>
          </div>
          <div className="coord-state">
            <div className="coord-state-head"><span className="coord-state-dot" /> SHARED STATE <span className="coord-revision">REVISION 12</span></div>
            <div className="coord-state-row"><span>Ownership transferred</span><strong>Confirmed</strong></div>
            <div className="coord-state-row coord-rejected"><span>Stale write · token 41</span><strong>Rejected</strong></div>
          </div>
          <p className="coord-caption">FENCING TOKENS PREVENT STALE WORKERS FROM OVERWRITING NEWER STATE</p>
        </div>
        <div className="auth-art-footer"><span>COHORT BY CERBÈRE</span><span>BUILD WITH CONTROL.</span></div>
      </aside>
    </main>
  );
}

type OnboardingPayload = {
  full_name: string;
  job_title: string;
  account_type: "individual" | "team" | "enterprise";
  workspace_name: string;
  company_size: "1-10" | "10-100" | "100+" | null;
  industry: string;
  primary_use_case: "personal_projects" | "internal_automation" | "production_agents" | "customer_facing_agents" | "research";
  expected_agents: "1-5" | "6-20" | "21-100" | "100+";
  frameworks: string[];
};

function OnboardingScreen({ initialName, email, busy, message, onSubmit, onSignOut }: {
  initialName: string;
  email: string;
  busy: boolean;
  message: { text: string; tone: "info" | "error" } | null;
  onSubmit: (payload: OnboardingPayload) => Promise<void>;
  onSignOut: () => void;
}) {
  const [fullName, setFullName] = useState(initialName);
  const [jobTitle, setJobTitle] = useState("");
  const [accountType, setAccountType] = useState<OnboardingPayload["account_type"]>("individual");
  const [workspaceName, setWorkspaceName] = useState("");
  const [companySize, setCompanySize] = useState<"" | "1-10" | "10-100" | "100+">("");
  const [industry, setIndustry] = useState("Software / Technology");
  const [useCase, setUseCase] = useState<OnboardingPayload["primary_use_case"]>("production_agents");
  const [expectedAgents, setExpectedAgents] = useState<OnboardingPayload["expected_agents"]>("1-5");
  const [frameworks, setFrameworks] = useState<string[]>([]);
  const frameworkOptions = [
    ["langchain", "LangChain"], ["crewai", "CrewAI"], ["autogen", "AutoGen"],
    ["openai_agents", "OpenAI Agents SDK"], ["custom", "Custom agents"], ["other", "Other"],
  ];
  function toggleFramework(value: string) {
    setFrameworks((current) => current.includes(value)
      ? current.filter((item) => item !== value)
      : [...current, value]);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    void onSubmit({
      full_name: fullName.trim(), job_title: jobTitle.trim(),
      account_type: accountType, workspace_name: workspaceName.trim(),
      company_size: accountType === "individual" ? null : companySize || null,
      industry: industry.trim(), primary_use_case: useCase,
      expected_agents: expectedAgents, frameworks,
    });
  }
  return (
    <main className="auth-page onboarding-page">
      <div className="app-bg" aria-hidden /><div className="bg-noise" aria-hidden />
      <section className="auth-card onboarding-card">
        <div className="row"><img src={cohortLogo} alt="" width={32} height={32} /><strong style={{ fontSize: 18, color: "var(--ink)" }}>Cohort</strong><span className="tag-beta">Beta</span></div>
        <div><p className="onboarding-step">SET UP YOUR WORKSPACE · 1/1</p><h1>Let’s set up your Cohort</h1><p>Tell us a little about you and how you plan to coordinate your agents.</p></div>
        <form onSubmit={submit} className="stack onboarding-form">
          <div className="onboarding-section">
            <h2>How will you use Cohort?</h2>
            <div className="account-types">
              {([
                ["individual", "Individual", "Just me", "user-round"],
                ["team", "Team", "A team or startup", "users"],
                ["enterprise", "Enterprise", "A larger organization", "building-2"],
              ] as const).map(([value, label, description]) => (
                <button type="button" key={value} className={`account-type ${accountType === value ? "selected" : ""}`} aria-pressed={accountType === value} onClick={() => setAccountType(value)}>
                  <strong>{label}</strong><span>{description}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="onboarding-section">
            <h2>About you</h2>
            <label className="field">Full name<input required maxLength={120} autoComplete="name" value={fullName} onChange={(e) => setFullName(e.target.value)} placeholder="Your full name" /></label>
            <label className="field">Your role <span className="muted">(optional)</span><input maxLength={120} value={jobTitle} onChange={(e) => setJobTitle(e.target.value)} placeholder="e.g. Founder, CTO, Engineer" /></label>
            <label className="field">Account email<input value={email} readOnly /></label>
          </div>
          <div className="onboarding-section">
            <h2>{accountType === "individual" ? "Your workspace" : "Your organization"}</h2>
            <label className="field">{accountType === "individual" ? "Workspace name" : "Organization name"}
              <input required maxLength={120} value={workspaceName} onChange={(e) => setWorkspaceName(e.target.value)} placeholder={accountType === "individual" ? "e.g. My workspace" : "e.g. Acme Inc."} />
            </label>
            {accountType !== "individual" && <label className="field">Company size
              <select required value={companySize} onChange={(e) => setCompanySize(e.target.value as "" | "1-10" | "10-100" | "100+")}>
                <option value="">Select company size</option><option value="1-10">1–10 employees</option><option value="10-100">10–100 employees</option><option value="100+">100+ employees</option>
              </select>
            </label>}
            <label className="field">Industry
              <select value={industry} onChange={(e) => setIndustry(e.target.value)}>
                {["Software / Technology", "Financial Services", "Healthcare", "E-commerce", "Consulting", "Education", "Manufacturing", "Other"].map((item) => <option key={item}>{item}</option>)}
              </select>
            </label>
          </div>
          <div className="onboarding-section">
            <h2>What are you building?</h2>
            <label className="field">Primary use case
              <select value={useCase} onChange={(e) => setUseCase(e.target.value as OnboardingPayload["primary_use_case"])}>
                <option value="personal_projects">Personal projects</option><option value="internal_automation">Internal automation</option><option value="production_agents">Production AI agents</option><option value="customer_facing_agents">Customer-facing agents</option><option value="research">Research and experimentation</option>
              </select>
            </label>
            <label className="field">How many agents do you expect to coordinate?
              <select value={expectedAgents} onChange={(e) => setExpectedAgents(e.target.value as OnboardingPayload["expected_agents"])}>
                <option value="1-5">1–5 agents</option><option value="6-20">6–20 agents</option><option value="21-100">21–100 agents</option><option value="100+">100+ agents</option>
              </select>
            </label>
            <div className="field"><span>Frameworks you use <span className="muted">(select all that apply)</span></span>
              <div className="framework-options">{frameworkOptions.map(([value, label]) => <label key={value}><input type="checkbox" checked={frameworks.includes(value)} onChange={() => toggleFramework(value)} />{label}</label>)}</div>
            </div>
          </div>
          {message && <p role="alert" className="notice err">{message.text}</p>}
          <button className="btn btn-primary btn-lg btn-block" disabled={busy || !fullName.trim() || !workspaceName.trim() || !industry.trim() || (accountType !== "individual" && !companySize)}>{busy ? "Creating your workspace…" : "Create workspace and continue"}</button>
        </form>
        <button className="btn btn-secondary btn-block" onClick={onSignOut}>Sign out</button>
      </section>
      <small className="muted" style={{ position: "relative", zIndex: 1 }}>Cohort by Cerbère · Coordination for AI agents</small>
    </main>
  );
}
