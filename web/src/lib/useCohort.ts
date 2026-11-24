import { useCallback, useEffect, useRef, useState } from "react";
import { api, isUnavailable } from "./api";
import type { Branch, Data, EventItem, Health, Lease, Overview, Settings, StateSnapshot } from "./types";

const empty: Data = {
  overview: null, branches: [], state: { head: "", parent: null, items: [] }, events: [], leases: [], settings: null, health: null,
  connected: false, loaded: false, error: "", lastUpdated: null,
};

const POLL_INTERVAL_MS = 30_000;
const RETRY_INTERVAL_MS = 60_000;

/**
 * Refreshes data without reloading the page. Polling is deliberately slow,
 * sequential, and paused while the browser tab is hidden.
 */
export function useCohort() {
  const [data, setData] = useState<Data>(empty);
  const inFlight = useRef(false);

  const refresh = useCallback(async (): Promise<boolean> => {
    if (inFlight.current) return false;
    inFlight.current = true;
    try {
      const [overview, branches, state, events, leases, settings, health] = await Promise.all([
        api<Overview>("/overview"), api<{ items: Branch[] }>("/branches"), api<StateSnapshot>("/state"),
        api<{ items: EventItem[] }>("/events?limit=300"), api<{ items: Lease[] }>("/leases"), api<Settings>("/settings"),
        api<Health>("/health").catch(() => null),
      ]);
      const next = {
        overview, branches: branches.items, state, events: events.items, leases: leases.items,
        settings, health, connected: true, loaded: true, error: "",
      };
      setData((prev) => {
        // Preserve the current state reference when the API returned no changes.
        // This avoids rerendering the entire dashboard on every polling cycle.
        const unchanged = JSON.stringify(prev.overview) === JSON.stringify(next.overview)
          && JSON.stringify(prev.branches) === JSON.stringify(next.branches)
          && JSON.stringify(prev.state) === JSON.stringify(next.state)
          && JSON.stringify(prev.events) === JSON.stringify(next.events)
          && JSON.stringify(prev.leases) === JSON.stringify(next.leases)
          && JSON.stringify(prev.settings) === JSON.stringify(next.settings)
          && JSON.stringify(prev.health) === JSON.stringify(next.health);
        if (unchanged && prev.connected && prev.loaded && !prev.error) return prev;
        return { ...next, lastUpdated: unchanged ? prev.lastUpdated : Date.now() };
      });
      return true;
    } catch (err) {
      setData((prev) => ({
        ...prev,
        connected: false,
        loaded: true,
        error: err instanceof Error ? err.message : "Unable to reach Cohort API",
      }));
      return false;
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;

    const clearTimer = () => {
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
    };

    const poll = async () => {
      if (!active || document.hidden) return;
      const succeeded = await refresh();
      if (!active || document.hidden) return;
      timer = window.setTimeout(poll, succeeded ? POLL_INTERVAL_MS : RETRY_INTERVAL_MS);
    };

    const onVisibilityChange = () => {
      clearTimer();
      // Returning to the tab must not trigger a burst of API calls or a visual refresh.
      // Resume the normal cadence instead.
      if (!document.hidden) timer = window.setTimeout(poll, POLL_INTERVAL_MS);
    };

    void poll();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      active = false;
      clearTimer();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [refresh]);

  return { data, refresh };
}

export type Optional<T> = { status: "loading" | "ready" | "unavailable" | "error"; data: T | null; error: string; reload: () => void };

/** Optional endpoints: unsupported responses become unavailable; polling pauses while the tab is hidden. */
export function useOptional<T>(path: string | null, intervalMs = 0): Optional<T> {
  const [state, setState] = useState<{ status: Optional<T>["status"]; data: T | null; error: string }>({ status: "loading", data: null, error: "" });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!path) return;
    let active = true;
    let timer: number | undefined;

    const clearTimer = () => {
      if (timer !== undefined) {
        window.clearTimeout(timer);
        timer = undefined;
      }
    };

    let load: () => Promise<void> = async () => {};

    const schedule = (delay: number) => {
      if (active && intervalMs && !document.hidden) {
        clearTimer();
        timer = window.setTimeout(load, delay);
      }
    };

    load = async () => {
      if (!active || document.hidden) return;
      try {
        const data = await api<T>(path);
        if (!active) return;
        setState((prev) => {
          if (prev.status === "ready" && JSON.stringify(prev.data) === JSON.stringify(data)) return prev;
          return { status: "ready", data, error: "" };
        });
        schedule(intervalMs);
      } catch (err) {
        if (!active) return;
        if (isUnavailable(err)) {
          setState({ status: "unavailable", data: null, error: "" });
        } else {
          setState((prev) => ({ status: "error", data: prev.data, error: err instanceof Error ? err.message : "Request failed" }));
          schedule(intervalMs * 2);
        }
      }
    };

    const onVisibilityChange = () => {
      clearTimer();
      // Do not refresh immediately when the user switches back to this window.
      if (!document.hidden && intervalMs) schedule(intervalMs);
    };

    void load();
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      active = false;
      clearTimer();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [path, intervalMs, nonce]);

  return { ...state, reload: () => setNonce((n) => n + 1) };
}
