import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Check, Search, X } from "lucide-react";

/* ---------- Toasts ---------- */
type Toast = { id: number; text: string; tone: "ok" | "err" };
const ToastCtx = createContext<(text: string, tone?: "ok" | "err") => void>(() => {});
export const useToast = () => useContext(ToastCtx);
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const notify = useCallback((text: string, tone: "ok" | "err" = "ok") => {
    const id = Date.now() + Math.random();
    setItems((prev) => [...prev.slice(-3), { id, text, tone }]);
    window.setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), tone === "err" ? 6000 : 3000);
  }, []);
  return <ToastCtx.Provider value={notify}>{children}
    <div className="toasts" role="status" aria-live="polite">{items.map((t) => <div key={t.id} className={`toast ${t.tone === "err" ? "err" : ""}`}>{t.tone === "ok" && <Check size={16} />}{t.text}</div>)}</div>
  </ToastCtx.Provider>;
}

/* ---------- Modal / Drawer ---------- */
export function Modal({ title, description, onClose, children, footer, size, drawer, className }: {
  title: string; description?: string; onClose: () => void; children: ReactNode; footer?: ReactNode; size?: "wide" | "xl"; drawer?: boolean; className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); onClose(); } };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); previous?.focus?.(); };
  }, [onClose]);
  return <div className={`backdrop ${drawer ? "drawer" : ""} ${className || ""}`} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <div className={`modal ${size || ""}`} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={ref}>
      <div className="modal-h"><div><h2>{title}</h2>{description && <p>{description}</p>}</div>
        <button className="icon-btn" onClick={onClose} aria-label="Close"><X size={16} /></button></div>
      <div className="modal-b">{children}</div>
      {footer && <div className="modal-f">{footer}</div>}
    </div>
  </div>;
}

/* ---------- Small building blocks ---------- */
export const Skeleton = ({ w = "100%", h = 16 }: { w?: number | string; h?: number }) => <div className="skel" style={{ width: w, height: h }} aria-hidden />;
export function TableSkeleton({ rows = 4 }: { rows?: number }) {
  return <div className="stack" style={{ padding: "0 24px 24px" }} aria-busy="true" aria-label="Loading">{Array.from({ length: rows }, (_, i) => <Skeleton key={i} h={32} />)}</div>;
}
export function Empty({ icon, title, children, action }: { icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return <div className="empty">{icon && <div className="ico">{icon}</div>}<strong>{title}</strong>{children && <p>{children}</p>}{action && <div style={{ marginTop: 16 }}>{action}</div>}</div>;
}
export function Tag({ tone, children }: { tone?: "ok" | "warn" | "err" | "accent"; children: ReactNode }) {
  return <span className={`tag ${tone || ""}`}><i />{children}</span>;
}
export function PageHead({ title, description, actions }: { title: string; description: string; actions?: ReactNode }) {
  return <div className="page-head"><div><h1>{title}</h1><p>{description}</p></div><div className="row">{actions}</div></div>;
}
export function Card({ title, description, actions, children, className, id, flush = true }: { title?: string; description?: string; actions?: ReactNode; children?: ReactNode; className?: string; id?: string; flush?: boolean }) {
  return <section id={id} className={`card ${className || ""}`}>
    {title && <div className="card-h"><div><h2>{title}</h2>{description && <p>{description}</p>}</div>{actions}</div>}
    {children && <div className={flush ? "card-b flush" : "card-b"}>{children}</div>}
  </section>;
}
export const Metric = ({ label, value, hint, tone, className }: { label: string; value: string | number; hint?: string; tone?: "err"; className?: string }) =>
  <div className={`card metric ${className || "c3"}`}><span>{label}</span><strong className={tone || ""}>{value}</strong>{hint && <small>{hint}</small>}</div>;
export const Seg = <T extends string>({ value, options, onChange, label }: { value: T; options: { id: T; label: string }[]; onChange: (v: T) => void; label: string }) =>
  <div className="seg" role="group" aria-label={label}>{options.map((o) => <button key={o.id} aria-pressed={value === o.id} onClick={() => onChange(o.id)}>{o.label}</button>)}</div>;
export const SearchInput = ({ value, onChange, placeholder }: { value: string; onChange: (v: string) => void; placeholder: string }) =>
  <label className="search-in"><Search size={16} /><input className="input" value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} aria-label={placeholder} /></label>;
export const Json = ({ value }: { value: unknown }) => <pre className="code">{JSON.stringify(value, null, 2)}</pre>;
export function Disconnected({ message, onRetry }: { message: string; onRetry: () => void }) {
  return <div className="notice err" role="alert"><div className="grow"><strong>Runtime unavailable</strong><span>{message}</span></div><button className="btn btn-secondary" onClick={onRetry}>Retry</button></div>;
}
export const GithubMark = ({ size = 18, className }: { size?: number; className?: string }) =>
  <svg className={className} width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden><path d="M12 .5A11.5 11.5 0 0 0 .5 12c0 5.08 3.29 9.39 7.86 10.91.58.1.79-.25.79-.56v-2c-3.2.7-3.88-1.37-3.88-1.37-.52-1.33-1.28-1.69-1.28-1.69-1.05-.71.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.55-.29-5.24-1.28-5.24-5.68 0-1.26.45-2.28 1.19-3.09-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.17 1.18a11 11 0 0 1 5.78 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.83 1.19 3.09 0 4.41-2.69 5.39-5.25 5.67.41.36.78 1.06.78 2.14v3.17c0 .31.21.67.8.56A11.5 11.5 0 0 0 23.5 12 11.5 11.5 0 0 0 12 .5Z" /></svg>;
