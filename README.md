# AEGIS AI

**Autonomous Enterprise Incident Intelligence Platform**

AEGIS AI is a platform for detecting, investigating, and resolving operational
incidents in enterprise environments. It correlates signals from monitoring
and operational systems, proposes root causes and remediation, and executes
approved actions under human supervision and with a full audit trail.

> **Status:** Foundation stage. The repository provides a FastAPI backend with
> service and health endpoints, an enterprise incident simulation engine that
> generates labelled synthetic telemetry in memory, a pytest suite, and
> placeholders for the frontend and infrastructure. Correlation, investigation,
> and remediation are planned.

## Features

Available now:

- `GET /`: service identity (name, version, status)
- `GET /health`: liveness probe for operators and orchestrators
- `POST /api/v1/simulations`: run a deterministic simulation with a seeded
  scenario and store its events in memory (see [Simulation](#simulation))
- `GET /api/v1/events`: page through stored events with filters
- Automated tests with pytest

Planned (see [docs/architecture.md](docs/architecture.md)):

- Signal ingestion and incident correlation
- Investigation agents backed by an LLM adapter
- Human approval workflow and audit log
- Controlled remediation execution
- Web dashboard and investigation workspace

## Technology

| Area           | Choice                      |
|----------------|-----------------------------|
| Language       | Python 3.13                 |
| API framework  | FastAPI                     |
| ASGI server    | Uvicorn                     |
| Testing        | pytest, httpx               |
| Planned data   | Oracle Database             |
| Planned UI     | To be decided               |

## Repository layout

```
aegis-ai/
├── backend/          FastAPI application and tests
│   ├── app/          Application package
│   │   ├── domain/       Pydantic telemetry models (no I/O)
│   │   ├── routers/      HTTP endpoints, one module per feature
│   │   └── services/     Simulation engine and event store
│   └── tests/        pytest suite
├── frontend/         Placeholder for the user interface
├── infrastructure/   Placeholder for deployment assets
├── docs/             Architecture documentation
├── CLAUDE.md         Conventions and security rules
├── requirements.txt  Pinned Python dependencies
└── pytest.ini        Test configuration
```

## Getting started

These commands target **Windows PowerShell** and **Python 3.13**.

### 1. Create a virtual environment

```powershell
cd C:\Projects\aegis-ai
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If activation is blocked by execution policy, run once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 2. Install dependencies

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Run the API

The simulation endpoint is off by default. To use it locally, set both
variables in the same PowerShell session (see [Configuration](#configuration)):

```powershell
$env:AEGIS_ENVIRONMENT = "development"
$env:AEGIS_SIMULATION_API_ENABLED = "true"
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --reload
```

Without these variables the API still starts, with the read-only routes only.
`--host 127.0.0.1` keeps the server reachable from this machine only. That
limits who can connect; it is **not** authentication. Any local user or process
can still call the endpoint. See [Configuration](#configuration).

Then open:

- http://127.0.0.1:8000/ for service information
- http://127.0.0.1:8000/health for the health check
- http://127.0.0.1:8000/docs for interactive API documentation

### 4. Run the tests

```powershell
python -m pytest
```

## Simulation

The simulation engine produces synthetic telemetry for a three-service chain
(`api-gateway` → `orders-service` → `orders-db`) with one injected fault and
ground-truth labels. It is the test bed for detection and correlation work;
it does not read from any real system.

### Scenarios

| `scenario` | Injected fault | Most affected metrics |
|------------|----------------|-----------------------|
| `normal_operation` | None; no event is labelled anomalous | Baseline only |
| `database_latency_spike` | Database query latency ×12 | `db_query_latency_ms` (median 8 → 96 ms), `request_latency_ms` ×3, `active_db_connections` ≈ 35 |
| `cpu_saturation` | `orders-service` CPU saturates | `cpu_utilization_percent` ≈ 96 on `orders-service`, `request_latency_ms` ×2.5 |
| `api_errors` | HTTP errors rise | `http_error_count` ≈ 6 per tick on `api-gateway` and `orders-service`, `request_latency_ms` ×1.4 |
| `connection_failure` | DB connections fail | `db_connection_failures` ≈ 8 per tick on `orders-service`, `active_db_connections` ≈ 5, `request_latency_ms` ×1.5 |

### Fault window and ground truth

The fault is active for ticks `[ticks // 3, 2·ticks // 3)`, end exclusive. A run
shorter than three ticks gets one anomalous tick so every fault scenario has
ground truth. For example, a two-tick run has its fault window at tick 0 only:
`[start_time, start_time + tick_interval_seconds)`.

Every event in the window has `label.is_anomalous = true`, including metrics
the fault does not change. Labels are per window, not per metric.

`is_anomalous` is **ground truth**, not a detection result. It records when the
fault was injected, and it does not depend on the sampled value. `severity` is
**threshold-based**: it classifies each value against its metric's thresholds
and ignores the label. The two are independent, so an event can have
`is_anomalous: true` and `severity: "info"`. For example, under
`database_latency_spike` the request latency median rises to about 360 ms,
which is below the 400 ms warning threshold, so many fault-window
`request_latency_ms` events are `info`. Score detectors against
`is_anomalous`; do not use `severity` as the fault indicator.

`label.scenario_id` is the scenario of the whole run. It is set on baseline
events as well, so `scenario_id=database_latency_spike` also returns the
non-anomalous ticks of that run. Filter on `is_anomalous` to isolate the fault.

### Timestamps

Timestamps are `start_time + tick × tick_interval_seconds`, and the clock is
never read. The last timestamp of a run must be representable as a datetime,
so `start_time + ticks × tick_interval_seconds` must not pass
`9999-12-31T23:59:59.999999Z`. A `start_time` that would overflow, or that falls
before `0001-01-01T00:00:00Z` after conversion to UTC, is rejected with `422`.

### Determinism

All randomness comes from `random.Random(seed)`. The same validated request
always yields the same events. Runs that differ only in `scenario` share a seed,
so their events match exactly up to the start of the fault window. After that
the fault changes how many random draws some metrics consume, so the values
diverge. Compare runs before the window, not after it.

The run ID is a hash of the validated request. Repeating a request returns
`200` with the same summary and stores nothing new.

### `POST /api/v1/simulations`

**Local development only.** This endpoint has no authentication or
authorization. It changes server state by adding a run to the in-memory store,
so it must not be exposed on any shared, staging, or production host. It is
off by default and is refused in production; see [Configuration](#configuration).
Production authentication is not implemented yet; see
[architecture §5](docs/architecture.md#5-security-architecture-planned).

Request body (unknown fields are rejected):

| Field | Type | Required | Bounds / default |
|-------|------|----------|------------------|
| `scenario` | string | yes | one of the scenario IDs above |
| `seed` | integer | yes | 0 to 4294967295 |
| `ticks` | integer | yes | 1 to 288 |
| `tick_interval_seconds` | integer | no | 1 to 3600; default 60 |
| `start_time` | RFC 3339 timestamp with offset | no | default `2026-01-01T00:00:00Z`; normalized to UTC |

Responses:

- `201 Created`: a new run was generated and stored
- `200 OK`: an identical run already exists; nothing new was stored
- `422 Unprocessable Content`: validation failed, or the run is larger than the
  whole event store. The oversized case returns a structured body and stores
  nothing:

  ```json
  {
    "detail": {
      "code": "run_exceeds_capacity",
      "message": "the simulation run is larger than the event store can hold",
      "run_event_count": 2592
    }
  }
  ```

  The store capacity is not disclosed. With the default capacity of 50,000
  events, the largest possible run (288 ticks, 2,592 events) always fits.

Example (PowerShell):

```powershell
$body = @{
    scenario = "database_latency_spike"
    seed     = 7
    ticks    = 30
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/simulations `
    -ContentType "application/json" -Body $body
```

Response (abridged):

```json
{
  "run_id": "sim-f0d42ee8093d2f22",
  "scenario_id": "database_latency_spike",
  "seed": 7,
  "ticks": 30,
  "tick_interval_seconds": 60,
  "start_time": "2026-01-01T00:00:00Z",
  "event_count": 270,
  "anomalous_event_count": 90,
  "anomaly_window": {
    "start": "2026-01-01T00:10:00Z",
    "end": "2026-01-01T00:20:00Z"
  }
}
```

The response is a summary. Events are read through `GET /api/v1/events`.

### `GET /api/v1/events`

Query parameters, all optional:

| Parameter | Type | Notes |
|-----------|------|-------|
| `run_id` | string | Format `sim-` followed by 16 lowercase hex characters |
| `scenario_id` | string | One of the scenario IDs |
| `service` | string | Lowercase, starts with a letter, 2 to 63 characters |
| `metric_name` | string | One of the metric names listed below |
| `is_anomalous` | boolean | `true` or `false` |
| `limit` | integer | 1 to 500; default 100 |
| `offset` | integer | 0 or greater; default 0 |

Results are in insertion order (run, then tick). `total` is the number of
matches across all pages.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/events?is_anomalous=true&limit=5"
```

### Event contract

Each event has these fields:

| Field | Description |
|-------|-------------|
| `event_id` | `<run_id>-<6-digit index>`, unique within the store |
| `run_id` | Run that produced the event |
| `timestamp` | UTC, RFC 3339 with `Z` suffix |
| `service` | `api-gateway`, `orders-service`, or `orders-db` |
| `event_type` | `metric_sample` or `error_count` |
| `metric_name` | See table below |
| `value` | Finite number, zero or greater, rounded to 3 decimals |
| `unit` | `milliseconds`, `percent`, or `count` |
| `severity` | `info`, `warning`, or `critical`, from this event's value and the thresholds below. Independent of `label` |
| `label` | `{ "scenario_id": ..., "is_anomalous": ... }`. `is_anomalous` is injected ground truth; `scenario_id` is the run's scenario |

| Metric | Reported by | Unit | Event type | Warning ≥ | Critical ≥ |
|--------|-------------|------|------------|-----------|------------|
| `request_latency_ms` | `api-gateway`, `orders-service` | milliseconds | metric_sample | 400 | 800 |
| `http_error_count` | `api-gateway`, `orders-service` | count | error_count | 1 | 5 |
| `cpu_utilization_percent` | `orders-service`, `orders-db` | percent | metric_sample | 80 | 95 |
| `db_connection_failures` | `orders-service` | count | error_count | 1 | 5 |
| `db_query_latency_ms` | `orders-db` | milliseconds | metric_sample | 30 | 60 |
| `active_db_connections` | `orders-db` | count | metric_sample | 40 | 50 |

Each tick emits 9 events (`EVENTS_PER_TICK`), so a run emits `ticks × 9`.

### Limitations

- **In memory only.** Events are lost when the process stops. The store holds
  at most 50,000 events. When a new run would exceed that, the oldest runs are
  evicted whole, so a run is either fully visible or gone. Resubmitting an
  evicted request stores it again and returns `201`. Stored
  runs are not durable and are not shared between processes.
- **Unauthenticated, local development only.** `POST /api/v1/simulations`
  changes server state but has no authentication or authorization. Anyone who
  can reach the port can add runs and evict others. CLAUDE.md rule 6 and
  [architecture §5](docs/architecture.md#5-security-architecture-planned)
  require an accepted design before this kind of endpoint ships. Run the API
  only on a trusted local machine, bound to `127.0.0.1`.
- **Synthetic data only.** Distributions are simple and stationary apart from
  the injected fault. Real telemetry has seasonality, bursts, and correlation
  this model does not capture.
- **Step changes.** Faults start and stop abruptly. There is no ramp.
- **Window-level labels.** Each event in the window is labelled anomalous,
  even when its metric is unaffected by the fault. Severity still reflects each
  value, so an anomalous event can be `info`.
- **Fixed topology.** The three services and their metrics cannot be changed
  through the API.
- **Pagination is offset-based and scans linearly.** Fine at the store's size,
  but not for large volumes.

## Configuration

Settings are read from environment variables when the app starts. Invalid
values stop startup with a message that names the problem.

| Variable | Values | Default | Effect |
|----------|--------|---------|--------|
| `AEGIS_ENVIRONMENT` | `development`, `production` | `production` | Deployment environment. An unset value fails closed. |
| `AEGIS_SIMULATION_API_ENABLED` | `true`, `false` | `false` | Registers `POST /api/v1/simulations`. |

**Production guard.** The simulation endpoint has no authentication, so the
settings refuse to enable it in production. Setting
`AEGIS_ENVIRONMENT=production` together with `AEGIS_SIMULATION_API_ENABLED=true`
stops the app from starting. When the endpoint is disabled, its route does not
exist, so requests to it return `404`. This guard cannot be lifted by a
configuration change; it needs the authentication design in
[architecture §5](docs/architecture.md#5-security-architecture-planned).

**Loopback binding is not authentication.** `--host 127.0.0.1` makes the server
reachable only from the local machine. It does not identify callers, so every
local user and process can still create and evict simulation runs. Do not bind
to `0.0.0.0` or any other address while the endpoint is enabled.

Secrets must never be committed; see the security rules in
[CLAUDE.md](CLAUDE.md).

## Documentation

- [Architecture](docs/architecture.md): target design, components, and phases
- [CLAUDE.md](CLAUDE.md): coding conventions and security rules
- [frontend/README.md](frontend/README.md): frontend placeholder
- [infrastructure/README.md](infrastructure/README.md): infrastructure placeholder

## Contributing

1. Read [CLAUDE.md](CLAUDE.md) before making changes.
2. Keep each change small and covered by tests.
3. Run `python -m pytest` before opening a pull request.
4. Update documentation when behavior or scope changes.

## License

To be determined.
