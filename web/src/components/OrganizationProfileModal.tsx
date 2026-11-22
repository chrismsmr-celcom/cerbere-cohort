import { useEffect, useState } from "react";
import { Building2, ImagePlus, LoaderCircle, Trash2, X } from "lucide-react";
import type { ChangeEvent, FormEvent } from "react";
import type { CohortWorkspace } from "../auth";
import { supabase } from "../auth";
import { api } from "../lib/api";

type Profile = {
  id: string;
  name: string;
  legal_name: string | null;
  description: string | null;
  website: string | null;
  industry: string | null;
  country: string | null;
  contact_email: string | null;
  logo_url: string | null;
  brand_color: string | null;
  account_type?: string | null;
};

const emptyProfile = (workspace: CohortWorkspace): Profile => ({
  id: workspace.id, name: workspace.name, legal_name: null, description: null,
  website: null, industry: null, country: null, contact_email: null,
  logo_url: null, brand_color: null, account_type: workspace.account_type || null,
});

export default function OrganizationProfileModal({ workspace, onClose, onSaved }: {
  workspace: CohortWorkspace;
  onClose: () => void;
  onSaved: (profile: Profile) => void;
}) {
  const [profile, setProfile] = useState<Profile>(() => emptyProfile(workspace));
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const [initialLogoUrl, setInitialLogoUrl] = useState<string | null>(null);
  const canEdit = ["owner", "admin"].includes(workspace.role);

  useEffect(() => {
    let active = true;
    void api<{ profile: Profile }>("/workspace/profile")
      .then((result) => { if (active) { const loadedProfile = { ...emptyProfile(workspace), ...result.profile }; setProfile(loadedProfile); setInitialLogoUrl(result.profile.logo_url || null); onSaved(loadedProfile); } })
      .catch((err) => { if (active) { setLoadFailed(true); setError(err instanceof Error ? err.message : "Unable to load organization profile."); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [workspace.id]);

  function change<K extends keyof Profile>(key: K, value: Profile[K]) {
    setProfile((current) => ({ ...current, [key]: value }));
  }

  async function uploadLogo(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    if (!["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
      setError("Choose a PNG, JPEG or WebP image.");
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      setError("Logo must be 2 MB or smaller.");
      return;
    }
    if (!supabase) {
      setError("Supabase Storage is not configured.");
      return;
    }
    setUploading(true);
    setError("");
    try {
      const extension = file.type === "image/jpeg" ? "jpg" : file.type === "image/webp" ? "webp" : "png";
      const path = `${workspace.id}/logo-${crypto.randomUUID()}.${extension}`;
      const { error: uploadError } = await supabase.storage
        .from("cohort-organization-logos")
        .upload(path, file, { upsert: false, contentType: file.type, cacheControl: "3600" });
      if (uploadError) throw uploadError;
      const { data } = supabase.storage.from("cohort-organization-logos").getPublicUrl(path);
      change("logo_url", data.publicUrl);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Logo upload failed.");
    } finally {
      setUploading(false);
    }
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!canEdit || loadFailed || saving) return;
    setSaving(true);
    setError("");
    try {
      const result = await api<{ profile: Profile }>("/workspace/profile", {
        method: "PATCH",
        body: JSON.stringify({
          name: profile.name.trim(),
          legal_name: profile.legal_name || null,
          description: profile.description || null,
          website: profile.website || null,
          industry: profile.industry || null,
          country: profile.country || null,
          contact_email: profile.contact_email || null,
          logo_url: profile.logo_url || null,
          brand_color: profile.brand_color || null,
        }),
      });
      if (initialLogoUrl && initialLogoUrl !== result.profile.logo_url && supabase) {
        const storageBase = `${(import.meta as any).env?.VITE_SUPABASE_URL}/storage/v1/object/public/cohort-organization-logos/`;
        if (initialLogoUrl.startsWith(storageBase)) {
          const oldPath = initialLogoUrl.slice(storageBase.length);
          if (oldPath) await supabase.storage.from("cohort-organization-logos").remove([decodeURIComponent(oldPath)]).catch(() => undefined);
        }
      }
      onSaved(result.profile);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to save organization profile.");
    } finally {
      setSaving(false);
    }
  }

  return <div className="backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="modal org-profile-modal" role="dialog" aria-modal="true" aria-labelledby="org-profile-title">
      <header className="modal-h">
        <div className="org-profile-heading">
          <span className="org-profile-icon"><Building2 size={18} /></span>
          <div><h2 id="org-profile-title">Organization profile</h2><p>Manage the identity shown across this workspace.</p></div>
        </div>
        <button className="icon-btn" type="button" onClick={onClose} aria-label="Close"><X size={18} /></button>
      </header>
      {loading ? <div className="org-profile-loading"><LoaderCircle size={18} className="spin" /> Loading organization profile…</div> : <form onSubmit={save}>
        <div className="modal-b org-profile-body">
          <div className="org-profile-logo-row">
            <div className="org-profile-logo" style={profile.brand_color ? { background: profile.brand_color } : undefined}>
              {profile.logo_url ? <img src={profile.logo_url} alt="Organization logo" /> : <span>{profile.name.slice(0, 1).toUpperCase() || "O"}</span>}
            </div>
            <div className="org-profile-logo-actions">
              <strong>Organization logo</strong>
              <small>PNG, JPEG or WebP · up to 2 MB</small>
              {canEdit && !loadFailed && <div className="org-profile-actions">
                <label className="btn btn-secondary org-btn-sm org-upload-label">
                  {uploading ? <LoaderCircle size={14} className="spin" /> : <ImagePlus size={14} />}
                  {uploading ? "Uploading…" : "Upload logo"}
                  <input type="file" accept="image/png,image/jpeg,image/webp" disabled={uploading} onChange={uploadLogo} />
                </label>
                {profile.logo_url && <button className="btn btn-secondary org-btn-sm" type="button" onClick={() => change("logo_url", null)}><Trash2 size={14} /> Remove</button>}
              </div>}
            </div>
          </div>
          <div className="org-profile-fields">
            <label className="field">Organization name
              <input required maxLength={120} disabled={!canEdit || loadFailed} value={profile.name} onChange={(e) => change("name", e.target.value)} />
            </label>
            <label className="field">Legal name <span className="muted">(optional)</span>
              <input maxLength={160} disabled={!canEdit || loadFailed} value={profile.legal_name || ""} onChange={(e) => change("legal_name", e.target.value || null)} placeholder="Registered company name" />
            </label>
            <label className="field org-profile-full">Description <span className="muted">(optional)</span>
              <textarea rows={3} maxLength={500} disabled={!canEdit || loadFailed} value={profile.description || ""} onChange={(e) => change("description", e.target.value || null)} placeholder="What does your organization do?" />
            </label>
            <label className="field">Website
              <input type="url" maxLength={240} disabled={!canEdit || loadFailed} value={profile.website || ""} onChange={(e) => change("website", e.target.value || null)} placeholder="https://example.com" />
            </label>
            <label className="field">Industry
              <input maxLength={80} disabled={!canEdit || loadFailed} value={profile.industry || ""} onChange={(e) => change("industry", e.target.value || null)} placeholder="Software / Technology" />
            </label>
            <label className="field">Country
              <input maxLength={80} disabled={!canEdit || loadFailed} value={profile.country || ""} onChange={(e) => change("country", e.target.value || null)} placeholder="Country" />
            </label>
            <label className="field">Business email
              <input type="email" maxLength={254} disabled={!canEdit || loadFailed} value={profile.contact_email || ""} onChange={(e) => change("contact_email", e.target.value || null)} placeholder="team@example.com" />
            </label>
            <label className="field org-profile-color">Brand color <span className="muted">(optional)</span>
              <span className="org-color-control"><input type="color" disabled={!canEdit || loadFailed} value={profile.brand_color || "#8062c8"} onChange={(e) => change("brand_color", e.target.value)} /><input aria-label="Hex brand color" disabled={!canEdit || loadFailed} maxLength={7} pattern="^#[0-9A-Fa-f]{6}$" value={profile.brand_color || ""} onChange={(e) => change("brand_color", e.target.value || null)} placeholder="#8062C8" /></span>
            </label>
          </div>
          {!canEdit && <p className="org-profile-readonly">Only workspace owners and admins can edit this profile.</p>}
          {error && <p className="notice err" role="alert">{error}</p>}
        </div>
        <footer className="modal-f">
          <button className="btn btn-secondary" type="button" onClick={onClose}>Close</button>
          {canEdit && <button className="btn btn-primary" type="submit" disabled={saving || uploading || loadFailed || !profile.name.trim()}>{saving ? "Saving…" : "Save changes"}</button>}
        </footer>
      </form>}
    </section>
  </div>;
}
