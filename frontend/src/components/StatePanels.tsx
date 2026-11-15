import type { ReactNode } from "react";

import type { ApiError } from "../api/client";

/** A polite status line for a load that is in progress. */
export function LoadingMessage({ children }: { children: ReactNode }) {
  return (
    <p className="state state-loading" role="status">
      {children}
    </p>
  );
}

/** A panel for a failed read. The message is the API's own, never raw text. */
export function ErrorPanel({
  title,
  error,
  onRetry,
}: {
  title: string;
  error: ApiError;
  onRetry: () => void;
}) {
  return (
    <div className="state state-error" role="alert">
      <h2>{title}</h2>
      <p>{error.message}</p>
      {error.code !== null && (
        <p className="muted">
          Code <code>{error.code}</code>
          {error.status !== null && <> · HTTP {error.status}</>}
        </p>
      )}
      <button type="button" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}

/** A panel for a successful read that has nothing to show. */
export function EmptyPanel({
  title,
  children,
  onRetry,
}: {
  title: string;
  children: ReactNode;
  onRetry: () => void;
}) {
  return (
    <div className="state state-empty">
      <h2>{title}</h2>
      {children}
      <button type="button" onClick={onRetry}>
        Check again
      </button>
    </div>
  );
}
