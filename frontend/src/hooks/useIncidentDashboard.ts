import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  BASELINES_URL,
  getJson,
  incidentsUrl,
  toApiError,
} from "../api/client";
import type { DemoBaselineList, IncidentPage } from "../api/types";

/**
 * Where the dashboard is in its load. ``ready`` carries the baseline IDs so the
 * investigation request can name the same baselines as the incident list.
 */
export type DashboardState =
  | { phase: "loading" }
  | { phase: "error"; error: ApiError }
  | { phase: "no-baselines" }
  | { phase: "no-incidents"; baselineIds: readonly string[] }
  | { phase: "ready"; baselineIds: readonly string[]; page: IncidentPage };

/**
 * Discover approved baselines, then list incidents against them. The second
 * request is skipped when there are no baselines, because the incident
 * endpoint requires at least one.
 */
async function loadDashboard(signal: AbortSignal): Promise<DashboardState> {
  const list = await getJson<DemoBaselineList>(BASELINES_URL, signal);
  const baselineIds = list.items.map((baseline) => baseline.run_id);
  if (baselineIds.length === 0) {
    return { phase: "no-baselines" };
  }

  const page = await getJson<IncidentPage>(incidentsUrl(baselineIds), signal);
  if (page.items.length === 0) {
    return { phase: "no-incidents", baselineIds };
  }
  return { phase: "ready", baselineIds, page };
}

/**
 * Load the dashboard once per attempt. A retry starts a new attempt, and any
 * request still in flight for an earlier attempt is aborted and then ignored.
 */
export function useIncidentDashboard(): {
  state: DashboardState;
  retry: () => void;
} {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<DashboardState>({ phase: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    loadDashboard(controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) {
          setState(next);
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setState({ phase: "error", error: toApiError(error) });
        }
      });
    return () => controller.abort();
  }, [attempt]);

  const retry = useCallback(() => {
    setState({ phase: "loading" });
    setAttempt((count) => count + 1);
  }, []);

  return { state, retry };
}
