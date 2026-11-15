import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BASELINE_A, BASELINE_B, INCIDENT_ID, investigation } from "../test/fixtures";
import { useInvestigation } from "./useInvestigation";

const BASELINE_IDS = [BASELINE_A, BASELINE_B];

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("useInvestigation", () => {
  it("keeps the newer response when an older request resolves after a retry", async () => {
    // The first request ignores its abort signal, so only the hook's guard can
    // stop its late response from being shown.
    const older = deferred<Response>();
    const signals: AbortSignal[] = [];
    let calls = 0;
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      calls += 1;
      if (init?.signal) {
        signals.push(init.signal);
      }
      if (calls === 1) {
        return older.promise;
      }
      return Promise.resolve(json(investigation(INCIDENT_ID, "Newer report.")));
    });
    vi.stubGlobal("fetch", fetchMock);

    const { result } = renderHook(() => useInvestigation(INCIDENT_ID, BASELINE_IDS));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Retry while the first request is still pending. The second request
    // resolves first.
    act(() => result.current.retry());

    await waitFor(() => expect(result.current.state.phase).toBe("ready"));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(signals[0].aborted).toBe(true);
    expect(signals[1].aborted).toBe(false);

    // The older response arrives after the newer one has been shown.
    await act(async () => {
      older.resolve(json(investigation(INCIDENT_ID, "Older report.")));
      await new Promise((done) => setTimeout(done, 0));
    });

    expect(result.current.state.phase).toBe("ready");
    if (result.current.state.phase !== "ready") {
      throw new Error("expected a ready investigation");
    }
    expect(result.current.state.report.verified_observations[0].statement).toBe(
      "Newer report.",
    );
  });
});
