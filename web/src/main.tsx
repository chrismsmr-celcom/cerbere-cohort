import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource-variable/manrope";
import "@fontsource/roboto/400.css";
import "@fontsource/roboto/500.css";
import App from "./App";
import LandingPage from "./LandingPage";
import ErrorBoundary from "./ErrorBoundary";

// Apply the saved theme before first paint so the sign-in screen matches too.
try {
  const saved = localStorage.getItem("cohort-theme");
  document.documentElement.dataset.theme = saved === "dark" || (!saved && window.matchMedia("(prefers-color-scheme: dark)").matches) ? "dark" : "light";
} catch { document.documentElement.dataset.theme = "light"; }

const Page = window.location.pathname === "/" ? LandingPage : App;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode><ErrorBoundary><Page /></ErrorBoundary></React.StrictMode>
);
