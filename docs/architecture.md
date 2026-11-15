# AEGIS AI Architecture

**Status:** Draft. Describes the target architecture. Today the backend has the
foundation (`GET /`, `GET /health`), an in-memory simulation engine with
`POST /api/v1/simulations` and `GET /api/v1/events` (section 10), read-only
analytics and baselines (section 11), offline statistical anomaly detection
in the Python library (section 12), and offline incident correlation with
read-only `GET /api/v1/incidents` endpoints (section 13), and a read-only,
deterministic investigation report per incident (section 14). Everything else
below
is planned and will be refined as each phase is implemented.

## 1. Purpose

AEGIS AI is an Autonomous Enterprise Incident Intelligence Platform. It is
intended to:

1. Ingest operational signals such as alerts, logs, metrics, and tickets.
2. Correlate those signals into incidents.
3. Investigate likely root causes using enterprise data sources.
4. Recommend remediation, and execute it only after human approval.
5. Record every decision and action for audit.

## 2. Guiding principles

- **Human in the loop.** Automated analysis is welcome. Automated changes to
  production systems require explicit approval.
- **Auditability.** Every recommendation, approval, and action is traceable
  to an actor and a time.
- **Least privilege.** Each component holds only the credentials and
  permissions it needs.
- **Modular monolith first.** Start with one deployable backend organized
  into clear modules. Split into services only when there is a measured
  reason.
- **Replaceable intelligence.** LLM and data-source integrations sit behind
  interfaces so providers can change without touching domain logic.

## 3. Logical components

```
                 +-------------------+
  Signals  --->  |  Ingestion layer  |  (planned)
 (alerts,logs)   +---------+---------+
                           |
                           v
                 +-------------------+
                 | Correlation and   |  (planned)
                 | incident engine   |
                 +---------+---------+
                           |
                           v
                 +-------------------+        +-------------------+
                 |  Investigation    |  --->  |  LLM provider     |  (planned)
                 |  agents           |        |  (behind adapter) |
                 +---------+---------+        +-------------------+
                           |
                           v
                 +-------------------+
                 | Recommendation and|  (planned)
                 | approval gate    |  <--- human reviewer
                 +---------+---------+
                           |
                           v
                 +-------------------+
                 | Action executor   |  (planned, runs only after approval)
                 +-------------------+

  Cross-cutting: API layer (FastAPI, exists), audit log (planned),
  authentication and authorization (planned), observability (planned).
```

| Component              | Responsibility                                          | Status  |
|------------------------|---------------------------------------------------------|---------|
| API layer              | HTTP interface for the frontend and integrations        | Started |
| Simulation engine      | Synthetic labelled telemetry for testing detection      | Started |
| Statistical detection  | Z-score and robust detectors, held-out evaluation       | Started (library, offline) |
| Ingestion              | Accept and normalize alerts, logs, and metrics          | Planned |
| Correlation engine     | Group related signals into incidents                    | Started (offline, read-only API) |
| Investigation agents   | Gather context and propose candidate explanations       | Started (deterministic rules, read-only; section 14) |
| Approval gate          | Hold recommended actions until a human approves         | Planned |
| Action executor        | Perform approved remediation through audited adapters   | Planned |
| Audit log              | Append-only record of decisions and actions             | Planned |
| Data access adapters   | Oracle and other enterprise sources, behind interfaces  | Planned |
| Frontend               | Dashboard, investigation workspace, approval screens    | Started (local read-only dashboard; see the note below the table) |

The local dashboard in `frontend/` is React 19, TypeScript, and Vite. It is development
only. It reads the Day 9 demo endpoints through the Vite proxy on `/api/v1` and does not
start simulations or write anything. Its only inputs are `GET /api/v1/demo/baselines`,
`GET /api/v1/incidents`, and `GET /api/v1/incidents/{id}/investigation`, all with the same
`baseline_run_id` values. Root cause stays `not_assessed` in the view, and hypotheses are
shown as `unverified`. Approval screens and authentication are not built. The dashboard
adds no endpoint. Any future write or action endpoint still needs the authentication
design required by CLAUDE.md, security rule 6.

## 4. Backend structure

```
backend/
├── app/
│   ├── __init__.py        version constant
│   ├── main.py            application factory (composition root)
│   ├── domain/            telemetry models: no I/O, no framework imports
│   ├── services/          simulation, event store, analytics, detection, correlation,
│   │                      trusted baselines and offline validation
│   └── routers/           HTTP layer, one module per feature area
└── tests/                 pytest suite using the TestClient fixture
```

Planned additions, each as its own package:

- `app/core/`: configuration loaded from environment variables, logging setup
- `app/domain/`: incident, signal, and recommendation models (telemetry exists)
- `app/adapters/`: Oracle, LLM provider, and notification integrations
- `app/security/`: authentication and authorization dependencies

Dependency direction: routers depend on services, services depend on domain
and adapter interfaces, and nothing in `domain/` imports from the framework.
Shared state, such as the event store, is created in `create_app()` and
reached through a dependency, so each application instance has its own state.

## 5. Security architecture (planned)

| Concern          | Approach                                                       |
|------------------|----------------------------------------------------------------|
| Authentication   | Enterprise identity provider (OIDC) with short-lived tokens    |
| Authorization    | Role-based access; approval rights separated from investigation |
| Secrets          | Environment variables injected by a secrets manager; never in Git |
| Input validation | Pydantic models on every endpoint                              |
| Audit            | Append-only log of approvals and executed actions              |
| Transport        | TLS for all external and internal HTTP traffic                 |
| Data handling    | Minimize sensitive data in logs and LLM prompts                |
| Model output     | Treated as untrusted; validated before any action              |

No state-changing endpoint is added until the authentication and
authorization design is accepted.

**Investigation endpoint (as implemented).** `GET /api/v1/incidents/{id}/investigation`
is read-only and calls no external service, but it has no authentication and no
runtime guard. `main.py` registers it in every environment, and the environment
setting does not gate it. In production it returns 403 for any baseline request,
because trusted baselines cannot be loaded there. That is an indirect effect, not
a guard. A caller that builds the app with its own `TrustedBaselines` would expose
the endpoint in production. The endpoint is therefore a local-only operational
restriction, to be run only on a trusted local machine, until the authentication
design below is accepted.

**Investigation agent (owner decisions, Phases 1 and 2).** The LLM investigation
agent has no HTTP endpoint. It runs as a Python library and a local-only CLI. No
remotely accessible agent endpoint is permitted until an authentication and
authorization design is documented and approved here. Loopback access is not
authentication. The optional OpenAI adapter is for local development only. It
runs only with an explicit `--provider openai --live`, and it needs separate
authorization for any paid run. Production use is not approved.

**Open gap (as of the simulation engine).** `POST /api/v1/simulations` is
unauthenticated and local-development-only. It changes server state: it appends
a run to the in-memory event store, and it can evict other runs. The rule above
therefore applies to it, and no accepted design covers it yet. It is shipped
ahead of that decision at the owner's request, so it is a known exception and
not a precedent. No production authentication is implemented. Until the design
is accepted, the endpoint must not be exposed on any shared, staging, or
production host, and must run only on a trusted local machine.

Implemented guard: the endpoint is off by default and is registered only when
`AEGIS_SIMULATION_API_ENABLED=true`. `app/config.py` refuses that combination
with `AEGIS_ENVIRONMENT=production`, so the app does not start. Binding to
`127.0.0.1` limits reach to the local machine; it is not an authorization
control. Options for the owner:

1. Accept an interim design: a local-only switch, disabled by default outside
   development, with the write limited to ephemeral memory and no auth
   required. Document the switch and its default here.
2. Require authentication and authorization on the endpoint before any
   non-local use, as the rule states.
3. Remove the endpoint from the public API and keep the engine as a library
   until the design is accepted.

`GET /api/v1/events` and the simulation engine are read-only in effect and
need no authorization decision.

## 6. Data

The target database is Oracle. No connection code exists yet. The first
database design will define the incident, signal, recommendation, and audit
schemas. Connection details will come from environment configuration, and
wallet files will never be committed.

Today, simulated events are held in a bounded in-memory store
(`app/services/event_store.py`). It holds at most 50,000 events, evicts whole
runs oldest-first, and is lost on restart. It is a stand-in with the same
retrieval shape the database adapter will need, not a persistence design.

## 7. Deployment (planned)

- Phase 1 (current): run locally with `uvicorn` and `pytest`.
- Later: containerize the backend, add a local compose stack, then define
  Kubernetes manifests in `infrastructure/`.
- Health endpoint `GET /health` is designed for liveness and readiness probes.

## 8. Delivery phases

| Phase | Goal                                          | Status      |
|-------|-----------------------------------------------|-------------|
| 0     | Project foundation, health endpoint, tests    | In progress |
| 1     | Configuration, logging, and domain models     | Partial: telemetry models, simulation engine (section 10), analytics (section 11), detectors (section 12) |
| 2     | Signal ingestion and incident correlation     | Partial: correlation and incident API (section 13); ingestion planned |
| 3     | Authentication and authorization              | Planned     |
| 4     | Investigation agents with LLM adapter         | Partial: deterministic investigation (section 14); agent library with a fake provider, an optional OpenAI adapter not yet run live, and an optional Anthropic adapter with one recorded single-case live run (section 14.9) |
| 5     | Approval workflow and audit log               | Planned     |
| 6     | Controlled action execution                   | Planned     |
| 7     | Frontend dashboard and workspace              | Started: local read-only dashboard (`frontend/`); approval workflow not built |
| 8     | Containerization and Kubernetes deployment    | Planned     |

## 9. Open decisions

- Authentication provider and token model
- Message or queue technology for ingestion
- Hosting and secrets manager for each environment
- Whether the investigation layer uses a single model or a multi-agent design

## 10. Simulation engine and event contracts

