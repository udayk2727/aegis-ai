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

### Known gaps

- No persistence and no shared store between processes.
- Stationary baseline distributions; no seasonality or bursts.
- Abrupt step faults with no ramp.
- Fixed three-service topology, not configurable through the API.
- Linear scan for queries. Acceptable at this capacity; an index belongs with
  the database adapter.
- `POST /api/v1/simulations` is a state-changing endpoint without an accepted
  authorization design (see section 5).
