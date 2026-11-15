import type {
  DemoBaseline,
  IncidentPage,
  IncidentSummary,
  Investigation,
} from "../api/types";

export const BASELINE_A = "run-normal-301";
export const BASELINE_B = "run-normal-302";
export const INCIDENT_ID = "inc-0123456789abcdef";
export const OTHER_INCIDENT_ID = "inc-fedcba9876543210";

export function baseline(runId: string, seed: number): DemoBaseline {
  return {
    run_id: runId,
    scenario_id: "normal_operation",
    seed,
    ticks: 288,
    event_count: 864,
    approval_method: "synthetic_ground_truth",
  };
}

export function incidentSummary(
  incidentId: string,
  services: string[] = ["checkout-api"],
): IncidentSummary {
  return {
    incident_id: incidentId,
    status: "detected",
    root_cause_assessment: "not_assessed",
    source_run_id: "run-fault-311",
    first_detection_at: "2026-01-01T00:12:00Z",
    latest_detection_at: "2026-01-01T00:20:00Z",
    alert_count: 3,
    affected_services: services,
    affected_metrics: ["cpu_utilization"],
    evidence_count: 9,
  };
}

export function incidentPage(items: IncidentSummary[]): IncidentPage {
  return { items, total: items.length, limit: 50, offset: 0 };
}

/**
 * A report with one unverified CPU hypothesis. The shape follows the backend's
 * Investigation model, so the component test exercises the real field names.
 */
export function investigation(
  incidentId: string,
  statement = "CPU utilization stayed above the baseline band.",
): Investigation {
  return {
    investigation_id: "inv-0123456789abcdef",
    analysis_version: "test",
    source_incident_id: incidentId,
    source_run_id: "run-fault-311",
    baseline_run_ids: [BASELINE_A, BASELINE_B],
    analysis_span_start: "2026-01-01T00:00:00Z",
    analysis_span_end: "2026-01-01T00:20:00Z",
    status: "analyzed",
    root_cause_assessment: "not_assessed",
    ranking_basis: "Ranked by the number of supporting observations.",
    ranking_caveat: "Rank orders candidates. It is not a probability.",
    verified_observations: [
      {
        observation_id: "obs-cpu-anomalous",
        kind: "anomalous_series",
        series: [{ service: "checkout-api", metric_name: "cpu_utilization" }],
        statement,
        source_event_ids: ["evt-1", "evt-2"],
        source_event_count: 2,
        first_at: "2026-01-01T00:12:00Z",
        last_at: "2026-01-01T00:20:00Z",
        source_events_truncated: false,
      },
    ],
    hypotheses: [
      {
        kind: "cpu_resource_saturation",
        rank: 1,
        role: "potential_initiating_fault",
        statement: "The CPU series shows a sustained upward deviation.",
        status: "supported",
        verification_status: "unverified",
        affected_services: ["checkout-api"],
        supporting_observation_ids: ["obs-cpu-anomalous"],
        contradicting_observation_ids: [],
        supporting_event_ids: ["evt-1", "evt-2"],
        supporting_event_count: 2,
        alternative_kinds: [],
        uncertainty: "Host metrics were not in the telemetry model.",
        supporting_events_truncated: false,
      },
    ],
    contradictory_evidence: [],
    missing_evidence: [
      {
        kind: "not_in_telemetry_model",
        description: "Host-level metrics are not collected.",
        series: [],
        source_event_ids: [],
        source_event_count: 0,
        affects: ["cpu_resource_saturation"],
        source_events_truncated: false,
      },
    ],
    suggested_checks: [
      {
        kind: "review_host_metrics",
        description: "Review host CPU metrics for the same window.",
        read_only: true,
        affects: ["cpu_resource_saturation"],
      },
    ],
    limitations: ["Synthetic data only."],
    evidence_truncated: false,
  };
}
