# AEGIS AI

**Autonomous Enterprise Incident Intelligence Platform**

AEGIS AI is a platform for detecting, investigating, and resolving operational
incidents in enterprise environments. It correlates signals from monitoring
and operational systems, proposes root causes and remediation, and executes
approved actions under human supervision and with a full audit trail.

> **Status:** Foundation stage. The repository provides a FastAPI backend with
> service and health endpoints, an enterprise incident simulation engine that
> generates labelled synthetic telemetry in memory, a pytest suite, and
> placeholders for the frontend and infrastructure. Offline incident correlation
> over simulated data is implemented; investigation, approval, and remediation
> are planned.

## Features

Available now:

- `GET /`: service identity (name, version, status)
- `GET /health`: liveness probe for operators and orchestrators
- `POST /api/v1/simulations`: run a deterministic simulation with a seeded
  scenario and store its events in memory (see [Simulation](#simulation))
- `GET /api/v1/events`: page through stored events with filters
- `GET /api/v1/analytics/summary`: descriptive statistics per service, metric,
  and unit (see [Analytics](#analytics))
- `GET /api/v1/analytics/baselines`: normal-operation baseline statistics
- `GET /api/v1/analytics/timeseries`: one run's series with rolling statistics
- Offline charts from `scripts/plot_telemetry.py` (see
  [Running visualizations](#running-visualizations))
- Statistical anomaly detectors (z-score and robust) as a Python library, with
  held-out evaluation (see [Anomaly detection](#anomaly-detection))
- `GET /api/v1/incidents` and `GET /api/v1/incidents/{incident_id}`: read-only
  incidents correlated from detection alerts, with evidence and a timeline
  (see [Incident correlation](#incident-correlation))
- Automated tests with pytest

Planned (see [docs/architecture.md](docs/architecture.md)):

- Signal ingestion from external systems (the correlation runs on simulated data)
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
│   │   └── services/     Simulation engine, event store, analytics, and detectors
│   └── tests/        pytest suite
├── scripts/          Offline analysis scripts (charts, detector evaluation)
├── local/            Git-ignored local manifests (trusted baselines)
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

The core API needs only `requirements.txt`. The offline charts have their own
optional file; see [Running visualizations](#running-visualizations).

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

## Analytics

The analytics layer turns stored telemetry into statistics that a future anomaly
detector can be measured against. Its endpoints are read-only and change no
state. The layer does not detect anything yet.

### Why analytics comes before anomaly detection

A detector's output is only meaningful relative to what normal looks like. Before
any model is trained, the project needs known-good distributions per metric,
a reproducible split between data used to learn normal and data used to judge
it, and rolling statistics that show how a metric moves over time. Building
these first lets each later detector be scored on the same ground truth and
the same baseline, which makes its results comparable.

### Statistics and conventions

Every series is identified by `(service, metric_name, unit)`. Statistics are
never computed across series, so latencies in milliseconds and counts or
percentages cannot be combined by accident.

| Statistic | Definition |
|-----------|------------|
| `sample_count` | Number of finite, non-missing values |
| `missing_count` | Values excluded because they were `null` |
| `mean` | Sum divided by count, with compensated summation |
| `median` | Equal to `p50` |
| `minimum`, `maximum` | Smallest and largest value |
| `stdev` | Sample standard deviation, divisor n − 1; `null` when n < 2 |
| `p50`, `p95`, `p99` | Quantiles with linear interpolation (R-7, the NumPy and pandas default): position `h = (n − 1) · q`, interpolated between the two surrounding order statistics |

Edge cases:

- **Empty sample:** every statistic is `null` and `sample_count` is 0.
- **Single value:** mean, median, minimum, maximum, and percentiles equal that
  value; `stdev` is `null`, not 0, because a single value has no spread.
- **Zero variance:** `stdev` is exactly 0.
- **Missing values:** excluded and counted separately.
- **NaN, infinity, booleans, strings:** rejected with an error. They never
  reach a statistic.

Rolling statistics use a trailing window of `window` values, configurable from
2 to 60 (default 5). Until the window is full, `mean` and `stdev` are `null`
and `window_count` reports how many values exist, so a partial window is never
mistaken for a full one. A window of zero variance has `stdev` 0.

### Baselines and leakage control

A baseline is built only from events whose ground-truth label is not anomalous.
Labels are used to choose the normal data, and the baseline carries no labels.
The response reports the sample count, the source run IDs, and the selection
method, so a result can be traced to its inputs.

A `TrainEvaluationSplit` keeps training and evaluation apart:

- training and evaluation use disjoint runs, so a model never scores a run it
  learned from;
- training data is normal-labelled and strictly before a UTC cutoff;
- evaluation data comes from the held-out runs at or after the cutoff.

Exploratory and evaluation baselines are kept separate:

- `GET /api/v1/analytics/baselines` is for inspecting normal behaviour. Without
  `run_id` it uses every stored run, so it can include runs that an evaluation
  holds out. Its behaviour is unchanged for compatibility.
- An evaluation workflow must build its reference with
  `TrainEvaluationSplit.training_baseline(events)`. It uses only the training
  runs, normal-labelled and strictly before the cutoff, so a held-out run cannot
  change it. The tests in `backend/tests/test_baseline.py` check this.

### Analytics endpoints

All three are `GET`, read-only, and bounded. They never return a per-event
ground-truth label. They can still reveal label-derived information: the
`is_anomalous` and `scenario_id` filters on the summary return counts and
statistics that depend on the labels (for example, `event_count` with
`is_anomalous=true`). Use those filters to compare distributions, not as a
source of answers. Label isolation is not complete.

**`GET /api/v1/analytics/summary`** — statistics per series for the events that
match the filters. Filters: `run_id`, `scenario_id`, `service`, `metric_name`,
and `is_anomalous`. Use `is_anomalous` to compare normal and incident
distributions. The number of series is bounded by the topology (at most nine).

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/analytics/summary?is_anomalous=false"
```

**`GET /api/v1/analytics/baselines`** — normal-operation baseline. Optional
`run_id` (repeat up to 20 times) and `before` (an RFC 3339 timestamp with an
offset) limit the source events.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/analytics/baselines?before=2026-01-01T00:10:00Z"
```

**`GET /api/v1/analytics/timeseries`** — one page of one series from one run,
with rolling statistics. Required: `run_id`, `service`, `metric_name`. Optional:
`window` (2 to 60, default 5), `limit` (1 to 500, default 100), `offset`. The
rolling window is computed over the whole series before paging, so each page
carries the same statistics. An unmatched combination returns an empty page
with `total` 0.

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/analytics/timeseries?run_id=sim-f0d42ee8093d2f22&service=orders-db&metric_name=db_query_latency_ms&window=5&limit=20"
```

### Running visualizations

The charts use pandas and Matplotlib. They are listed in
`requirements-analytics.txt`, not `requirements.txt`, because the API, its tests,
and its deployment never import them. Keeping them optional stops the core
install from pulling in numpy and a plotting stack. Install both files into the
same virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r requirements-analytics.txt
python scripts\plot_telemetry.py
```

The script writes five PNG files to `artifacts/plots/` (ignored by Git):
`db_query_latency.png`, `api_response_latency.png`, `cpu_utilization.png`,
`latency_distribution.png` (normal vs incident), and
`rolling_mean_baseline.png` (rolling mean against a baseline from a separate
normal run). It uses fixed seeds, so reruns produce the same images.

### Assumptions and limitations

- Sample standard deviation and linear quantiles are conventions, chosen to match
  NumPy and pandas. Another convention gives slightly different percentiles.
- Baselines assume the simulator's stationary normal distribution. Real systems
  have seasonality and bursts that a single baseline does not capture.
- Labels mark each whole fault window, not each metric, so a normal-labelled
  event can still be affected by a fault in another metric. Baselines inherit
  that imprecision.
- Summaries and baselines read the whole in-memory store, which is bounded at
  50,000 events. Timeseries pages are computed over one run's series and are
  bounded by the 288-tick limit.
- Label isolation is partial. Label-filtered aggregates are reachable through
  the summary's `is_anomalous` and `scenario_id` filters, as they are through
  `/api/v1/events`.
- Future ML inference must not use `is_anomalous`, `scenario_id`, or any value
  derived from them as an input feature. Labels select normal data and score
  predictions; they are never model inputs.
- Nothing here sets alert thresholds. Thresholds belong to a later, documented
  detector, and each must state its assumptions.

## Anomaly detection

Two statistical detectors score each telemetry event as `normal`, `anomalous`,
or `unscored`. They are a Python library with no HTTP endpoint. A read-only API
is proposed but not built; see
[architecture §12.7](docs/architecture.md#127-proposed-read-only-detection-api-planned-not-implemented).
The full algorithms, leakage rules, and evaluation definitions are in
[architecture §12](docs/architecture.md#12-statistical-anomaly-detection).

### Detectors

| Detector | Center and spread | Score | Default threshold |
|----------|-------------------|-------|-------------------|
| `ZScoreDetector` | Training mean and sample stdev | `z = (x − mean) / stdev` | 3.0 |
| `RobustDetector` | Training median and MAD | `M = 0.6745 · (x − median) / MAD` | 3.5 |

Both accept a `direction`: `TWO_SIDED` (the default) flags values above or
below normal, and `UPPER_TAIL` flags only values above. Both group events by
`(service, metric_name, unit)` and need at least `min_training_samples`
values per series (default 10).

Unsupported input is never reported as normal. A series that was not in
training, a series with too few training values, and a non-finite value each
return `unscored` with a reason. A series whose training spread is zero
(stdev or MAD is 0) flags any deviation with reason `zero_spread_deviation`,
and has no score.

### Using a detector

Training and evaluation are separate steps. `run_experiment` enforces the
split, so use it rather than calling `fit` and `predict` by hand:

```python
from datetime import timedelta

from app.services.baseline import TrainEvaluationSplit
from app.services.detectors import RobustConfig, RobustDetector
from app.services.experiment import run_experiment

report = run_experiment(
    RobustDetector(RobustConfig(threshold=3.5)),
    split,        # TrainEvaluationSplit: training runs before a cutoff, held-out runs after
    events,       # TelemetryEvent objects from the simulator or the event store
)
print(report.metric_level.confusion, report.window_level.confusion)
```

`report.metric_level` scores each event against its label. `report.window_level`
scores each held-out run as one decision, and reports detection delay in seconds.

### Evaluation definitions

- **Metric level:** every scored event is one decision. Ground truth is the
  scenario-window label, which marks every metric in the fault window, so it is
  not evidence that each metric deviated.
- **Window level:** every held-out run is one decision. Alerts come from an
  incident rule over the per-tick verdicts. The rule can require several affected
  series and several consecutive ticks. The default rule alerts on any anomalous
  verdict. Consecutive ticks must be one telemetry interval apart (60 seconds by
  default), so a missing tick breaks the streak. A run is detected when an alert
  falls inside its labelled fault window. Alerts before the window count as
  metric-level false positives, are reported as pre-incident alerts, and do not
  detect the window.
- **Effect level:** an event is anomalous only if it is in a fault window and its
  series is one the fault changes. This separates misses from metrics the fault
  never moved.
- **Detection delay:** seconds from the first anomalous tick of a window to the
  first alert inside it. Resolution is one tick (60 seconds by default).
- **Undefined rates:** precision, recall, F1, and false-positive rate are shown
  as `n/a` when their denominator is zero. They are never 0 or 1 by default.
- **Unscored events** are excluded from the matrices and counted separately.

### Reproducing the experiment

The script fits on fault-free training runs. It chooses one incident rule per
detector on fifty validation runs, then selects the preferred detector on
validation, and then scores 100 final runs once (seeds 401–420). Every seed is
fixed. It needs only the core install:

```powershell
cd C:\Projects\aegis-ai
.\.venv\Scripts\Activate.ps1
python scripts\evaluate_detectors.py
```

Rerunning it prints identical output. The results and their limitations are in
[architecture §12.8](docs/architecture.md#128-results-on-the-simulator). The
main findings: the robust detector flags every nonzero count on sparse series
whose training MAD is 0. Under the any-positive policy every normal run raised
alerts. The chosen persistence rules alerted on no validation normal run, at the
cost of detection delay. The preferred detector is the z-score upper-tail with
series ≥ 3 and ticks ≥ 1, chosen on validation F1 and then delay. The robust
upper-tail with series ≥ 3 and ticks ≥ 2 is kept as a comparison. Section 12.11
gives the frozen final results.

### Limitations

- Results describe synthetic, stationary data with step faults. They are not
  evidence about production telemetry.
- Labels mark whole fault windows, so metric-level recall is capped by metrics
  the fault does not change.
- Detectors assume each series is stationary, and points are scored
  independently. Real telemetry has seasonality, drift, and correlated ticks.
- The zero-spread policy and the default thresholds were chosen before the
  held-out evaluation and were not tuned on it.

## Incident correlation

The correlation layer groups detection alerts into incidents, attaches the
stored observations that support them, and builds a timeline. It is an offline
pipeline (`backend/app/services/incident_pipeline.py`) behind two read-only
endpoints. It uses the Day 4 incident rule and detector without changes.

> **Correlation is not causation.** Each incident records that alerts happened
> close together on services that call each other. It does not say which
> service caused the others. `root_cause_assessment` is always `not_assessed`.

### Correlation rules

Two alerts in the same run are linked when both hold:

1. they are at most 600 seconds apart (inclusive), and
2. their services are equal, or one calls the other in the declared topology
   (`api-gateway -> orders-service -> orders-db`).

An incident is a group of alerts linked directly or through a chain of related
alerts. Runs are never merged. The 600-second default is a tuned value, not a
validated one (see the limitations in
[docs/architecture.md](docs/architecture.md#1310-limitations)).

### Baselines

Every request names the normal runs the detector learns from with
`baseline_run_id` (one to eight, repeatable). A baseline is accepted only if it
was approved offline. No request can approve, add, or change a baseline. An
unapproved run is refused with `403` and the code `baseline_not_approved`, before
the store is consulted.

Approval is a local development workflow:

```powershell
# 1. Approve candidate normal runs offline. A candidate with any injected fault
#    stops the script, and no manifest is written.
python scripts/approve_baselines.py --seeds 101 102 103 104 --ticks 288

# 2. Start the API with the manifest. It re-simulates and re-checks every entry
#    on load, and refuses the whole file if any entry fails.
$env:AEGIS_TRUSTED_BASELINES_PATH = "local/trusted_baselines.json"
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --reload

# 3. Create each approved run with POST /api/v1/simulations, using the same
#    scenario, seed, ticks, and cadence. The run ID is derived from the spec, so
#    it matches the manifest. Then pass it as baseline_run_id.
```

The synthetic check uses ground-truth labels, offline. It proves only that a
synthetic baseline contains no injected fault. It does not prove that the run is
representative of production traffic (see
[docs/architecture.md](docs/architecture.md#1312-trusted-baselines)). The
manifest is a development feature. Settings refuse it in production, where no
baseline is trusted, so every incident request is refused.

### Endpoints

```text
GET /api/v1/incidents?baseline_run_id=<run>[&run_id=<run>][&limit=50][&offset=0]
GET /api/v1/incidents/{incident_id}?baseline_run_id=<run>
```

The list returns summaries, ordered by first detection and then by ID. The
detail returns the full incident with `evidence`, `timeline`, and
`telemetry_gaps`. Errors have a fixed `code` and no internal detail:

| Status | Code | Meaning |
|-------:|------|---------|
| 403 | `baseline_not_approved` | A named baseline is not on the trusted list |
| 404 | `baseline_run_not_found` | An approved baseline is not in the event store |
| 404 | `incident_not_found` | No incident with that ID for the given baselines |

A sample detail response, from the API with two approved baselines and a
`cpu_saturation` run, trimmed to three of 293 evidence records (one per
inclusion reason) and two of the timeline entries:

```json
{
  "incident_id": "inc-b5cb080ba8a5c34d",
  "status": "detected",
  "root_cause_assessment": "not_assessed",
  "source_run_id": "sim-c8ffd0c14e1c496c",
  "baseline_run_ids": [
    "sim-29689797753d5dc4",
    "sim-adc625dd6a5e7fe5"
  ],
  "detector": "zscore",
  "rule": "series>=3 ticks>=1",
  "correlation_window_seconds": 600,
  "first_observation_at": "2026-01-01T01:36:00Z",
  "first_detection_at": "2026-01-01T01:36:00Z",
  "latest_detection_at": "2026-01-01T03:11:00Z",
  "latest_observation_at": "2026-01-01T03:11:00Z",
  "alert_count": 96,
  "affected_services": [
    "api-gateway",
    "orders-service"
  ],
  "affected_metrics": [
    "cpu_utilization_percent",
    "db_connection_failures",
    "http_error_count",
    "request_latency_ms"
  ],
  "correlation_rationale": [
    {
      "kind": "temporal_proximity",
      "detail": "96 alert ticks linked by a chain of related alerts at most 600 seconds apart"
    },
    {
      "kind": "dependency_link",
      "detail": "api-gateway calls orders-service in the declared topology"
    },
    {
      "kind": "related_metric_behavior",
      "detail": "5 series anomalous together across 2 service(s); metrics: cpu_utilization_percent, db_connection_failures, http_error_count, request_latency_ms"
    },
    {
      "kind": "not_causal",
      "detail": "co-occurrence in time and in the declared topology does not establish which service caused the others"
    }
  ],
  "evidence": [
    {
      "source_event_id": "sim-c8ffd0c14e1c496c-000778",
      "run_id": "sim-c8ffd0c14e1c496c",
      "service": "orders-service",
      "metric_name": "http_error_count",
      "unit": "count",
      "severity": "warning",
      "observed_at": "2026-01-01T01:26:00Z",
      "observed_value": 2.0,
      "inclusion_reason": "pre_incident_context",
      "baseline_comparison": {
        "detector": "zscore",
        "direction": "upper_tail",
        "threshold": 3.0,
        "score": 3.798207913758143,
        "training_run_ids": [
          "sim-29689797753d5dc4",
          "sim-adc625dd6a5e7fe5"
        ]
      }
    },
    {
      "source_event_id": "sim-c8ffd0c14e1c496c-000865",
      "run_id": "sim-c8ffd0c14e1c496c",
      "service": "api-gateway",
      "metric_name": "request_latency_ms",
      "unit": "milliseconds",
      "severity": "info",
      "observed_at": "2026-01-01T01:36:00Z",
      "observed_value": 291.563,
      "inclusion_reason": "contributed_to_alert",
      "baseline_comparison": {
        "detector": "zscore",
        "direction": "upper_tail",
        "threshold": 3.0,
        "score": 9.794043816616199,
        "training_run_ids": [
          "sim-29689797753d5dc4",
          "sim-adc625dd6a5e7fe5"
        ]
      }
    },
    {
      "source_event_id": "sim-c8ffd0c14e1c496c-001731",
      "run_id": "sim-c8ffd0c14e1c496c",
      "service": "orders-service",
      "metric_name": "request_latency_ms",
      "unit": "milliseconds",
      "severity": "info",
      "observed_at": "2026-01-01T03:12:00Z",
      "observed_value": 176.993,
      "inclusion_reason": "anomalous_in_window",
      "baseline_comparison": {
        "detector": "zscore",
        "direction": "upper_tail",
        "threshold": 3.0,
        "score": 3.043937015786426,
        "training_run_ids": [
          "sim-29689797753d5dc4",
          "sim-adc625dd6a5e7fe5"
        ]
      }
    }
  ],
  "timeline": [
    {
      "timestamp": "2026-01-01T01:36:00Z",
      "kind": "observation",
      "milestone": "earliest_supporting_observation",
      "service": "api-gateway",
      "metric_name": "request_latency_ms",
      "source_event_id": "sim-c8ffd0c14e1c496c-000865",
      "description": "api-gateway request_latency_ms observed 291.563 milliseconds (info)"
    },
    {
      "timestamp": "2026-01-01T03:11:00Z",
      "kind": "observation",
      "milestone": "latest_observed_evidence",
      "service": "orders-service",
      "metric_name": "request_latency_ms",
      "source_event_id": "sim-c8ffd0c14e1c496c-001722",
      "description": "orders-service request_latency_ms observed 341.926 milliseconds (info)"
    }
  ],
  "telemetry_gaps": []
}
```

The full response includes every evidence record and every timeline entry.

### Evidence and timestamps

- Each evidence record names the stored event it came from (`source_event_id`)
  and copies that event's value and timestamp. Nothing is invented.
- Each record has one `inclusion_reason`, and only the first is a qualifying
  detection:
  - `contributed_to_alert`: a qualifying detection. The sample was taken at an
    alert tick.
  - `anomalous_in_window`: an anomalous measurement after the first detection
    that did not qualify an alert.
  - `pre_incident_context`: an anomalous sample before the first detection. It is
    context only. It is often noise, and it is not proof that the incident was
    already under way, or of its cause. It never sets observation times or the
    timeline.
- `first_observation_at` and `latest_observation_at` are the first and last
  qualifying samples. They sit at alert ticks, so they equal the first and latest
  detection times.
- `*_detection_at` fields are ticks at which the rule decided to alert.
- A zero-spread deviation has evidence but `baseline_comparison: null`, because
  it has no finite score.

### Window limits

The 600-second window is provisional. It was chosen on a small set of simulator
runs and has not been validated on held-out data. On synthetic validation seeds
it produced no false merges and no false splits, but a shorter window splits
faults, and a longer window could merge unrelated alerts in production telemetry.
Reproduce the synthetic sweep with `python scripts/evaluate_correlation_window.py`.
Details are in [docs/architecture.md](docs/architecture.md#1313-correlation-window-and-error-types).


### Verifying the pipeline (Windows PowerShell)

Run the suite from the repository root:

```powershell
cd C:\Projects\aegis-ai
.\.venv\Scripts\python.exe -m pytest -q
```

Approve the two normal baseline seeds offline. This writes
`local/trusted_baselines.json`, which the API reads at startup. A fault run cannot
be approved, and the script stops if any candidate contains an injected fault:

```powershell
.\.venv\Scripts\python.exe scriptspprove_baselines.py --seeds 301 302 --ticks 288
```

Run the API locally with the simulation endpoint enabled, and with the approved
baselines loaded. Both variables are development-only:

```powershell
$env:AEGIS_ENVIRONMENT = "development"
$env:AEGIS_SIMULATION_API_ENABLED = "true"
$env:AEGIS_TRUSTED_BASELINES_PATH = "local	rusted_baselines.json"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

In a second PowerShell window, create the two approved normal baseline runs and
one fault run, then list the incidents. The run IDs come from each `POST` response.
A fault run passed as a baseline gets `403 baseline_not_approved`:

```powershell
$base = "http://127.0.0.1:8000/api/v1"
$json = "application/json"
$b1 = (Invoke-RestMethod -Method Post -Uri "$base/simulations" -ContentType $json -Body '{"scenario":"normal_operation","seed":301,"ticks":288}').run_id
$b2 = (Invoke-RestMethod -Method Post -Uri "$base/simulations" -ContentType $json -Body '{"scenario":"normal_operation","seed":302,"ticks":288}').run_id
$fault = (Invoke-RestMethod -Method Post -Uri "$base/simulations" -ContentType $json -Body '{"scenario":"cpu_saturation","seed":311,"ticks":288}').run_id
Invoke-RestMethod -Uri "$base/incidents?baseline_run_id=$b1&baseline_run_id=$b2"
```

To inspect one incident, pass its `incident_id` from that list together with the
same baseline parameters:

```powershell
$incidentId = "<incident_id from the list>"
Invoke-RestMethod -Uri "$base/incidents/$($incidentId)?baseline_run_id=$b1&baseline_run_id=$b2"
```

## Configuration

Settings are read from environment variables when the app starts. Invalid
values stop startup with a message that names the problem.

| Variable | Values | Default | Effect |
|----------|--------|---------|--------|
| `AEGIS_ENVIRONMENT` | `development`, `production` | `production` | Deployment environment. An unset value fails closed. |
| `AEGIS_SIMULATION_API_ENABLED` | `true`, `false` | `false` | Registers `POST /api/v1/simulations`. |
| `AEGIS_TRUSTED_BASELINES_PATH` | Path to a manifest | Unset | Loads approved baselines for incident detection. Development only. Unset trusts no baseline. |

**Production guard.** The simulation endpoint has no authentication, so the
settings refuse to enable it in production. Setting
`AEGIS_ENVIRONMENT=production` together with `AEGIS_SIMULATION_API_ENABLED=true`
stops the app from starting. When the endpoint is disabled, its route does not
exist, so requests to it return `404`. This guard cannot be lifted by a
configuration change; it needs the authentication design in
[architecture §5](docs/architecture.md#5-security-architecture-planned).

**Trusted baselines.** `AEGIS_TRUSTED_BASELINES_PATH` names a manifest written by
`scripts/approve_baselines.py`. The app re-checks every entry on load and refuses
the whole file if any entry is faulty or altered. Setting it in production stops
startup, the same way the simulation guard does. Without it, every incident request
that names a baseline is refused with `403`.

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
