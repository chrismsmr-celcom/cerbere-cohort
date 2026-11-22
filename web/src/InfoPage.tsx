import { ArrowLeft, ArrowUpRight } from "lucide-react";
import cohortLogo from "./cohortLogo";
import "./landing.css";

type InfoKind = "terms" | "privacy" | "blog";
export default function InfoPage({ kind }: { kind: InfoKind }) {
  const isBlog = kind === "blog";
  const title = kind === "terms" ? "Terms of service" : kind === "privacy" ? "Privacy & confidentiality" : "Notes from Cohort";
  const description = isBlog
    ? "Product updates, engineering notes and ideas about reliable multi-agent coordination will be published here."
    : "This page is being prepared for the Cohort public beta. We are not publishing incomplete legal terms or making unsupported claims about data handling.";
  return <main className="cs-info-page">
    <a className="cs-info-brand" href="/"><img src={cohortLogo} alt="" /> <strong>Cohort</strong></a>
    <div className="cs-info-card">
      <span className="cs-section-kicker">{isBlog ? "THE COHORT BLOG" : "COHORT · PUBLIC BETA"}</span>
      <h1>{title}</h1>
      <p>{description}</p>
      {!isBlog && <p id="confidentiality">For questions about the current beta, account information, workspace data or confidentiality, contact the Cohort team directly. We will provide the applicable details before asking users to rely on a finalized policy.</p>}
      {isBlog && <p>In the meantime, follow the project repository for source changes and release notes.</p>}
      <div className="cs-info-actions">
        <a className="cs-button" href={isBlog ? "https://github.com/chrismsmr-celcom/cerbere-cohort" : "mailto:christopher-ag@cerbereag.site"}>{isBlog ? "Follow Cohort on GitHub" : "Contact Cohort"} <ArrowUpRight size={15} /></a>
        <a className="cs-info-back" href="/"><ArrowLeft size={15} /> Back to home</a>
      </div>
      <small>christopher-ag@cerbereag.site</small>
    </div>
  </main>;
}
