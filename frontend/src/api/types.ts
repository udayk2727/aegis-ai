// Response shapes for the Day 9 read endpoints. They mirror the Pydantic models in
// backend/app/domain/*.py and backend/app/routers/demo.py. Keep them in step when
// the backend contract changes.

export interface DemoBaseline {
  run_id: string;
  scenario_id: string;
  seed: number;
  ticks: number;
  event_count: number;
  approval_method: string;
}

export interface DemoBaselineList {
  data_origin: "synthetic";
  items: DemoBaseline[];
  total: number;
}

export interface IncidentSummary {
  incident_id: string;
  status: "detected";
  root_cause_assessment: "not_assessed";
  source_run_id: string;
  first_detection_at: string;
  latest_detection_at: string;
  alert_count: number;
  affected_services: string[];
  affected_metrics: string[];
  evidence_count: number;
}

export interface IncidentPage {
  items: IncidentSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface SeriesRef {
  service: string;
  metric_name: string;
}

export interface VerifiedObservation {
  observation_id: string;
  kind: "anomalous_series" | "normal_series" | "onset_order";
  series: SeriesRef[];
  statement: string;
  source_event_ids: string[];
  source_event_count: number;
  first_at: string;
  last_at: string;
  source_events_truncated: boolean;
}

export interface Hypothesis {
  kind: string;
  rank: number;
  role: "potential_initiating_fault" | "downstream_symptom";
  statement: string;
  status: "supported" | "supported_with_contradictions";
  verification_status: "unverified";
  affected_services: string[];
  supporting_observation_ids: string[];
  contradicting_observation_ids: string[];
  supporting_event_ids: string[];
  supporting_event_count: number;
  alternative_kinds: string[];
  uncertainty: string;
  supporting_events_truncated: boolean;
}

export interface ContradictoryEvidence {
  hypothesis_kind: string;
  observation_id: string;
  explanation: string;
  source_event_ids: string[];
  source_event_count: number;
  source_events_truncated: boolean;
}

export interface MissingEvidence {
  kind: string;
  description: string;
  series: SeriesRef[];
  source_event_ids: string[];
  source_event_count: number;
  affects: string[];
  source_events_truncated: boolean;
}

export interface SuggestedCheck {
  kind: string;
  description: string;
  read_only: true;
  affects: string[];
}

export interface Investigation {
  investigation_id: string;
  analysis_version: string;
  source_incident_id: string;
  source_run_id: string;
  baseline_run_ids: string[];
  analysis_span_start: string;
  analysis_span_end: string;
  status: "analyzed" | "insufficient_evidence";
  root_cause_assessment: "not_assessed";
  ranking_basis: string;
  ranking_caveat: string;
  verified_observations: VerifiedObservation[];
  hypotheses: Hypothesis[];
  contradictory_evidence: ContradictoryEvidence[];
  missing_evidence: MissingEvidence[];
  suggested_checks: SuggestedCheck[];
  limitations: string[];
  evidence_truncated: boolean;
}

/** Structured error body from the incident-derived endpoints (403, 404, 500). */
export interface ApiErrorBody {
  code: string;
  message: string;
}
