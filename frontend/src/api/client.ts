import type { ApiErrorBody } from "./types";

/** Same-origin path. The Vite dev proxy forwards it to the FastAPI server. */
export const API_BASE = "/api/v1";

export const BASELINES_URL = `${API_BASE}/demo/baselines`;

/**
 * Error from a read request. It carries the API's fixed code and message, and
 * never a response body, stack trace, or path. ``status`` is null when the
 * server could not be reached at all.
 */
export class ApiError extends Error {
  readonly status: number | null;
  readonly code: string | null;

  constructor(message: string, status: number | null, code: string | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/**
 * Build the repeated ``baseline_run_id`` query string, in the order given.
 * The backend reads each occurrence as one baseline, so the list is never
 * joined into a single comma-separated value.
 */
export function baselineQuery(baselineRunIds: readonly string[]): string {
  const params = new URLSearchParams();
  for (const runId of baselineRunIds) {
    params.append("baseline_run_id", runId);
  }
  return params.toString();
}

export function incidentsUrl(baselineRunIds: readonly string[]): string {
  return `${API_BASE}/incidents?${baselineQuery(baselineRunIds)}`;
}

export function investigationUrl(
  incidentId: string,
  baselineRunIds: readonly string[],
): string {
  const path = `${API_BASE}/incidents/${encodeURIComponent(incidentId)}/investigation`;
  return `${path}?${baselineQuery(baselineRunIds)}`;
}

/**
 * GET a JSON resource. A failed response becomes an ApiError built from the
 * body's ``code`` and ``message`` only when both are strings; anything else gets
 * a generic message, so raw server text never reaches the page.
 */
export async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, {
      headers: { Accept: "application/json" },
      signal,
    });
  } catch (error) {
    if (signal?.aborted) {
      throw error;
    }
    throw new ApiError("The API could not be reached.", null, null);
  }

  if (!response.ok) {
    const body = await readErrorBody(response);
    throw new ApiError(
      body?.message ?? `The API returned status ${response.status}.`,
      response.status,
      body?.code ?? null,
    );
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("The API returned a response that is not JSON.", response.status, null);
  }
}

/** Normalize anything a failed read throws into an ApiError the page can show. */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error;
  }
  return new ApiError("An unexpected error occurred.", null, null);
}

async function readErrorBody(response: Response): Promise<ApiErrorBody | null> {
  try {
    const body: unknown = await response.json();
    if (
      typeof body === "object" &&
      body !== null &&
      "code" in body &&
      "message" in body &&
      typeof body.code === "string" &&
      typeof body.message === "string"
    ) {
      return { code: body.code, message: body.message };
    }
  } catch {
    // Not JSON, or no body. The caller falls back to the status line.
  }
  return null;
}
