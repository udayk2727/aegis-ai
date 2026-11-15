import { useCallback, useEffect, useState } from "react";

import { ApiError, getJson, investigationUrl, toApiError } from "../api/client";
import type { Investigation } from "../api/types";

export type InvestigationState =
  | { phase: "loading" }
  | { phase: "error"; error: ApiError }
  | { phase: "ready"; report: Investigation };

/**
 * Load the investigation for one incident. The caller keys the component by
 * incident ID, so a new selection starts a fresh request. The cleanup aborts
 * the previous request, and the guard drops any response that still arrives
 * after its request was aborted, so it cannot replace the current report.
 */
export function useInvestigation(
  incidentId: string,
  baselineIds: readonly string[],
): { state: InvestigationState; retry: () => void } {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<InvestigationState>({ phase: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    getJson<Investigation>(
      investigationUrl(incidentId, baselineIds),
      controller.signal,
    )
      .then((report) => {
        if (!controller.signal.aborted) {
          setState({ phase: "ready", report });
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setState({ phase: "error", error: toApiError(error) });
        }
      });
    return () => controller.abort();
  }, [incidentId, baselineIds, attempt]);

  const retry = useCallback(() => {
    setState({ phase: "loading" });
    setAttempt((count) => count + 1);
  }, []);

  return { state, retry };
}