Status: **Implemented, in memory.** The engine generates labelled synthetic
telemetry so later detection and correlation work has ground truth to score
against. Full usage, examples, and limitations are in the
[README](../README.md#simulation).

### Contracts

- Identifiers (`EventId`, `RunId`, `ServiceName`) are validated by pattern and
  length. Only `RunId` is derived by the engine; the others are checked at
  input.
- Timestamps must carry a UTC offset. A naive timestamp is rejected, and an
  offset other than UTC is converted to UTC.
- Metric values are finite and zero or greater. `NaN` and infinity are rejected.
- Each metric has one catalog entry (unit, event type, thresholds). An event
  whose unit, event type, or severity disagrees with the catalog is rejected.
- Ground-truth labels are validated: `normal_operation` can never be anomalous.
- Request bodies and query parameters reject unknown fields and out-of-range
  values with `422`. Responses declare `response_model`. No response contains
  stack traces, paths, or environment details.

### Design decisions

- **Determinism over randomness.** One `random.Random(seed)` per run, no clock
  reads, and a run ID hashed from the request. Identical requests yield
  identical runs, so tests need no mocks and results can be reproduced.
- **Paired scenarios.** Scenarios sharing a seed share baseline noise. Changes
  between them come from the injected fault only.
- **Window-level labels.** Simpler and honest about what the simulator knows.
  Per-metric labels would require the simulator to model each metric's causal
  effect, which is not implemented.
- **Idempotent creation.** Repeating a request returns `200` and stores nothing,
  so retries cannot double the event count.
- **Whole-run eviction.** A run is kept or dropped as a unit, so no query can
  show part of a run or labels without their events.
- **Bounded everywhere.** Ticks are capped at 288 (at most 2,592 events per
  run), page size at 500, and the store at 50,000 events. Each bound is
  enforced in the model or router, not left to the caller.
- **State on the app.** The store is created per application instance, which
  keeps tests isolated and avoids module-level mutable state.
- **Thread safety.** Endpoints run in FastAPI's thread pool, so the store
  guards its state with a lock.

### Known gaps (simulation)

- No persistence and no shared store between processes.
- Stationary baseline distributions; no seasonality or bursts.
- Abrupt step faults with no ramp.
- Fixed three-service topology, not configurable through the API.
- Linear scan for queries. Acceptable at this capacity; an index belongs with
  the database adapter.
- `POST /api/v1/simulations` is a state-changing endpoint without an accepted
  authorization design (see section 5).

## 11. Telemetry analytics and statistical baselines

Status: **Implemented, read-only, in memory.** Analytics turns stored telemetry
into descriptive statistics and normal-operation baselines. It exists before
any anomaly detector so that each later detector is scored against the same
ground truth and the same, documented baseline. Usage and examples are in the
[README](../README.md#analytics).

### Components

| Module | Responsibility | Framework imports |
|--------|----------------|-------------------|
| `app/services/statistics.py` | Quantiles, sample standard deviation, descriptive and rolling statistics | None |
| `app/services/analytics.py` | Series grouping by service, metric, and unit; single-series rolling analytics | None |
| `app/services/baseline.py` | Normal-labelled baseline selection and the train/evaluation split | None |
| `app/routers/analytics.py` | Three read-only endpoints and their Pydantic contracts | FastAPI, Pydantic |

The statistics modules are pure functions. The router converts their results
into response models and holds no statistical logic, so the numbers can be
tested without HTTP.

### Decisions

- **Sample standard deviation** (divisor n − 1). It is the estimator a
  detector needs when it generalizes from a sample. Population deviation would
  understate spread on small samples. It is `null` below two values, because a
  single value has no spread.
- **Linear quantiles** (R-7). This matches NumPy and pandas, so results can be
  checked against them.
- **Groups by (service, metric, unit).** A unit is part of the identity, so a
  mean can never combine milliseconds with percent.
- **Missing values are excluded and counted; invalid values are rejected.**
  Missing is `null`. NaN, infinity, booleans, and strings raise an error and are
  never silently coerced.
- **Rolling windows are trailing and incomplete windows carry `null`.** A
  partial window is not reported as a full one. Rolling statistics reject
  missing values rather than skipping them, because skipping would shift the
  window.
- **Rolling statistics require one series from one run.** A window across
  services or runs would mix unrelated timelines, so the service raises
  instead of computing it. The endpoint enforces this by filtering first.
- **Baselines use ground-truth labels only to select normal data.** The
  baseline carries no labels. Analytics responses never return a per-event
  label, but label isolation is partial: the summary's `is_anomalous` and
  `scenario_id` filters return label-derived aggregates. Labels are kept only
  where scoring needs them, in the evaluation side of the split.
- **Labels are never ML features.** Future inference must not use
  `is_anomalous`, `scenario_id`, or any value derived from them as an input.
  They select normal data and score predictions only.
- **Training and evaluation are separated by run and by time.** Runs must be
  disjoint. Training data is strictly before the cutoff, and evaluation data is
  at or after it. A UTC cutoff is required so the comparison is unambiguous.
  `TrainEvaluationSplit.training_baseline` builds the training reference from
  the training side only, so a held-out run cannot change it.
- **Exploratory analytics stay separate from evaluation.** The
  `/api/v1/analytics/baselines` endpoint uses every stored run by default and
  is for inspection. Evaluation workflows use `training_baseline` instead. The
  endpoint's contract is unchanged.
- **Bounded responses.** Summaries are bounded by the topology (at most nine
  series). Timeseries pages are capped at 500 points, the window at 60, and the
  baseline selection at 20 runs.
- **No thresholds.** Analytics computes statistics and makes no alert decision.

### Endpoints

| Method and path | Purpose | Bounds |
|-----------------|---------|--------|
| `GET /api/v1/analytics/summary` | Statistics per series, with filters including `is_anomalous` | Topology-bounded |
| `GET /api/v1/analytics/baselines` | Normal-only baseline with sample count and source run IDs | ≤ 20 `run_id`s |
| `GET /api/v1/analytics/timeseries` | One series from one run, with rolling statistics | Window 2–60; limit 1–500 |

Every endpoint is read-only and needs no authorization decision under section 5,
because it changes no state. Section 5 still applies to the simulation endpoint.

### Limitations

- Statistics are computed over the in-memory store, which is lost on restart.
- The baseline assumes stationary normal behaviour. The simulator has no
  seasonality, so a real baseline would need time-of-day or weekly structure.
- Labels cover each whole fault window, not each metric. Some normal-labelled
  events are affected by a fault in another metric, so the baseline is
  slightly contaminated. This is a known limitation of the simulator.
- Summaries and baselines scan the whole store, which is bounded at 50,000
  events. An index belongs with the database adapter.
- Label isolation is partial. The `is_anomalous` and `scenario_id` filters on
  the summary, and on `/api/v1/events`, let a caller learn label-derived counts
  and statistics. Restricting or removing those filters is an owner decision,
  not yet made.

### Visualization

`scripts/plot_telemetry.py` produces offline PNG charts under `artifacts/plots/`.
It uses pandas and Matplotlib, which are pinned in `requirements-analytics.txt`
rather than `requirements.txt`. They are optional because the API, its tests,
and its deployment never import them, so the core install stays free of numpy
and a plotting stack. The script calls the same services as the API. The charts
are not committed.

## 12. Statistical anomaly detection

Status: **Implemented as a Python library, offline.** Two detectors are fitted
on healthy training runs and scored on held-out runs, and the results are
evaluated at two levels. No API endpoint exists for detection. The proposed
read-only API is in section 12.7 and is **planned**. Usage is in the
[README](../README.md#anomaly-detection).

### 12.1 Components

| Module | Responsibility | Framework imports |
|--------|----------------|-------------------|
| `app/services/detection.py` | Interface `AnomalyDetector` (`fit`, `predict`), `Prediction`, `Decision`, `Reason`, `Direction`, and the shared unsupported-input rules | None |
| `app/services/detectors.py` | `ZScoreDetector` and `RobustDetector` with their configurations | None |
| `app/services/evaluation.py` | Confusion matrix, rates, metric-level and window-level evaluation, detection delay | None |
| `app/services/experiment.py` | `run_experiment`, the one sanctioned path from split to score | None |
| `scripts/evaluate_detectors.py` | Reproduces the held-out experiment with fixed seeds | None (standard library) |

No new dependency was added. The detectors use the statistics module from
section 11.

### 12.2 Interface contract

- `fit(training_events)` learns one model per series. Refitting replaces the
  previous model. A non-finite training value rejects the whole fit.
- `predict(evaluation_events)` returns one `Prediction` per event, in input
  order. It raises if the detector is unfitted.
- Each `Prediction` has a `decision` of `anomalous`, `normal`, or `unscored`.
  It also carries a `score`, which is `None` when no finite score exists, and a
  `reason` for unscored or zero-spread verdicts.
- Series are keyed by `(service, metric_name, unit)`, the same key as analytics.
- Predictions are point-wise. A verdict depends on the fitted model and the one
  event being scored. Scoring a prefix of the evaluation events gives the same
  verdicts as scoring all of them, so no later observation influences an
  earlier one.
- Detectors never read `label`. Labels are used before `fit`, to choose
  normal training events, and after `predict`, to score predictions.

### 12.3 Algorithms

**Z-score.** `z = (x − mean) / s`, where `mean` is the training mean and `s` is
the sample standard deviation (divisor n − 1). Default threshold 3.0.

**Robust.** `M = 0.6745 · (x − median) / MAD`, where `median` is the training
median and `MAD` is the median of the absolute deviations from that median.
The constant 0.6745 is the 0.75 quantile of the standard normal, so for normal
data MAD ≈ 0.6745 σ and `M` is on the same scale as a z-score. The default
threshold is 3.5, the Iglewicz and Hoaglin recommendation for `M`.

**Direction.** `two_sided` scores `|z|` or `|M|`. `upper_tail` scores the
signed value, so only values above normal are flagged. The threshold is
compared with `>=`, as the severity thresholds are (section 10).

**Ties.** Duplicate training values count at full multiplicity. The median of an
even sample is the mean of the two middle values. The MAD therefore uses
ordinary order statistics, with no tie-breaking rule. A worked example with
values `1, 1, 2, 2, 3, 3` gives median 2 and MAD 1.

### 12.4 Unsupported inputs and zero spread

Every rule below returns `unscored` or flags explicitly. None returns `normal`
by default.

| Condition | Verdict | Reason |
|-----------|---------|--------|
| Series not in training | `unscored` | `unseen_series` |
| Fewer than `min_training_samples` in training (default 10, minimum 2) | `unscored` | `insufficient_training_samples` |
| Non-finite evaluation value | `unscored` | `invalid_value` |
| Spread is zero (z-score: stdev 0; robust: MAD 0) and the value equals the center | `normal`, score 0 | none |
| Spread is zero and the value deviates in a counted direction | `anomalous`, no score | `zero_spread_deviation` |

The zero-spread rule is a policy. With no spread a standardized score is
undefined, so the detector cannot say how far a value is from normal. It flags
any deviation rather than dividing by zero or inventing a scale. Section 12.8
gives the consequence observed on the simulator.

Non-finite values are already rejected by the domain model (section 10). The
detector checks them again as a second line of defence, because the domain
model can be bypassed by constructing objects without validation.

### 12.5 Training versus evaluation

`run_experiment` is the only sanctioned pipeline:

1. `TrainEvaluationSplit.training_events` returns normal-labelled events from
   training runs strictly before the cutoff. This is the only place labels
   choose data for a detector.
2. `fit` receives those events and nothing else.
3. `TrainEvaluationSplit.evaluation_events` returns events from held-out runs at
   or after the cutoff.
4. `predict` runs on those events. Labels are not read.
5. The evaluation module reads labels and compares them with the predictions.

Training and evaluation runs are disjoint, and the split validates that. Tests
in `test_experiment.py` check that shifting every held-out value leaves the fitted
model unchanged, and that the training selection never includes a held-out run.
`test_detectors.py` checks that flipping every label leaves the fit and the
predictions unchanged.

### 12.6 Evaluation

**Two levels, reported separately.**

- *Metric level.* Each scored event is one decision. Ground truth is
  `label.is_anomalous`. Results are pooled and also broken down per series.
- *Window level.* Each evaluation run is one decision. A run is actually
  anomalous if it contains an anomalous label. Alerts come from an
  `IncidentRule` applied to the run's predictions (section 12.9). The default
  rule alerts on any anomalous prediction. A run is predicted anomalous if an
  alert falls inside its labelled window. For a normal run, any alert counts.
  The window is the range from the first to the last anomalous timestamp,
  inclusive. The window is derived from every labelled event in the run, scored
  or not, so it reflects the ground truth as the simulator recorded it.
- *Effect level.* Each scored event is anomalous only if it is inside a fault
  window and its series is one the fault changes (`AFFECTED_SERIES` in
  `evaluation.py`). This separates a detector miss from a metric the fault never
  moved. It is a reporting annotation and is never an input to a detector.

**Confusion matrix and rates.** `TP`, `FP`, `TN`, and `FN` are counted per
level. The rates are:

| Rate | Formula | Undefined when |
|------|---------|----------------|
| Precision | TP / (TP + FP) | Nothing was flagged |
| Recall | TP / (TP + FN) | Nothing was anomalous |
| F1 | 2TP / (2TP + FP + FN) | No positives on either side |
| False-positive rate | FP / (FP + TN) | No normal decisions |

An undefined rate is reported as `None` and never as 0 or 1. Where a rate is
defined, F1 equals the harmonic mean of precision and recall, and a test checks
that.

**Unscored events.** They are excluded from every confusion matrix and counted
in `unscored_count`. A run with no scored events is excluded and counted in
`unscored_run_count`. Neither is counted as normal.

**Detection delay.** For each detected anomalous run:

- *Window start* is the first anomalous timestamp of the run.
- *First alert* is the earliest `anomalous` prediction inside
  `[window start, window end]`. Alerts before the window do not count for
  detection, although they remain metric-level false positives.
- *Delay* is `first alert − window start`, in seconds. Its resolution is the
  tick interval, which is 60 seconds by default. A delay of 0 means the first
  anomalous tick was flagged.

The delay summary covers detected windows only. Undetected windows are visible
through `FN`.

### 12.7 Proposed read-only detection API (planned, not implemented)

The owner asked for an optional read-only API after the architecture review.
This section proposes it; no code exists.

- `GET /api/v1/detection/evaluation` runs the held-out experiment on stored
  runs and returns the `ExperimentReport` shape. Parameters are bounded:
  detector name from an enum, a threshold from a range, a direction from an
  enum, and a cutoff. The run IDs are enumerated and capped.
- It changes no state. The model is fitted per request, in memory, and then
  discarded.
- **Training is not exposed.** No endpoint accepts training data or stores a
  fitted model. Fitting through an unauthenticated public endpoint is excluded
  by section 5.
- It reads labels, so it inherits the label-isolation limitation in section 11.
  Its response must say that the metric-level truth is the window label.
- It is not registered until an authorization design is accepted, unless the
  owner decides it may ship as read-only under the existing read-only rule in
  section 5.

Until that decision, the detector is used as a Python service, through
`run_experiment` or the script.

### 12.8 Results on the simulator

This section records the first run, under the any-positive window policy. The
rule-based results, with the validation and test split, are in section 12.9.
Seeds 201 to 203 were retired after section 12.9 and are not used for any
decision. The frozen final results are in section 12.11.

`scripts/evaluate_detectors.py` runs four configurations on one split. The
training runs are four fault-free runs with seeds 101 to 104, ending at the
cutoff. The evaluation runs are fifteen runs with seeds 201 to 203, starting at
the cutoff: for each seed, one fault-free run and one run per fault scenario,
each 60 ticks. The thresholds were fixed before the experiment, from the
defaults above, and were not tuned on the held-out results.

| Configuration | Level | TP | FP | TN | FN | Precision | Recall | F1 | FPR |
|---------------|-------|---:|---:|---:|---:|----------:|-------:|---:|----:|
| z-score, two-sided | metric | 880 | 63 | 5877 | 1280 | 0.933 | 0.407 | 0.567 | 0.011 |
| z-score, two-sided | window | 12 | 3 | 0 | 0 | 0.800 | 1.000 | 0.889 | 1.000 |
| z-score, upper-tail | metric | 820 | 62 | 5878 | 1340 | 0.930 | 0.380 | 0.539 | 0.010 |
| robust, two-sided | metric | 927 | 288 | 5652 | 1233 | 0.763 | 0.429 | 0.549 | 0.048 |
| robust, upper-tail | metric | 867 | 288 | 5652 | 1293 | 0.751 | 0.401 | 0.523 | 0.048 |

The window rows are identical for all four configurations (12 of 12 fault runs
detected, 3 of 3 normal runs alerted). Detection delay was 0 s in every
detected window, so the first anomalous tick is always flagged. Three normal
runs give too few decisions to estimate a window-level false-positive rate.
Read that cell as "every normal run alerted", not as a rate.

What the numbers show:

- **Metric-level recall is capped by the labels.** The fault-window label covers
  every metric in the window, including metrics the fault does not change. For
  example, `cpu_utilization_percent` on `orders-db` has 240 anomalous events
  across the twelve fault runs and no alert in any run. None of the four fault
  scenarios moves that metric out of its normal range, so those 240 are false
  negatives by the label, not misses the detector could have caught.
- **The robust detector flags sparse counts.** `http_error_count` is zero in
  most normal ticks, so for those series the training MAD is 0. Under the
  zero-spread rule every nonzero count is flagged. That accounts for 259 of the 288 robust
  false positives. The z-score detector is less affected, but
  `db_connection_failures` contributes 27 of its 63 false positives for the same
  reason. Count metrics are a poor fit for a MAD-based detector.
- **Every normal run raised alerts, many of them.** Across the three fault-free
  runs, the z-score detector raised 63 false positives, about 21 per run, and the
  robust detector raised 288, about 96 per run. A per-event rate of about 1%
  looks small, but it is 540 events per run, so per-event rates understate the
  operational burden.

Owner decision needed: a zero-MAD fallback to the mean absolute deviation, as
Iglewicz and Hoaglin suggest, would reduce the count-metric false positives. It
is not implemented here. Changing the rule now would mean tuning on the
held-out data this section reports, so it needs an independent evaluation run
first.

### 12.9 Incident decision layer

**Why.** Under the any-positive policy, a run is an alert if any one of its
metric verdicts is anomalous. In the first run, each of the three healthy runs
had four to eight alarms. Most came from count metrics, where a Poisson count of
one or two can score above the threshold. One such alarm is enough, so the run-level
false-positive rate was 1.0.

**Design.** `app/services/incident.py` defines `IncidentRule(min_affected_series,
min_consecutive_ticks)`:

- A tick is *hot* when at least `min_affected_series` distinct series are
  anomalous in it.
- Consecutive means one expected interval apart, not adjacent in the data.
  `tick_interval_seconds` is the telemetry cadence. It defaults to the
  simulator's `DEFAULT_TICK_INTERVAL_SECONDS`, 60 s. A missing tick breaks the
  streak, so a gap starts a new count rather than being bridged. The check
  looks only at the previous tick, so the rule stays causal.
- A tick without a hot verdict breaks the streak. This includes a tick whose
  predictions are all unscored, because an unscored tick gives no evidence that
  a series is hot.
- An alert fires at each tick that extends a run of hot ticks to
  `min_consecutive_ticks` or more.
- `ANY_POSITIVE_RULE`, which is one series for one tick, is the original policy.
  Gaps and unscored ticks do not change its alerts.

The rule reads `Prediction` objects only: timestamp, series, and decision. It
cannot see a label. An alert at tick `t` depends only on ticks up to `t`, and a
test checks this by comparing truncated runs with the full run. A test also
checks that flipping labels leaves predictions and alerts unchanged.

The rule is passed to `evaluate_window_level` and `run_experiment`, both with the
any-positive default. The fitted model and the metric-level and effect-level
results do not depend on the rule. A test checks this.

**Detection delay.** The formula is unchanged. Delay is the first alert inside
the labelled window minus the window start. Alerts before the window do not
count for detection. The window bounds come from labels, and they select which
alerts are scored. They do not change when an alert fires. An alert time is the
tick where the rule fires, which uses only that tick and earlier ticks. The
window is taken from every labelled event in the run, scored or not, so an
unscored onset still anchors the delay. Alerts before onset are counted per run
as `pre_incident_alert_count` and reported separately. They never detect a
window.

**Calibration protocol.** No threshold or rule was tuned on the exploratory or
final runs.

| Split | Seeds | Runs | Use |
|-------|-------|-----:|-----|
| Training | 101–104 | 4 normal | Fit each detector. Before the cutoff. |
| Validation | 301–310 | 50 (10 normal, 40 fault) | Choose one rule per detector, then the preferred detector. |
| Exploratory (retired) | 201–203 | 15 (3 normal, 12 fault) | Viewed before the staging in 12.11. Not used for any decision. |
| Final | 401–420 | 100 (20 normal, 80 fault) | Score the frozen configurations once (section 12.11). |

The candidate grid is `min_affected_series` in {1, 2, 3} and
`min_consecutive_ticks` in {1, 2, 3, 5}. The selection rule was fixed before the
run. Among rules whose validation run-level false-positive rate is at most 0.10,
the rule with the highest validation window F1 is chosen. Ties go to the lower
false-positive rate, then to fewer consecutive ticks, then to fewer series.
`select_incident_rule` takes validation reports only. If no rule meets the cap,
it raises an error rather than relaxing the cap.

On validation, the any-positive policy alerted on all 10 normal runs for every
detector. The chosen rules alerted on none of them. The validation F1 of the
chosen rule was 1.000 for each detector.

| Detector | Chosen rule (validation) | Validation delay s |
|----------|--------------------------|-------------------:|
| z-score, two-sided | series ≥ 3, ticks ≥ 1 | 9.0 |
| z-score, upper-tail | series ≥ 3, ticks ≥ 1 | 9.0 |
| robust, two-sided | series ≥ 3, ticks ≥ 2 | 94.5 |
| robust, upper-tail | series ≥ 3, ticks ≥ 2 | 94.5 |

**Exploratory results, run level (seeds 201–203, retired).** These were viewed
before the staging in 12.11. They are not evidence for any choice. Each row is
one run-level decision: 12 fault runs and 3 normal runs.

| Detector | Policy | TP | FP | TN | FN | Precision | Recall | F1 | FPR | Delay s |
|----------|--------|---:|---:|---:|---:|----------:|-------:|---:|----:|--------:|
| z-score, two-sided | any-positive | 12 | 3 | 0 | 0 | 0.800 | 1.000 | 0.889 | 1.000 | 0.0 |
| z-score, two-sided | series ≥ 3, ticks ≥ 1 | 12 | 0 | 3 | 0 | 1.000 | 1.000 | 1.000 | 0.000 | 5.0 |
| z-score, upper-tail | any-positive | 12 | 3 | 0 | 0 | 0.800 | 1.000 | 0.889 | 1.000 | 0.0 |
| z-score, upper-tail | series ≥ 3, ticks ≥ 1 | 12 | 0 | 3 | 0 | 1.000 | 1.000 | 1.000 | 0.000 | 5.0 |
| robust, two-sided | any-positive | 12 | 3 | 0 | 0 | 0.800 | 1.000 | 0.889 | 1.000 | 0.0 |
| robust, two-sided | series ≥ 3, ticks ≥ 2 | 12 | 0 | 3 | 0 | 1.000 | 1.000 | 1.000 | 0.000 | 125.0 |
| robust, upper-tail | any-positive | 12 | 3 | 0 | 0 | 0.800 | 1.000 | 0.889 | 1.000 | 0.0 |
| robust, upper-tail | series ≥ 3, ticks ≥ 2 | 12 | 0 | 3 | 0 | 1.000 | 1.000 | 1.000 | 0.000 | 125.0 |

**Exploratory results, event level.** The rule does not change event-level results, so
each detector's labelled and affected rows are the same under both policies.

| Detector | Level | TP | FP | TN | FN | Precision | Recall | F1 | FPR |
|----------|-------|---:|---:|---:|---:|----------:|-------:|---:|----:|
| z-score, two-sided | labelled | 880 | 63 | 5877 | 1280 | 0.933 | 0.407 | 0.567 | 0.011 |
| z-score, two-sided | affected | 863 | 80 | 7000 | 157 | 0.915 | 0.846 | 0.879 | 0.011 |
| z-score, upper-tail | labelled | 820 | 62 | 5878 | 1340 | 0.930 | 0.380 | 0.539 | 0.010 |
| z-score, upper-tail | affected | 803 | 79 | 7001 | 217 | 0.910 | 0.787 | 0.844 | 0.011 |
| robust, two-sided | labelled | 927 | 288 | 5652 | 1233 | 0.763 | 0.429 | 0.549 | 0.048 |
| robust, two-sided | affected | 865 | 350 | 6730 | 155 | 0.712 | 0.848 | 0.774 | 0.049 |
| robust, upper-tail | labelled | 867 | 288 | 5652 | 1293 | 0.751 | 0.401 | 0.523 | 0.048 |
| robust, upper-tail | affected | 805 | 350 | 6730 | 215 | 0.697 | 0.789 | 0.740 | 0.049 |

What the numbers show:

- **The run-level false-positive rate falls from 1.000 to 0.000 on test.** The
  test set has only three normal runs, so this is zero of three, not a rate
  estimate. The validation set gives the stronger evidence: zero of ten normal
  runs alerted under the chosen rule, against ten of ten under the original
  policy.
- **Event-level false positives are unchanged.** The rule acts on runs, not on
  events. About 1% of healthy events still fire, and count metrics still produce
  them. This matters for any operator who sees per-event alerts.
- **Persistence costs detection latency.** The any-positive delay of 0 s means
  the first alarm came in the first tick of the fault. The chosen rules must wait
  for persistence, so the mean delay over the twelve detected windows is 5 s for
  the z-score detector and 125 s for the robust detector.
- **Affected recall is higher than labelled recall.** Against the labels, the
  z-score detector's recall is 0.407. Restricted to series the fault changes, it
  is 0.846. The gap is the labels marking unaffected metrics anomalous, not
  missed detections.

**Known gaps in this layer.**

- Validation and test use the same simulator. They show behaviour on the
  simulator's stationary data with step faults, not on production telemetry.
- Pre-window alerts in fault runs are counted per run in `pre_incident_alert_count`
  and reported in section 12.11. They are not detections.
- Only the first alert of each run is scored. The number of alerts per run, which
  an operator would see as an alert storm, is not reported.
- A streak can begin before the fault window and continue into it. The in-window
  delay would then understate the true delay from fault onset. No exploratory
  detection had delay 0, so this did not occur there. It has not been measured
  on the final runs.
- The affected-series table is written by hand from the simulator's fault
  effects. A one-off check over six seeds of 240 ticks found that every listed
  series moved its in-window mean by at least 2.5 pooled standard deviations, and
  every unlisted series by under 0.1. No test enforces this, so a simulator change
  could leave the table stale.
- The grid and the selection cap were chosen by the author. Other choices could
  select a different rule.

### 12.10 Assumptions and limitations

- **Stationarity.** Each series is assumed to have one stable distribution over
  training and evaluation. Seasonality and drift break this, and the simulator
  has neither.
- **Independence.** Points are scored one at a time. Neighbouring ticks are
  correlated in real systems, which a point detector ignores.
- **Distribution shape.** The z-score assumes roughly symmetric, light-tailed
  data. Counts are discrete and skewed; latencies are right-skewed. The robust
  detector is less sensitive to tails but still assumes a unimodal core.
- **Small samples.** The default minimum of 10 training values is a floor, not
  an estimate of adequacy. Confidence in the mean, stdev, median, and MAD is low
  for small samples.
- **Labels.** Window labels mark every metric in the fault window. Metric-level
  truth is therefore a scenario-window label, not a per-metric effect.
  Metric-level precision and recall measure agreement with that label.
- **Simulator scope.** The results describe synthetic, stationary data with step
  faults. They do not show performance on production telemetry.
- **Scale.** Scoring is linear in events, with per-series lookup. Fitting is
  linear in training events. Both run in memory, bounded by the 50,000-event
  store (section 10).

### 12.11 Model selection and final evaluation

**Selection, validation only.** The two upper-tail detectors are the candidates.
The upper-tail direction was fixed before this step: a drop in latency or error
counts is not an incident (`Direction`). Each candidate uses the incident rule
chosen for it on validation. `select_detector` ranks candidates by validation
window F1, then by mean validation detection delay. Test and final results are
not an input.

| Detector | Validation rule | Validation F1 | Validation delay s |
|----------|-----------------|--------------:|-------------------:|
| z-score, upper-tail | series ≥ 3, ticks ≥ 1 | 1.000 | 9.0 |
| robust, upper-tail | series ≥ 3, ticks ≥ 2 | 1.000 | 94.5 |

The z-score upper-tail is preferred. Validation F1 is tied, and its delay is
shorter. The robust upper-tail is kept as a comparison model. The two-sided
z-score also reaches validation F1 1.000 at 9.0 s under the same rule. It was
not a candidate, because the direction was fixed above.

The gap rule in 12.9 did not change any validation number. The simulator emits
every metric on every tick, so the rule's gap check is exercised only by unit
tests.

**Frozen configuration.** Nothing below was changed after the final run.

- Preferred: `ZScoreDetector`, threshold 3.0, upper-tail, rule series ≥ 3 and
  ticks ≥ 1.
- Comparison: `RobustDetector`, threshold 3.5, upper-tail, rule series ≥ 3 and
  ticks ≥ 2.
- Baseline: the any-positive rule, for both detectors.
- Training: seeds 101–104, fault-free, before the cutoff. Final: seeds 401–420,
  one normal and four fault runs per seed, 100 runs, 54,000 held-out events.

**Final results, run level, seeds 401–420 (scored once).** Each row is one
run-level decision: 80 fault runs and 20 normal runs.

| Detector (rule) | TP | FP | TN | FN | Precision | Recall | F1 | FPR | Mean delay s | Pre-incident alerts | Missed incidents |
|-----------------|---:|---:|---:|---:|----------:|-------:|---:|----:|-------------:|--------------------:|-----------------:|
| z-score upper-tail (series ≥ 3, ticks ≥ 1) | 80 | 0 | 20 | 0 | 1.000 | 1.000 | 1.000 | 0.000 | 11.2 | 0 runs | 0 of 80 |
| robust upper-tail (series ≥ 3, ticks ≥ 2) | 80 | 0 | 20 | 0 | 1.000 | 1.000 | 1.000 | 0.000 | 126.8 | 0 runs | 0 of 80 |
| any-positive baseline (z-score) | 80 | 20 | 0 | 0 | 0.800 | 1.000 | 0.889 | 1.000 | 0.0 | not reported | 0 of 80 |
| any-positive baseline (robust) | 80 | 20 | 0 | 0 | 0.800 | 1.000 | 0.889 | 1.000 | 0.0 | not reported | 0 of 80 |

Delays are whole multiples of the 60 s tick. The z-score mean of 11.25 s is 15
ticks over 80 detections, so at most 15 windows had any delay and at least 65
were detected at onset. The robust rule needs two hot ticks, so every
detection there has delay of at least 60 s.

Event level, final runs (for context; the rule does not affect it):

| Detector | Level | TP | FP | TN | FN | Precision | Recall | F1 | FPR |
|----------|-------|---:|---:|---:|---:|----------:|-------:|---:|----:|
| z-score upper-tail | labelled | 5456 | 435 | 39165 | 8944 | 0.926 | 0.379 | 0.538 | 0.011 |
| z-score upper-tail | affected | 5358 | 533 | 46667 | 1442 | 0.910 | 0.788 | 0.844 | 0.011 |
| robust upper-tail | labelled | 5759 | 1984 | 37616 | 8641 | 0.744 | 0.400 | 0.520 | 0.050 |
| robust upper-tail | affected | 5372 | 2371 | 44829 | 1428 | 0.694 | 0.790 | 0.739 | 0.050 |

**What these results can and cannot support.**

- Zero false positives in 20 normal runs leaves room for a real run-level FPR of
  up to about 14% at 95% confidence. Zero misses in 80 fault runs leaves room
  for a miss rate of up to about 4%.
- Every fault in the simulator is a large, step change. Perfect run-level scores
  are expected from that design. They say little about subtle faults.
- The run-level result is strong, but the event-level results are not. The
  z-score detector still raises about 1% false positives per event, and the
  robust detector about 5%. The incident rule only suppresses run-level alarms.
- The final seeds come from the same simulator as validation. They are a check
  of repeatability, not of transfer to production telemetry.

**Limitations.**

- The selected rule grid and the 10% run-level cap were chosen by the author
  before validation. Other choices could select a different rule.
- The gap rule has no effect on any reported number, because the simulator does
  not drop ticks. Its behaviour is covered only by unit tests.
- The training model is fitted once on four normal runs and never updated.

## 13. Incident correlation and evidence

Status: **Implemented, read-only, computed on request from the in-memory store.**
Correlation groups the alerts of one simulation run into incidents, attaches
the stored observations that support each incident, and builds a timeline. It
uses the Day 4 incident rule unchanged and makes no root-cause claim.

### 13.1 Components

| Module | Responsibility |
|--------|----------------|
| `app/domain/incident_records.py` | Pydantic contracts: `Incident`, `IncidentSummary`, `EvidenceRecord`, `TimelineEntry`, `TelemetryGap`, `CorrelationReason`, `BaselineComparison` |
| `app/services/topology.py` | Declared service calls: `api-gateway -> orders-service -> orders-db` |
| `app/services/correlation.py` | Alert construction, the grouping rule, and the correlation rationale |
| `app/services/evidence.py` | Read-only evidence collection and telemetry gap detection |
| `app/services/timeline.py` | Deterministic timeline of observations and detection decisions |
| `app/services/incident_pipeline.py` | Offline pipeline: runs in, incident records out, no I/O |
| `app/services/baseline_trust.py` | Label-free registry of approved baselines. Required by `build_incidents` (13.12) |
| `app/services/trusted_baselines.py` | Loads and writes the local manifest, re-verifying every entry (13.12) |
| `app/services/offline_validation.py` | Label-aware check that a synthetic run has no injected fault. Offline and startup only (13.12) |
| `app/services/correlation_validation.py` | Label-aware offline scoring of the window against synthetic truth (13.13) |
| `scripts/approve_baselines.py`, `scripts/evaluate_correlation_window.py` | Offline workflows that write the manifest and print the window sweep (13.12, 13.13) |
| `app/routers/incidents.py` | Read-only HTTP endpoints; validation and safe errors only |

`app/services/incident.py` (the Day 4 rule) and `app/services/detectors.py` are
not modified. The pipeline calls `IncidentRule.alert_times` and the detector's
`predict` as they are.

### 13.2 Pipeline

1. **Baseline.** One detector is fitted on the events of the caller-named
   baseline runs. Each named run must be on the trusted list (13.12). The
   pipeline does not read labels, so it does not check that a run is fault-free.
   The API enforces trust before calling the pipeline. The selected detector is
   `ZScoreDetector` with upper-tail direction, as in the Day 4 validation.
2. **Scoring.** Every other stored run is scored against that fitted model.
   Baseline runs are never scored.
3. **Alerts.** The selected rule, `IncidentRule(min_affected_series=3,
   min_consecutive_ticks=1)`, gives the alert ticks of each run. Each alert is
   the set of series that were anomalous at its tick.
4. **Grouping.** Within one run, alerts are grouped as described in 13.3.
5. **Evidence and timeline.** Each group gets evidence (13.4), the telemetry
   gaps that overlap its span, a timeline (13.5), and a deterministic ID (13.6).

### 13.3 Correlation rules

Two alerts in the same run are linked when both hold:

1. **Temporal proximity.** They are at most `window_seconds` apart, inclusive.
   The default is 600 seconds (ten one-minute ticks).
2. **Service relation.** Their services are equal, or one calls the other in the
   declared topology. `api-gateway` and `orders-db` are not directly related, so
   alerts on only those two services stay apart even when close in time.

An incident is a connected component of the link graph. Links are transitive, so
a chain of related alerts can span more than one window. Alerts from different
runs are never linked.

Determinism: alerts are sorted by time before grouping, and groups are ordered
by their first alert, so input order does not change the result. A repeated
prediction for the same tick and series counts once.

### 13.4 Evidence contract

Each `EvidenceRecord` has:

- `source_event_id`, `run_id`, `service`, `metric_name`, `unit`, `severity`,
  `observed_at`, `observed_value`. These are copied from the stored event that
  the detector scored, so no value is invented.
- `inclusion_reason` separates three things, and only the first is a qualifying
  detection:
  - `contributed_to_alert`: the observation's tick was an alert tick. This is a
    qualifying detection.
  - `anomalous_in_window`: an anomalous measurement after the first qualifying
    detection that did not qualify an alert.
  - `pre_incident_context`: an anomalous sample before the first qualifying
    detection. It is context only, often noise, and not evidence that the
    incident was already under way or of its cause.
- `baseline_comparison`: detector, direction, threshold, score, and the baseline
  run IDs. It is `null` for a zero-spread deviation, which has no finite score.

The span of an incident is `[first alert - window, last alert + window]`. Its
evidence is the anomalous observations of the series that took part in its
alerts, sorted by observation time and then by series.

Only `contributed_to_alert` records show degradation. They set
`first_observation_at`, `latest_observation_at`, and the timeline. An
`anomalous_in_window` record is context. It can precede onset or follow the last
alert, so it stays in `evidence` but is never reported as supporting evidence.

### 13.5 Timeline

Each `TimelineEntry` has a `kind` that separates measurements from decisions:

| Kind | Meaning | Milestones |
|------|---------|------------|
| `observation` | A stored telemetry sample taken at an alert tick (context samples are excluded) | earliest supporting observation; related metric change (first alert-tick sample per series); latest observed evidence |
| `detection_decision` | The rule fired at a tick, using data up to and including that tick | first qualifying detection |
| `telemetry_gap` | No observation between two stored timestamps | telemetry gap |

### 13.6 Identity and status

- `incident_id` is `inc-` followed by 16 hex digits. It is a SHA-256 hash of the
  source run, detector, rule, baseline run IDs, first alert time, and series.
  The same inputs always give the same ID.
- `status` is `detected` for every incident. It means the rule fired. It says
  nothing about triage, resolution, or cause.
- `root_cause_assessment` is `not_assessed` for every incident. No field holds a
  root cause, and the validator rejects any other value.

### 13.7 Timestamp semantics

| Field | Meaning |
|-------|---------|
| `first_observation_at` | Time of the earliest alert-contributing sample. Alert-contributing samples sit at alert ticks, so this equals the first alert tick |
| `first_detection_at` | Tick at which the rule first fired (a decision) |
| `latest_detection_at` | Tick of the last alert in the group (a decision) |
| `latest_observation_at` | Time of the latest supporting sample |

The model requires `first_observation_at <= first_detection_at <=
latest_detection_at <= latest_observation_at`. The simulator has no alert
delay, so a decision is stamped at the tick whose data it used.

### 13.8 Endpoints

| Method and path | Purpose |
|-----------------|---------|
| `GET /api/v1/incidents` | Page of incident summaries, ordered by first detection, then ID |
| `GET /api/v1/incidents/{incident_id}` | One incident with evidence, timeline, and gaps |

Both endpoints require `baseline_run_id` (1 to 8, repeatable). On the list,
`limit` is 1 to 200 with a default of 50, `offset` is at least 0, and `run_id`
keeps only one run's incidents. The detail response is not paginated. Its
evidence holds at most one record per series per tick, so it is bounded by the
run: 288 ticks times 9 series at most. Each request recomputes every incident
from the store, which takes about 0.5 s at the 50,000-event capacity.

Errors have a fixed `code` and no internal detail:

| Status | Code | Meaning |
|-------:|------|---------|
| 403 | `baseline_not_approved` | A named baseline is not on the trusted list. Checked before the store |
| 404 | `baseline_run_not_found` | An approved baseline is not in the event store |
| 404 | `incident_not_found` | No incident with that ID for the given baselines |

Responses never include stack traces or internal paths. Requests cannot approve
or change baselines. The trusted list comes only from the offline manifest (13.12).

### 13.9 Correlation is not causation

Correlation here records co-occurrence in time and in a declared dependency
graph. It does not establish that one service caused another. Every incident
carries a `not_causal` rationale item that says so, and `root_cause_assessment`
stays `not_assessed`. A future investigation step must add its own evidence
before any cause is stated.

### 13.10 Limitations

- **Synthetic telemetry.** Every fault is a large, step-shaped change, so the
  correlation rules are tested on easy cases. The simulator has no seasonality,
  no ramps, and no partial outages.
- **Window chosen after inspection.** The 600-second default was picked after
  looking at simulator runs with seeds 201 to 205. It is retained. It was not
  retuned on held-out results. Section 13.13 gives the synthetic sweep, the
  error types, and the risk of false merges and false splits.
- **Context evidence.** The evidence list can start before onset and end after
  the last alert. On seeds 501 to 520, 35 of 80 fault incidents had
  `pre_incident_context` samples. Most were `http_error_count` and
  `db_connection_failures`, both Poisson error counts with a near-zero baseline.
  Context never sets observation times or the timeline. The detector's flagging
  of single low counts is a detector limitation, not a correlation one.
- **Chaining.** Transitive links can join two faults a few windows apart if
  alerts keep appearing in between.
- **Baseline trust.** A fault-containing baseline would teach the detector that
  the fault is normal. The API now accepts only approved baselines (13.12). The
  pipeline still does not check labels, so a direct caller of `build_incidents`
  can pass any run. The production trust source is undecided.
- **No persistence.** Incidents are recomputed on every request from the
  in-memory store. A restart empties the store and removes the incidents. Each
  request also rescans all stored runs. This is acceptable at the 50,000-event
  capacity; an index belongs with the database adapter.
- **Unauthenticated read.** The endpoints are read-only, but they share the
  development-only exposure of the simulation endpoints. They expose simulated
  data only.
- **Telemetry gaps** are covered by unit tests only. The simulator never drops
  ticks. Gaps are detected per run and per tick. A single series missing at one
  tick, while the rest of that tick arrives, is not reported as a gap.
- **Duplicate events.** A run that repeats an event ID is refused with
  `ValueError`. The API cannot ingest events, so this only applies to direct
  callers of the pipeline.

### 13.11 Planned consumption by AI agents (not implemented)

Investigation agents will read an incident's evidence and timeline. Each
`source_event_id` lets an agent cite the exact observation it relies on, and
each `correlation_rationale` item states what the grouping does and does not
show. Agent output stays untrusted under section 5: any claimed cause must be
checked against the cited evidence before it is shown as a finding.

### 13.12 Trusted baselines

**Problem.** The pipeline fits its detector on whatever baseline runs a request
names. A fault-containing baseline would teach the detector that the fault is
normal, and nothing would report it.

**Decision (owner, Option A).** Only explicitly approved baseline runs may be used.
Approval is offline, from ground-truth labels, and it is recorded with its
provenance. Requests cannot approve or change a baseline.

**Components.**

- `offline_validation.py` holds the label-aware check. `validate_synthetic_baseline`
  rejects a run whose scenario is a fault, or that has any anomalous label. It
  records the run ID, spec, check method, and event count.
- `scripts/approve_baselines.py` simulates candidate normal specs, runs the check,
  and writes a manifest to `local/trusted_baselines.json` by default. If any
  candidate is faulty, it writes nothing and exits non-zero.
- `baseline_trust.py` holds the registry, `TrustedBaselines`. It contains no label
  logic. Its only job is membership: `is_approved(run_id)`. Its default value
  trusts nothing.
- `trusted_baselines.py` loads the manifest. `load_trusted_baselines` re-simulates
  every entry, re-runs the check, and confirms each recorded run ID matches its
  spec. Any failure refuses the whole file. There is no `trusted=true` flag.
- `build_incidents` takes `trusted` as a required keyword argument, with no
  default. It refuses any baseline the registry does not approve, even when the
  run is stored and well formed. The API therefore cannot reach the pipeline
  with an unapproved baseline, and neither can a direct caller.
- `AEGIS_TRUSTED_BASELINES_PATH` names the manifest. Settings refuse it in
  production. Without it, the registry is empty, so every incident request that
  names a baseline is refused with `403 baseline_not_approved`.
- The router checks approval before it reads the store. An approved run that is
  not stored gives `404 baseline_run_not_found`.
- Test code uses the same offline check through `trusted_baselines()` in
  `tests/incident_factories.py`, so tests never build an approval by hand.

**Manifest format (version 1).**

```json
{
  "version": 1,
  "baselines": [
    {
      "run_id": "sim-...",
      "method": "synthetic_ground_truth",
      "checked_event_count": 2592,
      "spec": {"scenario": "normal_operation", "seed": 101, "ticks": 288}
    }
  ]
}
```

The `spec` object also records `start_time` and `tick_interval_seconds`.

**Label boundary.** Label-aware code runs only offline and at startup, when the
manifest is loaded. No request path reads labels. `test_label_isolation.py` checks
detection, correlation, the pipeline, and the router. It fails on any label read
or label import. It also follows the application's own imports, so it fails if
any of them reaches the offline modules, directly or indirectly.

**Limits.**

- Synthetic validation shows that a run has no injected fault. It does not show
  that the run is representative of production traffic. The simulator's normal
  behaviour is stationary.
- The manifest is for local development. There is no production trust source. A
  production design needs the authentication design in section 5 and an approval
  workflow (phase 5 in section 8).
- The store is in memory. After a restart, the manifest is reloaded, but the
  baseline runs must be created again from the same specs.

### 13.13 Correlation window and error types

**Status.** The 600-second window is provisional. It was retained for Day 5 and
was not retuned on held-out results.

**Error types**, scored per window on synthetic runs with known faults
(`app/services/correlation_validation.py`):

- **Missed fault.** A fault run has no incident that alerts inside its fault window.
- **False split.** A single fault is cut into more than one incident. Each
  incident beyond the first that alerts in the fault window counts once.
- **False merge.** One incident mixes alerts inside the fault window with alerts
  outside it. Unrelated noise was joined to the fault.
- **False incident.** An incident has no alert inside the fault window. Every
  incident in a normal run is one.

**Synthetic result.** Baselines are seeds 101 to 104, normal runs at 60 ticks.
Evaluation is seeds 501 to 520 (80 fault runs and 20 normal runs). Seeds 201 to
205 were used to choose the default. Seeds 401 to 420 are the held-out set, and
this sweep does not use them.

| Window (s) | Missed | Splits | Merges | False incidents | Incidents |
|-----------:|-------:|-------:|-------:|----------------:|----------:|
| 60 | 0 | 126 | 0 | 0 | 206 |
| 300 | 0 | 1 | 0 | 0 | 81 |
| 600 | 0 | 0 | 0 | 0 | 80 |
| 900 | 0 | 0 | 0 | 0 | 80 |
| 1200 | 0 | 0 | 0 | 0 | 80 |

Reproduce with `python scripts/evaluate_correlation_window.py`. The output is
deterministic.

**What this does and does not show.**

- The window matters most at the short end. A 60-second window splits faults.
  The 300 to 1200 second range gave the same result here, so the simulator does
  not separate 600 seconds from its neighbours.
- **False merging is possible in production.** A window that is too long can join
  an unrelated alert to a real fault, and transitive links can chain faults that
  are several windows apart. The simulator cannot show this. It has one fault per
  run, and the only merge it could produce is noise joining a fault, which did not
  happen here.
- **False splitting is possible in production.** When a rule stops firing for
  longer than the window, one fault becomes two incidents. Real telemetry with
  gaps, drift, or intermittent alerts is more likely to show this than the
  simulator.
- The result is conditional on the simulator. Its faults are large step changes,
  and its normal behaviour is stationary. Check the window again on real
  telemetry, with held-out runs chosen before the check.

## 14. Evidence-based investigation

Status: **Implemented, read-only, deterministic, offline.** For one incident, the
investigation separates verified observations from hypotheses, records
contradictory and missing evidence, and suggests read-only checks. It calls no
language model, writes no state, and sends no telemetry anywhere. Usage is in the
[README](../README.md#investigation). The LLM investigation agent (section 14.9) is a
separate, optional layer over these reports. Its default provider makes no network
call. An optional OpenAI adapter exists for local development only, and it has not
been run against the live API. An optional Anthropic adapter has one recorded
single-case live run (section 14.9).

### 14.1 Components

| Module | Responsibility | Labels |
|--------|----------------|--------|
| `app/domain/investigation_records.py` | Pydantic contracts and the cross-reference validator | Never read |
| `app/services/investigation_catalog.py` | Fixed hypothesis rules, source gaps, and check descriptions | Never read |
| `app/services/investigation_observations.py` | Verified observations from the incident and the source-run verdicts | Never read |
| `app/services/hypotheses.py` | Rule application, contradictions, missing evidence, ranking | Never read |
| `app/services/investigation.py` | Report assembly, missing evidence, suggested checks, deterministic ID | Never read |
| `app/services/investigation_evaluation.py` | Offline scoring against simulator labels | Reads labels; offline only |
| `app/services/incident_pipeline.py` (`score_run`) | Verdicts for one source run, from the same fit as `build_incidents` | Never read |
| `app/routers/incident_access.py` | Baseline trust and store snapshot, shared with the incident endpoints | Never read |
| `app/routers/investigations.py` | `GET /api/v1/incidents/{incident_id}/investigation` | Never read |
| `app/services/investigation_provenance.py` | Provenance checks, run by the API before a report is returned | Never read |
| `app/domain/agent_records.py` | Agent contracts: claims, evidence references, tool requests, verification status | Never read |
| `app/services/agent_tools.py` | Allowlisted read-only tools over one report, and canonical record text | Never read |
| `app/services/agent_verification.py` | Deterministic verifier against the canonical report | Never read |
| `app/services/agent_orchestration.py` | Bounded tool-calling loop | Never read |
| `app/services/agent_provider.py`, `agent_fake_provider.py` | Provider interface, and the deterministic fake provider | Never read |
| `app/services/agent_openai_provider.py` | Optional OpenAI Responses adapter, local development only | Never read |
| `app/services/agent_budget.py` | Run budgets, token reserve, pricing file, and cost estimate | Never read |

The label-isolation test (`test_label_isolation.py`) covers every module above
except the evaluator. It fails on any label read or label import, and it fails if
a detection-side module reaches an offline module through any chain of imports.

### 14.2 Pipeline

1. The request takes one snapshot of the store. The incident is built from it,
   and so are the verdicts for its source run. Both come from the same snapshot,
   so they refer to the same data.
2. Verified observations are built from the incident's evidence and the verdicts.
3. Each fixed rule is applied to the observations. The rule either generates a
   hypothesis or records why it did not.
4. Missing evidence, contradictions, and suggested checks are assembled.
5. The report gets a deterministic ID, `inv-` followed by 16 hex digits of a
   SHA-256 hash of the incident ID and the analysis version.
6. Cited event lists are capped (14.3). The report gets its ranking caveat.
7. The API checks every cited reference against the stored events of the source
   run (14.3). A report that fails the check is not returned. A failed analysis
   or check returns a generic `500 analysis_failed`, and its cause is logged on
   the server only.

### 14.3 Verified observations and provenance

- Three kinds: `anomalous_series`, `normal_series`, and `onset_order`. Each one
  cites the stored event IDs it was computed from.
- Only measurements that contributed to an alert produce an anomaly or an onset.
  Context samples before onset are excluded, as in section 13.4. The anomaly
  statement gives the detector's score range against its threshold, which is
  the baseline deviation, and it counts the excluded samples.
- A `normal_series` observation requires normal verdicts in the span and no alert
  contribution. It does not claim that the series never moved.
- The `Investigation` validator rejects any dangling reference. Each hypothesis
  cites observations that exist, and its supporting event IDs equal the union of
  those observations' events. Each contradiction belongs to a hypothesis that lists
  it. Ranks run from 1 without gaps, and the status matches whether any hypothesis
  exists.
- **Provenance is checked before a report is returned.**
  `investigation_provenance.provenance_violations` requires that each cited event
  exists in the stored run, belongs to the source run, is on a series the
  observation names, and falls inside the analysis span. Each observation and
  gap must also fall inside the span. Each hypothesis may cite only events that a
  supporting observation lists, and every observation it references must exist.
  The offline evaluator calls the same function (14.6).
- **Bounded citations.** Each cited event list keeps at most
  `investigation.max_cited_events` IDs (default 100, configurable through
  `AEGIS_INVESTIGATION_MAX_CITED_EVENTS`, range 1 to 1000). The total is kept in
  `source_event_count`, `supporting_event_count`, or the gap or contradiction's
  `source_event_count`. A `*_truncated` flag must match the counts, and the report
  sets `evidence_truncated` when any list is shortened. A capped hypothesis lists
  only events that a listed observation shows. Ranking, statements, and status are
  computed before the cap and do not depend on it.

### 14.4 Hypothesis rules

| Pattern | Primary signal | Corroborating | Competing (must be normal) | Expected order |
|---------|----------------|---------------|----------------------------|----------------|
| Database query performance degradation | `db_query_latency_ms`, orders-db | `request_latency_ms`, orders-service | none | database before request latency |
| CPU resource saturation | `cpu_utilization_percent`, orders-service | `request_latency_ms`, orders-service | none | CPU before request latency |
| Application processing bottleneck | `request_latency_ms`, orders-service | none | CPU, orders-service; database query latency | none |
| API error propagation | `http_error_count`, orders-service | `http_error_count`, api-gateway | none | none |
| Database connectivity problem | `db_connection_failures`, orders-service | none | none | none |

Decisions:

- **Minimum primary ticks.** The primary signal must contribute to at least
  `MIN_PRIMARY_ALERT_TICKS` (2) alert ticks. One sample that crosses a threshold
  is what a noisy count produces, so it is not a pattern. The value is one
  constant in `investigation_catalog.py`. It was chosen after one probe of the
  simulator's fault scenarios on seeds 301, 302, and 311. The probe showed
  single-tick hypotheses from noise, which is why the rule exists. Seeds 301 and
  302 are also the local development baseline seeds, so the probe used seeds that
  are not evaluation seeds. The value was not tuned on evaluation seeds 601 to 610.
- **Competing signals.** A competing signal that is anomalous, or has no verdict,
  stops the rule. The report records which signal stopped it.
- **Contradictions.** A corroborating signal measured normal is contradicting,
  because the hypothesis expected it to move. An onset order that runs backwards
  is contradicting. Temporal precedence is necessary for a causal reading, but it
  is not sufficient.
- **Status and verification.** A hypothesis is `supported`, or
  `supported_with_contradictions` when it has any contradicting observation. Its
  verification status is always `unverified`, and the validator enforces it.
- **Alternatives.** Each hypothesis lists the other hypotheses generated for the
  same incident.

### 14.5 Ranking

Hypotheses are ordered by fewer contradicting observations, then by more
anomaly-based supporting observations (not counting normal ones), then by catalog
order. The ordering is an aid for review. It is not a probability or a confidence
score, and the report states this in `ranking_basis`.

**Roles.** Each hypothesis carries a `role`. `downstream_symptom` marks an effect
that a fault elsewhere would produce. `api_error_propagation` is the only such rule,
because errors passed up a call chain do not show where they started.
`potential_initiating_fault` marks a condition that could start the degradation.
The role is fixed in the catalog and does not change the ranking.

**Ranking caveat.** Every report carries `ranking_caveat`. It states that the order
reflects heuristic evidence support, that it favours more corroborating series,
and that it is not a measure of causal certainty. When rank 1 is a downstream
symptom and a potentially initiating hypothesis was also generated, the caveat
names both. All generated hypotheses stay in the report, each still unverified.

**Known weakness.** Ranking counts observations, so it favours a hypothesis with
more corroborating series. In connection-failure runs, `api_error_propagation`
ranks above `database_connectivity_problem` in every evaluated run, because the
first has two anomalous error series and the second has one. Section 14.7 reports
this. The ordering was not changed after the held-out results were viewed. Changing
it now would tune on evaluation data. A causal ranking that weighs specificity is
future work. It needs its own design and validation on seeds that were not viewed,
and its choice is an owner decision.

### 14.6 Evaluation methodology

- `investigation_evaluation.py` is the only label-aware module in this section. It
  is offline, and the test suite keeps it out of the router's import graph.
- Five properties are measured per run:
  - **Window agreement.** Each incident is in window when its alert ticks overlap
    the span the simulator labelled anomalous. Otherwise it is a false positive.
    The detector is not tuned on this count. Normal runs have no labelled window,
    so all their incidents are false positives.
  - **Provenance.** Every cited event ID resolves to a stored event of the source
    run, of the named series, inside the span. This is the check the API applies
    before returning a report (14.3).
  - **Support.** Every hypothesis cites at least one non-normal observation.
  - **Reproducibility.** Rebuilding the incidents and reports from the same runs
    gives identical reports.
  - **Scenario agreement.** Whether the hypothesis that matches the simulator fault
    is generated, and whether it ranks first. This is agreement with a label, not
    causal accuracy.
- Baselines are seeds 101 to 104. Evaluation seeds 601 to 610 at 60 ticks were not
  used to choose any rule or threshold. Run it with
  `python scripts/evaluate_investigations.py`.
- A faulty baseline stops the evaluation with `BaselineRejected`, as in the
  correlation sweep.

### 14.7 Results on the simulator

Output of `scripts/evaluate_investigations.py`, 50 runs (10 per scenario):

| Scenario | Incidents | In window | False positives | Provenance failures | Unsupported | Irreproducible | Pattern present | Pattern first | Contradictions |
|----------|----------:|----------:|----------------:|--------------------:|------------:|---------------:|----------------:|--------------:|---------------:|
| normal_operation | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 |
| database_latency_spike | 10 | 10 | 0 | 0 | 0 | 0 | 10 | 10 | 1 |
| cpu_saturation | 10 | 10 | 0 | 0 | 0 | 0 | 10 | 10 | 1 |
| api_errors | 10 | 10 | 0 | 0 | 0 | 0 | 10 | 10 | 0 |
| connection_failure | 11 | 10 | 1 | 0 | 0 | 0 | 10 | 0 | 0 |

- **The 11 connection-failure incidents are 10 in window and 1 false positive.**
  Seed 606 produces a second incident: one alert tick at 00:59, 20 minutes after
  the labelled window closed, where three series crossed the threshold together.
  It is a single-tick group, which the selected Day 4 rule allows
  (`min_consecutive_ticks=1`). Its report is `insufficient_evidence`, with no
  hypotheses. The detector was not changed.
- The integrity checks pass in every run. Normal runs produce no incident.
- The expected pattern is generated in every fault run. It ranks first except in
  connection failure (14.5).
- Contradictions are rare. In the database and CPU sets, each contains one
  `api_error_propagation` hypothesis from orders-service error ticks, with the
  gateway measured normal. The simulator's clean step faults leave few conflicts.

### 14.8 Limitations

- **Simulator only.** Faults are large step changes that affect one tier at a
  time. The baseline is stationary.
- **Missing sources.** Query traces, application logs, host metrics, connection
  pool limits, and change records are not in the telemetry model. Each report
  lists the affected hypotheses under `missing_evidence` with the check that would
  read the source.
- **Upper-tail detector.** A fall in `active_db_connections` is outside the
  detector's direction, so it cannot appear as evidence. The report records this
  as `direction_not_evaluated`.
- **Rules are authored.** They were written from the simulator's documented fault
  effects and the declared topology. Other rule sets could give other answers.
- **Count-based ranking.** See 14.5.
- **Single incident.** Each report is computed from one incident. No reasoning
  spans incidents.
- **No persistence.** Reports are recomputed per request from the in-memory store,
  as in section 13.10. No runtime guard limits it to development. Section 5 records the exposure.
- **Recomputation cost.** Each request fits the detector twice, once for the
  incident and once for the verdicts, and scores the whole store. The store is
  capped at 50,000 events, but the cost is not bounded beyond that. The cited-event
  cap (14.3) bounds the response, not the work. Caching per store snapshot is future
  work.
- **Truncated citations.** A capped list shows only some event IDs. The total and
  the flag say so, but the report does not list the rest. There is no endpoint that
  lists them, so a reader cannot verify beyond the listed IDs through the API.

### 14.9 LLM investigation agent (fake and optional live provider)

Status: **implemented as a Python library and a local-only CLI.** Three providers
exist. The default `fake` provider makes no network call. The `openai` and
`anthropic` providers are optional, for local development only, and each runs only
with `--provider <name> --live`. **The OpenAI adapter has not been run against its
live API. The Anthropic adapter has one recorded single-case live run that completed
and verified** (section 14.9, "Recorded live validation"). That run is not evidence of
general accuracy, and the tests use simulated responses. Behaviour with a real model
is otherwise unverified. There is no HTTP endpoint.

**Approvals.** Phase 1 covered a local, read-only agent over synthetic evidence.
Phase 2 covered an optional OpenAI adapter for local development. Phase 3 covered
offline hardening, budgets, accounting, a preflight, and an offline evaluation. Phase
4A covered an optional Anthropic adapter for local development. The Day 7 exception
in `CLAUDE.md` records that scope, including the two pinned SDKs. It does not cover
production use, any endpoint, any change to authentication, paid requests without a
separate authorization for each, remediation, or any other provider.

**Decisions.**

- Days 1 to 6 are unchanged. The detectors, ranking, investigation schema, baseline
  trust, and provenance checks were not modified.
- The model only requests tools. Only the trusted, allowlisted, read-only registry
  runs them, and each tool is bound to one report.
- The model selects records by ID. The server supplies the canonical sentence.
- Every model output, from any provider, goes through the same deterministic verifier.
  A failed answer is rejected in full, never trimmed.
- Agent modules never read ground-truth labels or evaluation data.

**Provider architecture.**

| Component | Module | Role |
|-----------|--------|------|
| Provider interface | `services/agent_provider.py` | `ModelProvider`, and `ProviderError` with a fixed reason and any reported usage |
| Budgets and accounting | `services/agent_budget.py` | `RunBudget`, the token reserve, the pricing file, and cost estimates |
| Shared transport | `services/agent_transport.py` | The budgeted send loop used by every live adapter |
| Fake provider | `services/agent_fake_provider.py` | Scripted replies for tests, and the reference answer for the CLI |
| OpenAI adapter | `services/agent_openai_provider.py` | Responses API. The only module that imports the OpenAI SDK |
| Anthropic adapter | `services/agent_anthropic_provider.py` | Messages API. The only module that imports the Anthropic SDK |
| Loop | `services/agent_orchestration.py` | Budget checks, dispatch, verification, and status mapping |
| Compact answer | `services/agent_compact.py` | Rebuilds the full answer from a compact, ID-only answer (section 14.10) |
| Preflight | `scripts/check_llm_environment.py` | Checks both SDKs without sending a request |
| Evaluation | `scripts/evaluate_agent.py` | Offline fake-provider evaluation, and a gated live mode |

Each adapter is loaded only when its flags are given. Importing the application
loads neither SDK. A test checks this in a separate process.

**Shared request-budget transport.** `send_within_budget` sends every live request.
Each adapter supplies two things: a function that sends one request, and a function
that classifies its errors as a fixed reason and as transient or not. The transport
does the rest. For each attempt, including a retry, it checks the request count, the
deadline, and the token reserve before anything is sent. It takes the timeout from
the time left before the deadline, and it refuses to send if no time is left. It
counts every attempt as a request, and it retries only if the retry limit allows,
the error is transient, and the backoff still ends before the deadline. Usage from
each reply is added after it arrives. Because both adapters use one transport, the
budget rules are the same for each.

| Limit | Default | Hard maximum | Checked |
|-------|---------|--------------|---------|
| Provider requests, including retries | 8 | 20 | before every request |
| Tool requests, including refused ones | 6 | 20 | before every tool request |
| Total tokens | 30,000 | 200,000 | before each call, and after each reply |
| Output tokens per response | 1,000 | 4,000 | sent as the response limit |
| Timeout per request | 30 s | 120 s | per request, capped to the time left |
| Run deadline | 180 s | 900 s | before each call, and after each reply |
| Automatic retries | 0 | 3 | only if the retry limit allows |

The token reserve is an estimate: one token per two characters of the serialized
request, plus the output allowance. A request whose reserve would pass the token
limit is not sent. The estimate overestimates ordinary English. It is not a count,
and it does not guarantee that a request stays within budget. A local timeout does
not cancel the request on the server, and the provider may still bill it. A request
in flight can run past the deadline by at most one timeout.

**Configuration.** These come from the environment only:

| Variable | Required for | Notes |
|----------|--------------|-------|
| `ANTHROPIC_API_KEY` | `anthropic` | Held as a `SecretStr`. Never logged, printed, or put in a prompt |
| `OPENAI_API_KEY` | `openai` | Held as a `SecretStr`. Never logged, printed, or put in a prompt |
| `AEGIS_LLM_MODEL` | both live providers | No default. Must match a restricted identifier pattern |

`load_live_model_settings` in `config.py` fails closed. Its errors name the variable
that is missing or invalid, and never a value. The key variable is a parameter, so
each provider reads its own key. Pricing is not an environment setting. It comes
from a file the operator names with `--pricing-file`.

**Anthropic tool-calling workflow.**

1. The loop sends fixed instructions and the investigation ID. The transcript holds
   no report narrative.
2. The adapter sends a stateless request. The `system` field holds only the trusted
   instructions. Each user turn is a user message. Each earlier tool request is an
   assistant message with a `tool_use` block, carrying its `id`, `name`, and `input`.
   Each result is a user message with a `tool_result` block, carrying the same
   `tool_use_id`, and `is_error` set when the tool request was refused. The request
   carries `tool_choice` with `disable_parallel_tool_use` set. Tools are declared
   with `input_schema`, and the answer tool is `submit_answer`. The wire schema
   omits `minLength`, `maxLength`, and `maxItems`, which the API documents as
   unsupported. Pydantic still enforces those limits on every answer.
3. The adapter parses the reply by stop reason, as listed under Failure states. Each
   `tool_use` block becomes a turn. The first is returned, and the rest are queued,
   so no extra request is sent for them. Text becomes a final answer. More than 8
   tool-use blocks in one reply is malformed.
4. The loop checks its limits, then dispatches each tool request through the trusted
   registry. The SDK never runs a function. Invalid input produces a fixed refusal,
   returned with the same `tool_use_id`.
5. A `submit_answer` block, or a plain-text answer, is parsed in the run's answer
   format, `AgentOutput` by default or `CompactAgentOutput` (section 14.10). It is
   then verified. If it passes, each claim's canonical text is filled from the
   report.

**Anthropic thinking-block replay.** When the model thinks before a tool request, its
reply carries `thinking` or `redacted_thinking` blocks with signatures. The API requires
each tool round to send those blocks back unchanged, so the adapter does the following.

- It stores the reply's content blocks in order, in `ProviderTurn.replay_blocks`, and
  copies them to the transcript message for that tool request. Only the fields the API
  accepts are kept: `thinking` and `signature`, `data`, `text`, and `id`, `name`, and
  `input`. A block of any other type, or a thinking block without a string signature,
  is malformed.
- On replay, it sends the stored blocks in their original order, followed by the tool
  result. It checks that the stored `tool_use` block matches the recorded request
  (ID, name, and input) before sending anything. A mismatch is refused without a request.
- A reply with several tool requests and any thinking block is malformed, because the
  blocks cannot be replayed in order for each request. Without thinking blocks, the
  existing queue behaviour is unchanged.
- The system text and tool definitions are the same on every request. Thinking blocks
  never reach logs, results, or the CLI, and `replay_blocks` is excluded from `repr`.
- The request does not set `thinking`, so the model's default applies. Thinking tokens
  count against `max_output_tokens`, so a long thinking turn can stop with `max_tokens`.
  That is reported as `INCOMPLETE`. The budget is unchanged.

**OpenAI tool-calling workflow.** The OpenAI adapter follows the same pattern with the
Responses API. Each request is stateless, with `store` set to false. Calls are
`function_call` items, and results are `function_call_output` items with the same
`call_id`. Parallel calls are disabled. The final answer is `submit_answer`. The
store setting was set in code, and it has not been re-checked against OpenAI's
current documentation in this phase.

**Evidence and verification boundaries.** Every answer, from any provider, goes
through the rules in section 14.9 of Phase 1: kind, reference, canonical text, cited
events, proposed checks, caveat, insufficient evidence, and report provenance. Two rules
matter most for a live model:

- Claim text is optional. When it is left out, the server supplies the canonical
  sentence. When text is given, it must match exactly.
- `root_cause_assessment` must equal `not_assessed`. Any other value is malformed.

The three groups stay separate: `observation` for verified observations,
`hypothesis` for existing unconfirmed candidates, and `uncertainty` for missing
evidence, limitations, and the ranking caveat.

**Failure states.** Each failure has a fixed reason and carries no provider text.

| Reason | Status | Retried |
|--------|--------|---------|
| `refused` (`refusal` stop reason, or a refusal item) | `refused` | no |
| `incomplete` (`max_tokens` or `pause_turn`) | `incomplete` | no |
| `malformed` (unknown stop reason, or unusable content) | `malformed_output` | no |
| `timeout` | `provider_error` | yes, if the retry limit allows |
| `network` | `provider_error` | yes, if the retry limit allows |
| `rate_limited` (HTTP 429) | `provider_error` | yes, if the retry limit allows |
| `server_error` (HTTP 5xx) | `provider_error` | yes, if the retry limit allows |
| `invalid_request` (HTTP 400, 422) | `provider_error` | no |
| `authentication` (HTTP 401) | `provider_error` | no |
| `permission` (HTTP 403) | `provider_error` | no |
| `not_found` (HTTP 404) | `provider_error` | no |
| `api_error` (any other HTTP status, or an unrecognised exception) | `provider_error` | no |
| `budget` (a limit stopped the request) | `budget_exhausted` | no |

**Provider error classification.** The first authorized live attempt returned
`provider_error` with reason `api_error`, one request, and no tokens. The old
classification discarded the HTTP status, so the cause could not be identified. The
classification above was added so that a failed run records the status it received.
The adapter maps the Anthropic SDK's exceptions, and the shared transport applies the
retry rules.

- **Order.** Timeouts are checked first, then connection failures, then HTTP status
  errors. Only the status code and the request ID are kept from a status error. The
  SDK's message and response body are never read into a result.
- **Sanitized status.** A run records `provider_http_status`, an integer from 100 to
  599, when the provider returned an HTTP response. It is absent for timeouts,
  connection failures, budget stops, and local errors.
- **Request ID.** A run records `provider_request_id` only when the value from the
  response header matches `^[A-Za-z0-9_-]{1,128}$`. Any other value is dropped, not
  truncated or cleaned. The identifier is only in the run result. It is not logged,
  not sent to a model, and not returned by any HTTP endpoint.
- **Retry restrictions.** Only timeouts, connection failures, 429, and 5xx are
  retried. A retry needs the retry limit, the request count, the token reserve, and the
  deadline to allow it. The SDK's own retries are off, so 400, 401, 403, 404, 422, and
  any other unlisted status are sent once. The default retry limit is 0.
- **Fail closed.** A provider failure ends the run with status `provider_error` and
  verification `not_verified`. `AgentRun` rejects any claim or proposed check on an
  unverified run. The CLI exits with code 2. The loop does not fall back to another
  provider, and it does not return a partial answer from a malformed reply.
- **Scope.** The OpenAI adapter uses the same transport and the same retry rules. Its
  classifier does not yet record the HTTP status or request ID, and its 4xx responses
  stay `api_error`. Those reasons apply to the Anthropic adapter only.

Anything the classifier does not recognise is `api_error` and is not retried. The
message is dropped, because an SDK message can echo request details.

**Cost and usage accounting.** Each run returns a `RunAccounting` record with the
model identifier, provider requests, completed tool calls, input, output, and total
tokens, end-to-end latency, final status, and any stop reason. Cost is estimated only
from the operator's pricing file, keyed by model identifier, with each entry recording
its source and verification date. No rate is built into the code. A run without a
price for its model reports cost as unknown, never as zero.

**Security controls.**

- No credential appears in a repr, a dump, a log, a prompt, or an error. Tests check
  this with a sentinel that the SDK echoes back.
- Each SDK's own retries are off, so the budgeted transport is the only retry path.
- Tool output and telemetry reach the model only as data in tool results. They never
  enter the system text, which holds only the trusted instructions.
- A refused tool request never echoes the model's name or input.
- A provider's output is never executed, and it cannot bypass verification.

**Retention and data egress.** Each request carries the report's canonical statements
and identifiers as tool-result data. In this phase they come from simulated telemetry.
Real telemetry must not be sent without a separate data-handling decision. Anthropic's
API and data retention page (checked 2026-11-13) says that, for organizations without a
zero data retention (ZDR) agreement, conversation content is not retained by default,
except for Covered Models, which require 30-day retention. The page names the Fable and
Mythos families as Covered Models. It does not name Sonnet 5.5 in the part of the page
read for this check, but that must be confirmed with the account team before any live
use. ZDR is enabled per organization through sales. Retention is governed by the
provider's terms, and this code cannot enforce it.

**Model and pricing facts (checked 2026-11-13).** Anthropic's models overview lists
`claude-sonnet-5-5` as the Claude API ID for Sonnet 5.5, with 1M-token context, 128K
maximum output, and a base price of $2 per million input tokens and $10 per million
output tokens. The overview says Sonnet 5.5 uses adaptive thinking by default. This
adapter sets no thinking parameter, so whether thinking tokens are billed as output has
not been verified. Sources:
[Models overview](https://platform.claude.com/docs/en/models/overview),
[Pricing](https://platform.claude.com/docs/en/about-claude/pricing), and
[API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention).

**SDK pins and compatibility.** `requirements-llm.txt` pins `openai==2.54.0` and
`anthropic==0.109.0`. Each was chosen by a dry run that reported only real changes.
Newer releases of either SDK pull in `httpx2` and `truststore`, which are outside the
core pins, so they were not used. The earlier claim that the OpenAI 3.x release
upgrades `pydantic` came from a dry run that listed every package. Only the `httpx2`
conflict is real. `pip check` is clean with both SDKs installed, and the core pins are
unchanged.

**SDK preflight.** `scripts/check_llm_environment.py` checks each SDK's Python version,
installed version against its pin, import, client construction with a dummy key, and
resource loading. It sends no request. In this environment every check passes for both
SDKs. A pass shows the SDK loads here. It does not show that a live adapter works.

An earlier pytest run of the OpenAI client was blocked by Windows Application Control.
That failure has not been reproduced. Whether the policy applies to every process on
this machine is not verified. No security policy was changed or bypassed. If the block
returns, the authorized options are an approved development host, or an environment
whose security owner has approved the SDK.

**Offline tests.** `test_agent_openai_provider.py` and `test_agent_anthropic_provider.py`
drive each adapter with simulated responses and fake clients. They make no request. They
cover tool round trips, identifier correlation, multiple tool requests, invalid and
unauthorized tools, malformed output, fabricated IDs, cross-incident answers, injection,
refusal, incomplete output, timeouts, rate limits, each HTTP status category, retry
exhaustion, request-ID handling, configuration errors, token accounting, budget
exhaustion, credential handling, and the absence of
accidental imports or requests. `test_agent_budget.py` covers limit bounds, zero retries
by default, request-budget exhaustion across retries, deadline exhaustion before a
request, timeouts capped to the deadline, the token reserve, overruns, pricing, and unknown
cost. `test_agent_preflight.py` uses stand-ins. `test_agent_evaluation.py` checks that
every faulty answer is rejected, that the model never receives labels, and that live
mode is refused without its flags.

**Offline evaluation.** `scripts/evaluate_agent.py` runs the fake provider over four
synthetic scenarios and three seeds each. For each scenario it runs the faithful answer,
14 faulty answers, a bad tool request, a timeout, a refusal, and an exhausted tool
budget. Token counts are estimates, and cost is unknown. The measures are
structured-output validity, faulty answers rejected and accepted, statements accepted
that the report does not contain, tool successes and rejections, provider failures,
budget stops, estimated tokens, and latency. The accepted counts must be zero. Scenario
agreement is a label check. It is not evidence of causal accuracy. The default result is
228 cases, 168 faulty answers all rejected, none accepted, 228 of 228 expectations met,
and 12 of 12 scenario agreements. These results cover the simulator's fault types with
the fake provider. They say nothing about a real model.

**Live evaluation and live smoke test.** The evaluation script's live mode requires
`--provider openai|anthropic` and `--live`. It runs the reference case through the live
provider, capped by `--max-live-cases`, which defaults to one and is bounded to 12. The
single-run CLI is `scripts/run_investigation_agent.py`. The live mode of the evaluation
script has not been run against a live API, and the OpenAI provider has not been run
live at all. Pytest, CI, imports, and default evaluation never run them.

**Recorded live validation (Anthropic, one synthetic case, 2026-11-13).** One live
investigation completed and verified. It is one synthetic case. It is not evidence of
general causal accuracy, and it is not evidence of production readiness.

Configuration, as run:

- Provider: `anthropic` (Anthropic Messages API), with `--live`
- Model: `claude-sonnet-5-5` (Claude Sonnet 5.5)
- Answer format: `compact`
- Scenario and seed: `cpu_saturation`, seed 601
- Budget: 8 requests, 6 tool calls, 30,000 total tokens, 1,000 output tokens, 30 s
  per request (the default), 180 s deadline, 0 retries
- Pricing file: none

Result:

- Status: verified. Verification: verified
- Provider requests: 5. Completed tool calls: 4. Verified claims: 13. Read-only checks
  proposed: 5
- Tokens: 20,952 input, 747 output, 21,699 total. Latency: about 8.16 seconds
- Provider failures: none. Budget stop: none. Cost: unknown, no pricing file

The command is `scriptsun_investigation_agent.py --provider anthropic --live
--answer-format compact --scenario cpu_saturation --seed 601 --max-requests 8
--max-tool-calls 6 --max-total-tokens 30000 --max-output-tokens 1000
--request-timeout 30 --max-seconds 180 --max-retries 0`.

What this does not show: accuracy across other scenarios or seeds, repeat-run
stability, how often real answers are rejected, retention or data-handling suitability,
or production readiness. The offline evaluation (228 fake-provider cases) and this
single live run measure different things and must not be combined into one accuracy
claim.

**Authorization for any further Anthropic run.** Each further run is paid and needs all
of the following:

- Owner authorization for that specific run, because it makes paid requests.
- A passing preflight in the environment that will run it.
- `ANTHROPIC_API_KEY` and `AEGIS_LLM_MODEL` set in the environment, from the owner's
  secret store, with the model identifier the owner has chosen.
- Confirmation that the data to be sent is acceptable, and confirmation of retention
  terms for the chosen model.
- A pricing file, if a cost estimate is wanted. Without one, cost is reported as unknown.

Example command, which must not be run without that authorization:

    python scripts\run_investigation_agent.py --provider anthropic --live --scenario cpu_saturation --seed 601 --max-requests 4 --max-total-tokens 20000 --max-seconds 120 --max-retries 0

The run should record the exit code, status, failure reason or budget stop, accounting
record, and cost basis. It must not record the key.

**Anthropic connectivity diagnostic (run once, 2026-11-13).** Before the agent run,
`scripts/anthropic_connectivity_check.py` sends one synthetic message, "Reply with OK.",
with no tools, no system text, no thinking settings, and no investigation data. It
allows 16 output tokens and disables SDK retries. Without `--live` it checks the model
setting, the request shape, and the SDK client offline, and sends nothing. With `--live`
it sends one request. A failure reports the HTTP status, a sanitized request ID, and a
category from a fixed list. It never prints the key, the request body, response headers,
or the SDK's error message. It does not test tool calling, thinking-block replay, or
evidence verification. Those need the agent CLI above.

Command for one authorized request, as run:

    python scripts\anthropic_connectivity_check.py --live

The run completed successfully: model `claude-sonnet-5-5`, result `ok`, stop reason
`end_turn`, 15 input tokens, 5 output tokens. It used no investigation tools, so it
tests only that the SDK, key, and model identifier work. It is separate from the
investigation run recorded in section 14.9.

**Known limitations.**

- Live validation is limited to one Anthropic single-case run. The OpenAI adapter has
  not been validated against its API. How a real model behaves across scenarios and
  seeds, including how often its answers are rejected, is unknown.
- Strict canonical quoting may reject many real answers. A rejected answer is reported as
  rejected, never trimmed. Narrative text needs its own verification design.
- `claude-sonnet-5-5` is confirmed as an identifier on the models overview. It was used
  in the one recorded run. The installed SDK's list does not include it, though the SDK
  passes unknown identifiers through unchanged.
- The HTTP status of the first live attempt is unknown. That run predates the
  classification above, so its status was never recorded.
- The adaptive-thinking default and its effect on output tokens are unverified.
- Cost is an estimate from the operator's pricing file, not a provider invoice.
- A retry can repeat a request the provider already processed, and each attempt is
  billed.
- The deadline can be overrun by one in-flight request.
- Tool outputs and report statements are sent to the provider as data. Real telemetry
  must not be sent without a separate data-handling decision.
- The SDK preflight passes here, but an earlier pytest run of the OpenAI client was
  blocked by Windows Application Control. That has not been reproduced.

**Production authentication and exposure gap.** None of this is production-ready. The
investigation and incident endpoints, and the simulation API, have no authentication. The
agent has no endpoint. Any future endpoint needs the authentication design in section 5
first. The live adapters are for local development only, and production use needs a
separate approval.

**Not in this phase.** Production use, any HTTP endpoint, any change to authentication,
streaming, prompt caching, additional providers, free-form narrative, autonomous
remediation, and any paid request.

**Phase 1 notes preserved.** The fake provider makes no request, and its reference
answer is written from the report. Passing it shows that the verifier accepts a faithful
answer, and that the loop works. It does not show the behaviour of a real model.

### 14.10 Compact final answer (optional, local development only)

**Why.** A full answer repeats each claim's reference, including every event ID of its
observation. On the synthetic incidents, that answer was 18,700 to 21,800 characters
without claim text. The 1,000-token output cap is far below that, so a faithful full
answer could be cut off and reported as incomplete. The compact answer is designed to
fit within the cap.

**Contract.** `CompactAgentOutput` has six fields: `investigation_id`,
`root_cause_assessment` (fixed to `not_assessed`), `observations`, `hypotheses`,
`uncertainties`, and `proposed_checks`. Each list keeps its kind, so an observation, an
unconfirmed hypothesis, and an unknown stay distinct. `uncertainties` accepts only
missing-evidence, limitation, and ranking-caveat sources. The model never sends text,
event IDs, or a reference per claim. Extra fields are refused.

**Stable references.** The compact form relies only on identifiers the report already
holds:

| Record | Reference in a compact answer | Stability |
|--------|-------------------------------|-----------|
| Verified observation | `observation_id` (`obs-...`) | Stable within the report |
| Hypothesis | `HypothesisKind` | Stable. Each generated kind is named by its kind |
| Missing evidence | Position in the report | Positional only. The record has no ID |
| Limitation | Position in the report | Positional only. The record has no ID |
| Ranking caveat | Fixed ID `ranking_caveat` | Stable |
| Suggested check | Pair of `CheckKind` and affected hypotheses | No record ID. Identified by the pair |

No identifier was invented for the positional records or the checks. Positions match
the keys the existing verifier and tools already use.

**Expansion.** `expand_compact` builds an `AgentOutput` from trusted fields only. An
observation's events come from the report's `source_event_ids`. A hypothesis or
uncertainty takes its kind from the list it appears in. Each reference carries the
answer's investigation ID. Text is left unset, so the server fills it from the canonical
record after the answer is accepted. An unknown ID keeps its name and gets no events. The
verifier then rejects it. Expansion refuses nothing itself, and every refusal is one the
existing verifier makes.

**Verification guarantees.** The expanded answer is passed to the unchanged
`verify_output`, and the same `hydrate_claims` fills the text. The existing rules apply
without change: kind and source, canonical text, cited events against the source run,
suggested checks, ranking caveat, insufficient evidence, and report provenance. Each
case has a test. Unknown, other-incident, and ungenerated IDs are rejected. An answer
naming another investigation is rejected before any claim is checked. Malformed JSON,
extra fields, non-ID strings, a wrong source in `uncertainties`, and more references
than the limit are malformed. A format mismatch is malformed in both directions.

**Format selection.** `run_agent` takes an `answer_format`, which defaults to `full`.
The provider takes the same value, and it sets the answer tool's schema and the system
note. The CLI passes one value to both. The full path is unchanged, and it is still the
default, so the existing fake provider and tests behave as before.

**Measurements (estimates, offline, fake client).** On the CPU-saturation fixture, with
the same three-tool sequence:

| Measure | Full | Compact |
|---------|------|---------|
| Submit schema, characters | 3,609 | 2,606 |
| Final answer without claim text, characters | 18,736 | 1,404 |
| Final answer, estimated tokens (chars / 2) | 9,368 | 702 |
| Fourth-request estimate, tokens | 7,265 | 6,793 |

With the recorded live total of 12,331 tokens already used, the fourth request was
refused at 20,000 tokens for the full answer. For the compact answer it would still be
refused at 20,000 with the 1,000-token reserve, but it fits at 30,000. These numbers come
from a synthetic transcript and an estimator. They are not live token counts.

**Limitations.**

- The compact form has not been run against a live model, so whether a model names
  records correctly, and how often its answers are refused, is unknown.
- Positional references to missing evidence and limitations depend on the report's
  stored order. A report with a different order gives different positions.
- A compact observation reference cites every event of the observation. The full form
  allowed a subset, so the compact form is stricter, but it cannot express a narrower
  citation.
- The estimator counts characters, so the savings are estimates, not provider counts.
- Suggested checks are identified by the pair of kind and affected hypotheses. Two
  checks with the same pair would be indistinguishable.

### 14.11 Day 8: evaluation matrix, provider turns, and lifecycle trace

Status: **implemented, offline only.** The fake provider is the default and the only
provider used here. No live request was made for this section, and no new endpoint,
dependency, or paid path was added.

**Scope.** Day 8 measures verified completion and explains controlled failures from
an ordered trace. It changes no verifier decision, budget limit, tool scope, or
existing result field. The one addition to default output is the `provider_turns`
key on `AgentRun`.

**Provider turns.** `AgentRun.provider_turns` counts each provider invocation the
orchestrator attempts, including one that raises. A stop before an invocation is not
counted. It is separate from `provider_requests`, which counts requests the transport
admitted and sent. A fake run can have `provider_turns > 0` and `provider_requests == 0`.
The fake path does not call the transport, and no admission record is fabricated for it.

**Lifecycle trace.** `AgentRun.trace` is an ordered tuple of `TraceEvent` values,
bounded at 128 events. Each event carries:

| Field | Meaning |
|-------|---------|
| `sequence` | 1-based position within the run |
| `kind` | `provider_turn`, `admission`, `tool_outcome`, `verification`, `provider_failure`, `budget_stop`, or `terminal` |
| `outcome` | A fixed category for that kind (for example `invoked`, `ok`, `refused_unknown_tool`, `rejected`, `timeout`, `tokens`, `provider_error`) |
| `elapsed_ms` | Milliseconds from the first reading of a trace clock. It is null when no trace clock is given |
| `tool` | An allowlisted tool name, or null. It is never set for an unknown tool |
| `failure_codes` | The distinct verifier failure codes, in first-seen order, on a `verification` event only |

The trace is recorded where each step happens, by `app/services/agent_trace.py`.
Every run ends with exactly one `terminal` event, which is the last event. A budget
stop is recorded just before it. A trace has no prompt, transcript, tool argument or
result, telemetry, claim text, replay block, or raw error text.

**Timing without a second clock.** The trace takes its own optional clock, separate
from the budget's. The budget's clock governs the deadline, and tests script it
exactly. A run without a trace clock reads no extra time and leaves `elapsed_ms`
null. The evaluator passes a monotonic performance counter.

**Export.** The trace is excluded from `AgentRun` JSON by default. The evaluator's
`--json --trace` adds the traces of its offline cases. `--trace` without `--json`, or
with a live provider, is refused with exit code 3.

**Evaluation matrix.** `scripts/evaluate_agent.py` runs 240 cases: four scenarios,
seeds 601 to 603, and 20 cases per scenario-seed pair. The cases are split by design,
not by outcome:

- *Verified completion* (24 cases): the reference answer, and the reference after
  refused tool requests. Met when the status is `verified`.
- *Expected failure handling* (216 cases): 168 faulty answers, 12 budget
  exhaustions, 12 timeouts, 12 refusals, and 12 provider failures after one
  successful tool call. Met when the status is the expected stop or rejection, with
  no claims. The mid-run case also requires exactly one retained tool success.

Unsafe acceptance is reported separately, and it must be zero. It counts faulty
answers that verify (`attacks_accepted`) and verified claims whose text is not
canonical (`novel_statements_accepted`).

**Offline result, 2026-11-15, fake provider, seeds 601 to 603.** Verified completion
24/24. Expected failure handling 216/216. Faulty answers rejected 168/168, accepted 0.
Novel statements accepted 0. Provider turns 1056. Transport requests 0. Trace events
2352. Scenario agreement 12/12, a label check and not causal accuracy. Tokens are
estimates, 3,382,185 in total, one per two characters. Cost is unknown. Mean latency
was under 1 ms per case across the two recorded runs, and it varies. Two runs gave
identical outcomes, counts, and event order. Only the latency and the `elapsed_ms`
values differed.

**Limitations.**

- The fake provider is deterministic and scripted. Its results say nothing about how a
  live model behaves, and the mid-run failure is an injected stop, not a fault observed
  in a real provider.
- Tokens are estimates. The offline run has no price, so its cost is unknown.
- Trace timing is local and varies between runs.
- The live path does not export traces. Transport admissions appear in the trace only
  where the live transport records them, and no live run has been traced.
- Traces are not stored. A caller that needs them must keep them.

### 14.12 Day 9: demo listing of approved baselines (read-only, development only)

Status: **implemented.** Local development only, for the planned React dashboard.

**Gap.** The incident and investigation endpoints require each `baseline_run_id` by
name. Those IDs are returned only by `POST /api/v1/simulations`, and the approved
manifest is not visible over the API. After a restart, a client has no way to find
which stored runs are approved baselines, so it cannot list or open incidents.

**Decision.** Add one read-only route, `GET /api/v1/demo/baselines`. It returns the
approved baselines that are also stored, sorted by run ID, with `data_origin` set to
`synthetic`. An approved run that is not stored is left out, because the incident
endpoints would refuse it with a 404. The route is registered only when
`AEGIS_DEMO_API_ENABLED` is true. The settings refuse that flag in production, the
same way they refuse the simulation endpoint.

**Unchanged.** Existing routes and response contracts are unchanged. The listing
reads the trusted-baseline registry and the store. It does not start a simulation,
call a language model, write state, or approve a baseline. Preparing data still uses
`POST /api/v1/simulations`, which is the only write, and it is unauthenticated.

**Limitations.** The store is in memory, so a restart clears it. Each incident
request recomputes its incidents from the store. The listing is not paginated, which
is acceptable because it can hold no more entries than the manifest approves. The
route has no authentication, so it carries the same loopback-only restriction as the
simulation endpoint.
