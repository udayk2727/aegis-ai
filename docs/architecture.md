# AEGIS AI Architecture

**Status:** Draft. Describes the target architecture. Today the backend has the
foundation (`GET /`, `GET /health`) and an in-memory simulation engine with
`POST /api/v1/simulations` and `GET /api/v1/events` (section 10). Everything
else below is planned and will be refined as each phase is implemented.

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
| Ingestion              | Accept and normalize alerts, logs, and metrics          | Planned |
| Correlation engine     | Group related signals into incidents                    | Planned |
| Investigation agents   | Gather context and propose root-cause hypotheses        | Planned |
| Approval gate          | Hold recommended actions until a human approves         | Planned |
| Action executor        | Perform approved remediation through audited adapters   | Planned |
| Audit log              | Append-only record of decisions and actions             | Planned |
| Data access adapters   | Oracle and other enterprise sources, behind interfaces  | Planned |
| Frontend               | Dashboard, investigation workspace, approval screens    | Planned |

## 4. Backend structure

```
backend/
├── app/
│   ├── __init__.py        version constant
│   ├── main.py            application factory (composition root)
│   ├── domain/            telemetry models: no I/O, no framework imports
│   ├── services/          simulation engine and in-memory event store
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
| 1     | Configuration, logging, and domain models     | Partial: telemetry models and simulation engine (section 10) |
| 2     | Signal ingestion and incident correlation     | Planned     |
| 3     | Authentication and authorization              | Planned     |
| 4     | Investigation agents with LLM adapter         | Planned     |
| 5     | Approval workflow and audit log               | Planned     |
| 6     | Controlled action execution                   | Planned     |
| 7     | Frontend dashboard and workspace              | Planned     |
| 8     | Containerization and Kubernetes deployment    | Planned     |

## 9. Open decisions

- Frontend framework (React, or another choice)
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
