import type {
  ContradictoryEvidence,
  Hypothesis,
  Investigation,
  MissingEvidence,
  SuggestedCheck,
  VerifiedObservation,
} from "../api/types";
import { labelize } from "../utils/format";

/**
 * One investigation report. The sections stay separate on purpose: what was
 * observed, how the candidates are ranked, what contradicts them, what is
 * missing, and the checks that would close the gap. Root cause is shown once,
 * as not assessed, and is never implied by the rank order.
 */
export function InvestigationReport({ report }: { report: Investigation }) {
  return (
    <article className="report">
      <dl className="summary">
        <div>
          <dt>Incident</dt>
          <dd>
            <code>{report.source_incident_id}</code>
          </dd>
        </div>
        <div>
          <dt>Source run</dt>
          <dd>
            <code>{report.source_run_id}</code>
          </dd>
        </div>
        <div>
          <dt>Analysis window</dt>
          <dd>
            {report.analysis_span_start} to {report.analysis_span_end}
          </dd>
        </div>
        <div>
          <dt>Analysis status</dt>
          <dd>{labelize(report.status)}</dd>
        </div>
        <div>
          <dt>Root cause</dt>
          <dd>
            {labelize(report.root_cause_assessment)}. The ranking below orders
            candidate explanations and does not name a cause.
          </dd>
        </div>
      </dl>

      {report.evidence_truncated && (
        <p className="notice">
          Some cited event lists are shortened. Each count gives the full total.
        </p>
      )}

      <HypothesesSection report={report} />
      <ObservationsSection observations={report.verified_observations} />
      <ContradictionsSection items={report.contradictory_evidence} />
      <MissingSection items={report.missing_evidence} />
      <ChecksSection checks={report.suggested_checks} />

      <section aria-labelledby="limitations-heading">
        <h3 id="limitations-heading">Limitations</h3>
        <ul>
          {report.limitations.map((limitation, index) => (
            <li key={index}>{limitation}</li>
          ))}
        </ul>
      </section>
    </article>
  );
}

function HypothesesSection({ report }: { report: Investigation }) {
  if (report.hypotheses.length === 0) {
    return (
      <section aria-labelledby="hypotheses-heading">
        <h3 id="hypotheses-heading">Ranked hypotheses</h3>
        <p>No hypothesis was generated for this incident.</p>
      </section>
    );
  }
  return (
    <section aria-labelledby="hypotheses-heading">
      <h3 id="hypotheses-heading">Ranked hypotheses</h3>
      <p className="muted">{report.ranking_basis}</p>
      <p className="caveat">{report.ranking_caveat}</p>
      <ol className="hypotheses">
        {report.hypotheses.map((hypothesis) => (
          <HypothesisItem key={hypothesis.kind} hypothesis={hypothesis} />
        ))}
      </ol>
    </section>
  );
}

function HypothesisItem({ hypothesis }: { hypothesis: Hypothesis }) {
  return (
    <li className="hypothesis">
      <p className="hypothesis-title">
        <span className="rank">Rank {hypothesis.rank}</span>{" "}
        <strong>{labelize(hypothesis.kind)}</strong>
      </p>
      <p>{hypothesis.statement}</p>
      <ul className="badges" aria-label="Hypothesis status">
        <li className="badge badge-unverified">
          {labelize(hypothesis.verification_status)}
        </li>
        <li className="badge">{labelize(hypothesis.status)}</li>
        <li className="badge">{labelize(hypothesis.role)}</li>
      </ul>
      <p>
        <strong>Uncertainty:</strong> {hypothesis.uncertainty}
      </p>
      <p className="muted">
        {hypothesis.supporting_observation_ids.length} supporting observation
        {hypothesis.supporting_observation_ids.length === 1 ? "" : "s"}
        {hypothesis.contradicting_observation_ids.length > 0 &&
          `, ${hypothesis.contradicting_observation_ids.length} contradicting`}
        {" · "}
        {hypothesis.affected_services.join(", ")}
      </p>
      <EventIds
        label="Supporting source events"
        ids={hypothesis.supporting_event_ids}
        total={hypothesis.supporting_event_count}
        truncated={hypothesis.supporting_events_truncated}
      />
    </li>
  );
}

function ObservationsSection({
  observations,
}: {
  observations: VerifiedObservation[];
}) {
  return (
    <section aria-labelledby="observations-heading">
      <h3 id="observations-heading">Verified observations</h3>
      {observations.length === 0 ? (
        <p>No observation was computed.</p>
      ) : (
        <ul className="stack">
          {observations.map((observation) => (
            <li key={observation.observation_id} className="item">
              <p>
                <span className="badge">{labelize(observation.kind)}</span>
              </p>
              <p>{observation.statement}</p>
              <p className="muted">
                {observation.series
                  .map((series) => `${series.service} · ${series.metric_name}`)
                  .join(" → ")}
              </p>
              <p className="muted">
                {observation.first_at} to {observation.last_at}
              </p>
              <EventIds
                label="Source events"
                ids={observation.source_event_ids}
                total={observation.source_event_count}
                truncated={observation.source_events_truncated}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ContradictionsSection({
  items,
}: {
  items: ContradictoryEvidence[];
}) {
  return (
    <section aria-labelledby="contradictions-heading">
      <h3 id="contradictions-heading">Contradictory evidence</h3>
      {items.length === 0 ? (
        <p>No contradicting observation was recorded.</p>
      ) : (
        <ul className="stack">
          {items.map((item, index) => (
            <li key={index} className="item">
              <p>
                <strong>{labelize(item.hypothesis_kind)}</strong>
              </p>
              <p>{item.explanation}</p>
              <EventIds
                label="Source events"
                ids={item.source_event_ids}
                total={item.source_event_count}
                truncated={item.source_events_truncated}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function MissingSection({ items }: { items: MissingEvidence[] }) {
  return (
    <section aria-labelledby="missing-heading">
      <h3 id="missing-heading">Missing evidence</h3>
      {items.length === 0 ? (
        <p>No missing evidence was recorded.</p>
      ) : (
        <ul className="stack">
          {items.map((item, index) => (
            <li key={index} className="item">
              <p>
                <span className="badge">{labelize(item.kind)}</span>
              </p>
              <p>{item.description}</p>
              <p className="muted">
                Affects {item.affects.map(labelize).join(", ")}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ChecksSection({ checks }: { checks: SuggestedCheck[] }) {
  return (
    <section aria-labelledby="checks-heading">
      <h3 id="checks-heading">Suggested checks</h3>
      {checks.length === 0 ? (
        <p>No check is suggested.</p>
      ) : (
        <ul className="stack">
          {checks.map((check, index) => (
            <li key={index} className="item">
              <p>
                <span className="badge">Read-only</span>{" "}
                <strong>{labelize(check.kind)}</strong>
              </p>
              <p>{check.description}</p>
              <p className="muted">
                Affects {check.affects.map(labelize).join(", ")}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/**
 * Event IDs behind a claim. The list is collapsed by default, and the summary
 * shows the full count even when the listed IDs are a shortened subset.
 */
function EventIds({
  label,
  ids,
  total,
  truncated,
}: {
  label: string;
  ids: string[];
  total: number;
  truncated: boolean;
}) {
  const listed = truncated ? `${ids.length} of ${total} listed` : `${total}`;
  return (
    <details className="event-ids">
      <summary>
        {label} ({listed})
      </summary>
      <ul>
        {ids.map((id, index) => (
          <li key={index}>
            <code>{id}</code>
          </li>
        ))}
      </ul>
    </details>
  );
}
