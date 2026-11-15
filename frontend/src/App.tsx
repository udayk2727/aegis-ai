import { useState } from "react";

import { ErrorPanel, EmptyPanel, LoadingMessage } from "./components/StatePanels";
import { IncidentList } from "./components/IncidentList";
import { InvestigationReport } from "./components/InvestigationReport";
import { useIncidentDashboard, type DashboardState } from "./hooks/useIncidentDashboard";
import { useInvestigation } from "./hooks/useInvestigation";
import type { IncidentPage } from "./api/types";

export default function App() {
  const { state, retry } = useIncidentDashboard();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  return (
    <div className="app">
      <header className="app-header">
        <h1>Incident dashboard</h1>
        <p className="muted">
          Synthetic demo data from the local API. Read-only. Hypotheses are
          unverified, and no root cause is assessed.
        </p>
      </header>
      <main id="main" className="app-main">
        <DashboardBody
          state={state}
          onRetry={retry}
          selectedId={selectedId}
          onSelect={setSelectedId}
        />
      </main>
    </div>
  );
}

function DashboardBody({
  state,
  onRetry,
  selectedId,
  onSelect,
}: {
  state: DashboardState;
  onRetry: () => void;
  selectedId: string | null;
  onSelect: (incidentId: string) => void;
}) {
  switch (state.phase) {
    case "loading":
      return <LoadingMessage>Loading approved baselines…</LoadingMessage>;
    case "error":
      return (
        <ErrorPanel
          title="Could not load the demo data"
          error={state.error}
          onRetry={onRetry}
        />
      );
    case "no-baselines":
      return (
        <EmptyPanel title="No approved baselines are stored" onRetry={onRetry}>
          <p>
            Prepare the demo data as described in the README&apos;s Dashboard
            section, then check again. The API needs the demo listing enabled
            and both normal baseline runs stored.
          </p>
        </EmptyPanel>
      );
    case "no-incidents":
      return (
        <EmptyPanel
          title="No incidents were found for these baselines"
          onRetry={onRetry}
        >
          <p>
            The approved baselines are stored, but no fault run has produced an
            incident. Run the fault scenario in the README&apos;s Dashboard
            section, then check again.
          </p>
        </EmptyPanel>
      );
    case "ready":
      return (
        <IncidentWorkspace
          baselineIds={state.baselineIds}
          page={state.page}
          selectedId={selectedId}
          onSelect={onSelect}
        />
      );
  }
}

function IncidentWorkspace({
  baselineIds,
  page,
  selectedId,
  onSelect,
}: {
  baselineIds: readonly string[];
  page: IncidentPage;
  selectedId: string | null;
  onSelect: (incidentId: string) => void;
}) {
  // An unknown or stale selection falls back to the first incident, so the
  // panel always shows an investigation for something in the list.
  const selected =
    page.items.find((item) => item.incident_id === selectedId) ?? page.items[0];

  return (
    <div className="workspace">
      <IncidentList
        page={page}
        selectedId={selected.incident_id}
        onSelect={onSelect}
      />
      <InvestigationPanel
        key={selected.incident_id}
        incidentId={selected.incident_id}
        baselineIds={baselineIds}
      />
    </div>
  );
}

function InvestigationPanel({
  incidentId,
  baselineIds,
}: {
  incidentId: string;
  baselineIds: readonly string[];
}) {
  const { state, retry } = useInvestigation(incidentId, baselineIds);

  return (
    <section className="panel investigation" aria-labelledby="investigation-heading">
      <h2 id="investigation-heading">Investigation</h2>
      {state.phase === "loading" && (
        <LoadingMessage>Loading the investigation for {incidentId}…</LoadingMessage>
      )}
      {state.phase === "error" && (
        <ErrorPanel
          title="The investigation could not be loaded"
          error={state.error}
          onRetry={retry}
        />
      )}
      {state.phase === "ready" && <InvestigationReport report={state.report} />}
    </section>
  );
}
