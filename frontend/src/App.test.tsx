import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import App from "./App";
import {
  BASELINE_A,
  BASELINE_B,
  INCIDENT_ID,
  OTHER_INCIDENT_ID,
  baseline,
  incidentPage,
  incidentSummary,
  investigation,
} from "./test/fixtures";

type Handler = (
  url: URL,
  signal: AbortSignal | undefined,
) => Promise<Response> | Response;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Route fetch by path. The mock records every call so tests can check the query. */
function stubApi(handler: Handler) {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) =>
    Promise.resolve(
      handler(new URL(String(input), "http://127.0.0.1"), init?.signal ?? undefined),
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestedUrls(fetchMock: ReturnType<typeof stubApi>): string[] {
  return fetchMock.mock.calls.map(([input]) => String(input));
}

const BASELINES_BODY = {
  data_origin: "synthetic",
  items: [baseline(BASELINE_A, 301), baseline(BASELINE_B, 302)],
  total: 2,
};

function standardHandler(
  incidentIds: string[] = [INCIDENT_ID],
): Handler {
  return (url) => {
    if (url.pathname === "/api/v1/demo/baselines") {
      return json(BASELINES_BODY);
    }
    if (url.pathname === "/api/v1/incidents") {
      return json(incidentPage(incidentIds.map((id) => incidentSummary(id))));
    }
    const match = /^\/api\/v1\/incidents\/([^/]+)\/investigation$/.exec(url.pathname);
    if (match) {
      return json(investigation(decodeURIComponent(match[1])));
    }
    return json({ code: "not_found", message: "No such route." }, 404);
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("App", () => {
  it("discovers baselines, lists incidents, and renders the investigation", async () => {
    const fetchMock = stubApi(standardHandler());

    render(<App />);

    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading approved baselines",
    );
    expect(
      await screen.findByRole("heading", { name: "Incidents" }),
    ).toBeInTheDocument();

    const urls = requestedUrls(fetchMock);
    const query = `baseline_run_id=${BASELINE_A}&baseline_run_id=${BASELINE_B}`;
    expect(urls).toEqual([
      "/api/v1/demo/baselines",
      `/api/v1/incidents?${query}`,
      `/api/v1/incidents/${INCIDENT_ID}/investigation?${query}`,
    ]);

    expect(await screen.findByText("CPU resource saturation")).toBeInTheDocument();
    expect(screen.getByText("Unverified")).toBeInTheDocument();
    expect(screen.getByText(/Not assessed\./)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /inc-0123/ })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });

  it("shows the no-baselines state and does not request incidents", async () => {
    const fetchMock = stubApi((url) =>
      url.pathname === "/api/v1/demo/baselines"
        ? json({ data_origin: "synthetic", items: [], total: 0 })
        : json({ code: "x", message: "unexpected" }, 500),
    );

    render(<App />);

    expect(
      await screen.findByRole("heading", { name: "No approved baselines are stored" }),
    ).toBeInTheDocument();
    expect(requestedUrls(fetchMock)).toEqual(["/api/v1/demo/baselines"]);
  });

  it("shows the no-incidents state when the baselines have no incidents", async () => {
    stubApi(standardHandler([]));

    render(<App />);

    expect(
      await screen.findByRole("heading", {
        name: "No incidents were found for these baselines",
      }),
    ).toBeInTheDocument();
  });

  it("shows a baseline API error and retries the load", async () => {
    let baselineCalls = 0;
    const handler = standardHandler();
    const fetchMock = stubApi((url, signal) => {
      if (url.pathname === "/api/v1/demo/baselines") {
        baselineCalls += 1;
        if (baselineCalls === 1) {
          return json({ code: "internal", message: "Demo listing failed." }, 500);
        }
      }
      return handler(url, signal);
    });

    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("Demo listing failed.")).toBeInTheDocument();
    expect(within(alert).getByText("internal")).toBeInTheDocument();

    await userEvent.setup().click(within(alert).getByRole("button", { name: "Retry" }));

    expect(
      await screen.findByRole("heading", { name: "Incidents" }),
    ).toBeInTheDocument();
    expect(baselineCalls).toBe(2);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("shows an investigation error with its code and retries it", async () => {
    let investigationCalls = 0;
    const handler = standardHandler();
    stubApi((url, signal) => {
      if (url.pathname.endsWith("/investigation")) {
        investigationCalls += 1;
        if (investigationCalls === 1) {
          return json(
            {
              code: "analysis_failed",
              message: "The investigation could not be built.",
            },
            500,
          );
        }
      }
      return handler(url, signal);
    });

    render(<App />);

    const alert = await screen.findByRole("alert");
    expect(
      within(alert).getByRole("heading", {
        name: "The investigation could not be loaded",
      }),
    ).toBeInTheDocument();
    expect(within(alert).getByText("analysis_failed")).toBeInTheDocument();

    await userEvent.setup().click(within(alert).getByRole("button", { name: "Retry" }));

    expect(await screen.findByText("CPU resource saturation")).toBeInTheDocument();
    expect(investigationCalls).toBe(2);
  });

  it("aborts an earlier investigation and ignores its late response", async () => {
    const lateA = deferred<Response>();
    let signalA: AbortSignal | undefined;
    stubApi((url, signal) => {
      if (url.pathname === "/api/v1/demo/baselines") {
        return json(BASELINES_BODY);
      }
      if (url.pathname === "/api/v1/incidents") {
        return json(incidentPage([incidentSummary(INCIDENT_ID), incidentSummary(OTHER_INCIDENT_ID)]));
      }
      if (url.pathname.includes(INCIDENT_ID)) {
        signalA = signal;
        // Ignores the abort signal on purpose, so the component's own guard is tested.
        return lateA.promise;
      }
      return json(investigation(OTHER_INCIDENT_ID, "Report for the second incident."));
    });

    render(<App />);
    await screen.findByRole("button", { name: /inc-fedcba/ });

    await userEvent.setup().click(screen.getByRole("button", { name: /inc-fedcba/ }));

    expect(
      await screen.findByText("Report for the second incident."),
    ).toBeInTheDocument();
    expect(signalA?.aborted).toBe(true);

    lateA.resolve(json(investigation(INCIDENT_ID, "Late report for the first incident.")));
    await new Promise((done) => setTimeout(done, 0));

    expect(screen.queryByText("Late report for the first incident.")).toBeNull();
    expect(screen.getByText("Report for the second incident.")).toBeInTheDocument();
  });

  it("selects an incident with the keyboard and loads its investigation", async () => {
    const fetchMock = stubApi(standardHandler([INCIDENT_ID, OTHER_INCIDENT_ID]));

    render(<App />);
    const second = await screen.findByRole("button", { name: /inc-fedcba/ });
    await screen.findByText("CPU resource saturation");

    second.focus();
    await userEvent.setup().keyboard("{Enter}");

    expect(second).toHaveAttribute("aria-pressed", "true");
    await waitFor(() =>
      expect(requestedUrls(fetchMock)).toContain(
        `/api/v1/incidents/${OTHER_INCIDENT_ID}/investigation?` +
          `baseline_run_id=${BASELINE_A}&baseline_run_id=${BASELINE_B}`,
      ),
    );
  });
});
