import type { IncidentPage, IncidentSummary } from "../api/types";
import { showingLabel } from "../utils/format";

/**
 * Selectable incident list. Each row is a native button, so Tab and Enter or
 * Space select it without custom key handling. ``aria-pressed`` marks the one
 * whose investigation is shown.
 */
export function IncidentList({
  page,
  selectedId,
  onSelect,
}: {
  page: IncidentPage;
  selectedId: string;
  onSelect: (incidentId: string) => void;
}) {
  return (
    <section className="panel incident-list" aria-labelledby="incidents-heading">
      <h2 id="incidents-heading">Incidents</h2>
      <p className="muted">
        {showingLabel(page.items.length, page.total, "incident")}
      </p>
      <ul className="incident-items">
        {page.items.map((incident) => (
          <li key={incident.incident_id}>
            <IncidentButton
              incident={incident}
              pressed={incident.incident_id === selectedId}
              onSelect={onSelect}
            />
          </li>
        ))}
      </ul>
    </section>
  );
}

function IncidentButton({
  incident,
  pressed,
  onSelect,
}: {
  incident: IncidentSummary;
  pressed: boolean;
  onSelect: (incidentId: string) => void;
}) {
  return (
    <button
      type="button"
      className="incident-button"
      aria-pressed={pressed}
      onClick={() => onSelect(incident.incident_id)}
    >
      <span className="incident-id">{incident.incident_id}</span>
      <span className="incident-meta">
        First detected {incident.first_detection_at}
      </span>
      <span className="incident-meta">
        {incident.alert_count} alert{incident.alert_count === 1 ? "" : "s"} ·{" "}
        {incident.evidence_count} evidence ·{" "}
        {incident.affected_services.join(", ")}
      </span>
    </button>
  );
}
