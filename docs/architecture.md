# AEGIS AI Architecture

**Status:** Draft. Describes the target architecture. Only the backend
foundation (`GET /`, `GET /health`) exists today. Everything else below is
planned and will be refined as each phase is implemented.

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
│   └── routers/           HTTP layer, one module per feature area
└── tests/                 pytest suite using the TestClient fixture
```

Planned additions, each as its own package:

- `app/core/`: configuration loaded from environment variables, logging setup
- `app/domain/`: incident, signal, and recommendation models with no I/O
- `app/services/`: use cases that coordinate domain logic and adapters
- `app/adapters/`: Oracle, LLM provider, and notification integrations
- `app/security/`: authentication and authorization dependencies

Dependency direction: routers depend on services, services depend on domain
and adapter interfaces, and nothing in `domain/` imports from the framework.

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

## 6. Data

The target database is Oracle. No connection code exists yet. The first
database design will define the incident, signal, recommendation, and audit
schemas. Connection details will come from environment configuration, and
wallet files will never be committed.

## 7. Deployment (planned)

- Phase 1 (current): run locally with `uvicorn` and `pytest`.
- Later: containerize the backend, add a local compose stack, then define
  Kubernetes manifests in `infrastructure/`.
- Health endpoint `GET /health` is designed for liveness and readiness probes.

## 8. Delivery phases

| Phase | Goal                                          | Status      |
|-------|-----------------------------------------------|-------------|
| 0     | Project foundation, health endpoint, tests    | In progress |
| 1     | Configuration, logging, and domain models     | Planned     |
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
