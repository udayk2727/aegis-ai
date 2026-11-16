import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  baselineQuery,
  getJson,
  incidentsUrl,
  investigationUrl,
  toApiError,
} from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("baselineQuery", () => {
  it("repeats baseline_run_id once per baseline, in order", () => {
    expect(baselineQuery(["run-a", "run-b"])).toBe(
      "baseline_run_id=run-a&baseline_run_id=run-b",
    );
  });

  it("encodes values rather than joining them", () => {
    expect(baselineQuery(["run a&b"])).toBe("baseline_run_id=run+a%26b");
  });
});

describe("request URLs", () => {
  const baselines = ["run-a", "run-b"];

  it("uses the same baseline parameters for the incident list and investigation", () => {
    const list = incidentsUrl(baselines);
    const investigation = investigationUrl("inc-0123456789abcdef", baselines);

    expect(list).toBe(
      "/api/v1/incidents?baseline_run_id=run-a&baseline_run_id=run-b",
    );
    expect(investigation).toBe(
      "/api/v1/incidents/inc-0123456789abcdef/investigation" +
        "?baseline_run_id=run-a&baseline_run_id=run-b",
    );
    expect(list.split("?")[1]).toBe(investigation.split("?")[1]);
  });

  it("encodes the incident ID in the path", () => {
    expect(investigationUrl("inc/../x", ["run-a"])).toBe(
      "/api/v1/incidents/inc%2F..%2Fx/investigation?baseline_run_id=run-a",
    );
  });
});

describe("getJson", () => {
  it("returns the parsed body on success", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ ok: 1 })));

    await expect(getJson<{ ok: number }>("/api/v1/x")).resolves.toEqual({
      ok: 1,
    });
  });

  it("keeps the API's fixed code and message on an error response", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse(
            { code: "baseline_not_approved", message: "Baseline is not approved." },
            403,
          ),
        ),
    );

    const error = await getJson("/api/v1/x").catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 403,
      code: "baseline_not_approved",
      message: "Baseline is not approved.",
    });
  });

  it("explains a proxy error during an API restart as temporary", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(new Response("<html>502 Bad Gateway</html>", { status: 502 })),
    );

    const error = await getJson("/api/v1/x").catch((caught: unknown) => caught);

    expect(error).toMatchObject({ status: 502, code: null });
    expect((error as Error).message).toContain("temporarily unavailable");
  });

  it("uses a generic message when the error body is not the API shape", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response("<html>Traceback (most recent call last)</html>", {
            status: 500,
          }),
        ),
    );

    const error = await getJson("/api/v1/x").catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      status: 500,
      code: null,
      message: "The API returned status 500.",
    });
    expect((error as Error).message).not.toContain("Traceback");
  });

  it("reports an unreachable server with no status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("fetch failed")));

    const error = await getJson("/api/v1/x").catch((caught: unknown) => caught);

    expect(error).toMatchObject({
      status: null,
      code: null,
      message: "The API could not be reached.",
    });
  });

  it("rethrows an abort so the caller can tell it apart from a failure", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => {
        controller.abort();
        return Promise.reject(new DOMException("aborted", "AbortError"));
      }),
    );

    const error = await getJson("/api/v1/x", controller.signal).catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(DOMException);
    expect((error as DOMException).name).toBe("AbortError");
  });
});

describe("toApiError", () => {
  it("passes an ApiError through and wraps anything else", () => {
    const known = new ApiError("known", 500, "analysis_failed");

    expect(toApiError(known)).toBe(known);
    expect(toApiError("boom")).toMatchObject({
      message: "An unexpected error occurred.",
      status: null,
    });
  });
});
