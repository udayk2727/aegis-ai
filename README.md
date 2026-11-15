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
- `GET /api/v1/demo/baselines`: development-only, read-only listing of stored
  approved baselines for the demo workflow (see [Demo workflow](#demo-workflow))
- `GET /api/v1/incidents/{incident_id}/investigation`: read-only, deterministic
  investigation report with verified observations, ranked hypotheses,
  contradictory and missing evidence, and read-only checks
  (see [Investigation](#investigation))
- Investigation agent library and a local-only CLI. The default provider is
  deterministic and makes no network call. An optional OpenAI provider runs only
  with `--provider openai --live`. There is no HTTP endpoint
  (see [Investigation agent](#investigation-agent))
- Automated tests with pytest

Planned (see [docs/architecture.md](docs/architecture.md)):

- Signal ingestion from external systems (the correlation runs on simulated data)
- Live validation of the OpenAI provider adapter against the API. The adapter
  exists and is tested offline, but it has not been run against the live service
- Broader live validation of the Anthropic adapter. One single-case run is recorded;
  other scenarios, seeds, and repeat runs are not
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
│   │   └── services/     Simulation engine, event store, analytics, detectors,
│   │                     incident correlation, and investigation
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
.\.venv\Scripts\python.exe scripts\approve_baselines.py --seeds 301 302 --ticks 288
```

Run the API locally with the simulation endpoint enabled, and with the approved
baselines loaded. Both variables are development-only:

```powershell
$env:AEGIS_ENVIRONMENT = "development"
$env:AEGIS_SIMULATION_API_ENABLED = "true"
$env:AEGIS_TRUSTED_BASELINES_PATH = "local\trusted_baselines.json"
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

To produce the investigation report for that incident, and to run the offline
evaluation of every investigation (see [Investigation](#investigation)):

```powershell
$incident = (Invoke-RestMethod -Uri "$base/incidents?baseline_run_id=$b1&baseline_run_id=$b2&run_id=$fault").items[0].incident_id
Invoke-RestMethod -Uri "$base/incidents/$incident/investigation?baseline_run_id=$b1&baseline_run_id=$b2" | ConvertTo-Json -Depth 8
.\.venv\Scripts\python.exe scripts\evaluate_investigations.py
```

## Demo workflow

This is the local, read-only path a dashboard uses to browse prepared synthetic
incidents and open their investigations. It is development only. It uses the
simulation endpoint to prepare data, then only reads.

**Prepare the data.** Approve the baselines offline, then start the API with the
demo listing on. The three variables are development-only:

```powershell
cd C:\Projects\aegis-ai
.\.venv\Scripts\python.exe scripts\approve_baselines.py --seeds 301 302 --ticks 288
$env:AEGIS_ENVIRONMENT = "development"
$env:AEGIS_SIMULATION_API_ENABLED = "true"
$env:AEGIS_DEMO_API_ENABLED = "true"
$env:AEGIS_TRUSTED_BASELINES_PATH = "local\trusted_baselines.json"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

**Browse the incidents.** In a second PowerShell window, the store starts empty, so
the first listing returns no baselines. Then create the two approved baselines and
one fault run, and list the baselines again:

```powershell
$base = "http://127.0.0.1:8000/api/v1"
$json = "application/json"
Invoke-RestMethod -Uri "$base/demo/baselines"   # items: [], total: 0
Invoke-RestMethod -Method Post -Uri "$base/simulations" -ContentType $json -Body '{"scenario":"normal_operation","seed":301,"ticks":288}' | Out-Null
Invoke-RestMethod -Method Post -Uri "$base/simulations" -ContentType $json -Body '{"scenario":"normal_operation","seed":302,"ticks":288}' | Out-Null
Invoke-RestMethod -Method Post -Uri "$base/simulations" -ContentType $json -Body '{"scenario":"cpu_saturation","seed":311,"ticks":288}' | Out-Null
$baselines = (Invoke-RestMethod -Uri "$base/demo/baselines").items.run_id
$query = ($baselines | ForEach-Object { "baseline_run_id=$_" }) -join "&"
Invoke-RestMethod -Uri "$base/incidents?$query"
```

**Open one investigation.** Copy an `incident_id` from the list and fetch its
report with the same baselines. Hypotheses are always `unverified`, and
`root_cause_assessment` is always `not_assessed`:

```powershell
$incidentId = "<incident_id from the list>"
Invoke-RestMethod -Uri "$base/incidents/$incidentId/investigation?$query" | ConvertTo-Json -Depth 8
```

An unknown ID returns `404 incident_not_found`. An empty store returns
`{"items": [], "total": 0}` from the listing, not an error.

**The same steps in Swagger UI.** Open <http://127.0.0.1:8000/docs>:

1. Run `POST /api/v1/simulations` three times with the bodies above and note
   each `run_id` from the response.
2. Run `GET /api/v1/demo/baselines` and note the `run_id` values in `items`.
3. Run `GET /api/v1/incidents`. Under `baseline_run_id`, Swagger UI shows an
   "Add string item" control. Add one entry per baseline run ID.
4. Copy an `incident_id` from the response. Run
   `GET /api/v1/incidents/{incident_id}/investigation` with the same baseline
   run IDs.

**Limitations.** The store is in memory, so a restart clears it and the steps
must be repeated. The demo listing is registered only in development, and the
manifest is a local file. All data is synthetic and is not production telemetry.
The read endpoints do not call a language model, start a simulation, or perform
remediation. For the paid agent path, use the CLI in
[Investigation agent](#investigation-agent).

## Investigation

The deterministic investigation layer reads one incident and the verdicts for its source run,
then produces a structured report. The report separates what was measured from
the patterns that measurement might fit, and it says what is missing. It is
deterministic, read-only, and offline. It calls no language model and sends no
telemetry anywhere. The LLM investigation agent is a separate, optional layer over
these reports, described under [Investigation agent](#investigation-agent). Its code is in `backend/app/services/investigation*.py`
and `hypotheses.py`, and the design is in
[docs/architecture.md](docs/architecture.md#14-evidence-based-investigation).

> **Correlation and timing do not prove causation.** A hypothesis is a candidate
> pattern. Every hypothesis is `unverified`, and `root_cause_assessment` is always
> `not_assessed`. The report names what would be needed to test each one.

### What a report contains

| Part | Meaning |
|------|---------|
| `verified_observations` | Facts computed from stored events. Each cites the event IDs it came from. |
| `hypotheses` | Candidate patterns, ranked. Each has a `role`, lists supporting and contradicting observation IDs, and names the other hypotheses generated for the same incident. |
| `ranking_caveat` | The limits of the ordering, stated on every report. If a downstream symptom holds rank 1 while a potentially initiating fault was also generated, the caveat names both. |
| `contradictory_evidence` | An observation a hypothesis expected but did not get, or one that came out of order. |
| `missing_evidence` | What could not be checked, such as query traces, which the telemetry model does not collect. |
| `suggested_checks` | Read-only follow-up checks. None changes state or touches a live system. |
| `limitations` | Standing limits of the analysis. |

Each hypothesis `role` is one of `potential_initiating_fault` or
`downstream_symptom`. A downstream symptom is an effect that a fault elsewhere
would produce. `api_error_propagation` is the only such rule. The role is fixed by
the rule catalog and does not change the ranking.

Cited event lists can be bounded (see [Response bounds](#response-bounds)). When a
list is shortened, its total is kept next to it, and a `*_truncated` flag says
so.

### Verified observations

There are three kinds. Each is computed from stored events, and each cites the
event IDs it came from:

- `anomalous_series`: a series with measurements that contributed to an alert.
  Context samples from before onset do not count, but the statement reports how
  many were excluded, along with the detector's score range against its
  threshold.
- `normal_series`: a series with normal verdicts in the span and no alert
  contribution. The statement says so. It does not claim the series never moved.
- `onset_order`: the first alert-contributing measurement of two series, in the
  order they occurred.

### Evidence provenance

Every cited reference is checked before the report is returned, by
`app/services/investigation_provenance.py`. The check requires that:

- each cited event exists in the stored run,
- the event belongs to the source run,
- the event is on a series the observation names,
- each observation, and each event it cites, falls inside the analysis span,
- each hypothesis cites only events that a supporting observation lists, and
  every observation it references exists in the report.

A report that fails any check is not returned. The API answers with the generic
`500 analysis_failed` and logs the first violation on the server. The offline
evaluator uses the same check, so the numbers in the evaluation below test the
code the API runs.

### Hypothesis rules

Each rule is a fixed declaration. A rule applies only when its primary signal
contributed to alerts at two or more ticks, and each competing signal is measured
normal. A single noisy sample is not treated as a pattern.

| Hypothesis | Primary signal | Expects | Must be normal |
|------------|----------------|---------|----------------|
| `database_query_performance_degradation` | `db_query_latency_ms` on orders-db | `request_latency_ms` on orders-service, after the database signal | none |
| `cpu_resource_saturation` | `cpu_utilization_percent` on orders-service | `request_latency_ms` on orders-service, after the CPU signal | none |
| `application_processing_bottleneck` | `request_latency_ms` on orders-service | none | CPU on orders-service, database query latency |
| `api_error_propagation` | `http_error_count` on orders-service | `http_error_count` on api-gateway | none |
| `database_connectivity_problem` | `db_connection_failures` on orders-service | none | none |

A corroborating signal that is measured normal is recorded as contradicting
evidence. So is an onset order that runs backwards. Temporal precedence is
necessary for a causal reading, but it is not sufficient.

Hypotheses are ranked by fewer contradicting observations, then more
anomaly-based supporting observations, then fixed catalog order. This orders
candidates for review. It is not a probability.

**Ranking limitations.** The count favours a rule with more corroborating
series. In connection-failure runs, the downstream `api_error_propagation` ranks
above the potentially initiating `database_connectivity_problem`. Each report
states this in `ranking_caveat`, and the role field marks the symptom. Every
generated hypothesis is kept with its alternatives. The ranking was not changed
after the held-out results were viewed, so it is not tuned on those seeds. A
causal ranking that weighs specificity is future work. It needs its own design and
its own validation on seeds that were not viewed.

**Choice of `MIN_PRIMARY_ALERT_TICKS`.** A rule applies only when its primary
signal contributed to alerts at two or more ticks. This is one constant in
`investigation_catalog.py`. It was chosen after one probe of the simulator's
fault scenarios on seeds 301, 302, and 311. Those are development seeds, and seeds
301 and 302 are also the local baseline seeds. None of them is an evaluation seed.
The probe showed that single-tick matches were noise, which is why the rule exists.
The value was not tuned on the evaluation seeds 601 to 610.

### Endpoint

```text
GET /api/v1/incidents/{incident_id}/investigation?baseline_run_id=<run>[&baseline_run_id=<run>]
```

It applies the same baseline trust as the incident endpoints. An unapproved
baseline returns `403 baseline_not_approved`. An unstored one returns
`404 baseline_run_not_found`, and an unknown incident returns
`404 incident_not_found`. A malformed incident ID or a missing baseline returns
`422`. A failed analysis, including a provenance violation, returns
`500 analysis_failed` with a fixed message and no detail. Only `GET` is allowed,
so other methods return `405`.

### Response bounds

Each cited event list is capped at `investigation.max_cited_events`, which
defaults to 100 and can be set with `AEGIS_INVESTIGATION_MAX_CITED_EVENTS`
(1 to 1000). The cap changes only which IDs are listed. Each capped list keeps its
total next to the listed IDs: `source_event_count` on an observation, a
contradiction, or a gap, and `supporting_event_count` on a hypothesis. A
`*_truncated` flag sits beside each total and must agree with the counts. The
report sets `evidence_truncated` when any list is shortened. Ranking, statements, and status are computed before the cap
and do not change with it.

The cap bounds the listed references, not the work. Each request still fits the
detector twice and scores the store, which is capped at 50,000 events. Caching is
future work.

Abridged output for the database-latency fault at 288 ticks, seed 311, with
baselines 301 and 302:

```json
{
  "status": "analyzed",
  "root_cause_assessment": "not_assessed",
  "hypotheses": [
    {
      "kind": "database_query_performance_degradation",
      "rank": 1,
      "status": "supported",
      "verification_status": "unverified",
      "supporting_observation_ids": [
        "obs-anomalous_series-orders-db-db_query_latency_ms",
        "obs-anomalous_series-orders-service-request_latency_ms",
        "obs-onset_order-orders-db-db_query_latency_ms-orders-service-request_latency_ms"
      ],
      "contradicting_observation_ids": [],
      "alternative_kinds": []
    }
  ],
  "suggested_checks": [
    {"kind": "review_query_traces", "read_only": true},
    {"kind": "review_change_records", "read_only": true}
  ]
}
```

### Evaluation

`scripts/evaluate_investigations.py` scores every investigation offline against
the simulator's labels. It uses seeds 601 to 610 at 60 ticks, which were not used
to choose any rule. Baselines are seeds 101 to 104. Output from the default run:

| Scenario | Runs | Incidents | In window | False positives | Provenance failures | Unsupported hypotheses | Irreproducible runs | Expected pattern present | Expected ranked first | Contradictions |
|----------|-----:|----------:|----------:|----------------:|--------------------:|-----------------------:|--------------------:|-------------------------:|----------------------:|---------------:|
| normal_operation | 10 | 0 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 |
| database_latency_spike | 10 | 10 | 10 | 0 | 0 | 0 | 0 | 10 | 10 | 1 |
| cpu_saturation | 10 | 10 | 10 | 0 | 0 | 0 | 0 | 10 | 10 | 1 |
| api_errors | 10 | 10 | 10 | 0 | 0 | 0 | 0 | 10 | 10 | 0 |
| connection_failure | 10 | 11 | 10 | 1 | 0 | 0 | 0 | 10 | 0 | 0 |

An incident is **in window** when its alert ticks overlap the span the simulator
labelled anomalous. Otherwise it is a **false positive**. The evaluation reads
labels, so this count is offline only. The request path never sees it.

**Why 10 connection-failure runs produced 11 incidents.** Seed 606 has two
incidents. The first is the fault's own, with 19 alert ticks inside the labelled
window. The second is one alert tick at 00:59, 20 minutes after the window closed,
where three series crossed the threshold together. The detector's selected rule
accepts a single tick (`min_consecutive_ticks=1`, the Day 4 choice), so this is an
ordinary false positive. The detector was not changed. Its report has no
hypotheses and is `insufficient_evidence`, which is the correct result for it.
The other nine connection-failure runs produce exactly one incident each.

What these results show:

- Provenance, support, and reproducibility hold in every run. Normal runs produce
  no incident, so they produce no investigation.
- The matching pattern is generated in every fault run. It ranks first in the
  database, CPU, and API-error runs.
- **Connection failure is the weak case.** `database_connectivity_problem` is
  generated in all ten runs, but `api_error_propagation` ranks first in each.
  The ranking counts observations, and the API rule has two anomalous error
  series against the connectivity rule's one. The ranking was not changed, because
  changing it on the evaluation seeds would tune on held-out data. Each report now
  says so in `ranking_caveat`. Choosing a causal ranking is an owner decision.
- Contradictions are rare on the simulator, because its faults are clean step
  changes. The one per set in the database and CPU runs is an
  `api_error_propagation` hypothesis built from orders-service error ticks, with
  the gateway measured normal.

Scenario agreement is not causal accuracy. The simulator's fault type is a label,
not a verified cause. A match shows that the rules fire on the signals they were
designed for. It does not show that they would find the cause of a real incident.

### Limitations

- The rules and the two-tick threshold were written by the author. The threshold
  was chosen after a single look at the simulator's output on test seeds 301, 302,
  and 311. Other choices could give different results.
- The simulator's faults are large, step-shaped, and affect one tier at a time.
  Production telemetry will be noisier and less clean.
- Several hypotheses cannot be checked from stored data: query traces, application
  logs, host metrics, connection pool limits, and change records are not in the
  telemetry model. The report lists each one as missing evidence.
- The upper-tail detector does not flag a fall in `active_db_connections`, so a
  downward change cannot appear as evidence. The report says so.
- Ranking counts observations and does not weigh specificity. It is a review aid
  only, and the report says so in `ranking_caveat`.
- False-positive counts come from one simulated seed set. They do not estimate a
  production false-positive rate.
- The endpoint has no authentication and no runtime guard. It is registered in
  every environment (`app/main.py`), and it is not gated by the environment
  setting. In production it returns 403 for any baseline request, because trusted
  baselines cannot be loaded there. That is an indirect effect, not a guard, and
  code that builds the app with its own `TrustedBaselines` would expose it. Run
  it only on a trusted local machine until the authentication design in
  [architecture.md section 5](docs/architecture.md#5-security-architecture-planned)
  is accepted. It recomputes everything from the in-memory store on each
  request, and the recomputation cost is not bounded beyond the store capacity.

### Investigation agent

The LLM investigation agent runs as a Python library and a local-only CLI. It has
no HTTP endpoint. Its design is in
[architecture.md section 14.9](docs/architecture.md#149-llm-investigation-agent-fake-and-optional-live-provider).

#### Providers

| Provider | Selected by | Network | SDK pin (`requirements-llm.txt`) | Use |
|----------|-------------|---------|----------------------------------|-----|
| `fake` (default) | nothing | none | none | tests and local demonstrations |
| `openai` | `--provider openai --live` | OpenAI Responses API | `openai==2.54.0` | local development only |
| `anthropic` | `--provider anthropic --live` | Anthropic Messages API | `anthropic==0.109.0` | local development only |

A live provider runs only with its explicit flags. Pytest, CI, ordinary imports,
and the default evaluation never use one. Install the optional SDKs after the core
requirements:

```powershell
python -m pip install -r requirements-llm.txt
```

#### Configuration

Credentials and the model come from the environment only. Nothing is read from a
source file or a `.env` file.

| Variable | Needed for | Notes |
|----------|-----------|-------|
| `ANTHROPIC_API_KEY` | `anthropic` | Never logged, printed, or put in a prompt |
| `OPENAI_API_KEY` | `openai` | Never logged, printed, or put in a prompt |
| `AEGIS_LLM_MODEL` | both live providers | No default. Use a model identifier you have chosen. Anthropic's model overview lists `claude-sonnet-5-5` as the Sonnet 5.5 API ID. |

Set a key in your own session from your secret store, for example
`$env:ANTHROPIC_API_KEY = "<key from your secret store>"`. Do not commit it.

Cost estimates need a pricing file, passed with `--pricing-file`. The repository
ships none. Without one, cost is reported as unknown, never as zero. The file format
is described in the Cost and usage section below.

Exit codes: `0` verified, `2` refused, `1` no incident was detected, and `3` a
required setting is missing or invalid, a budget is out of range, or `--live` was not
given.

#### Offline procedures (no network, no key)

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\evaluate_agent.py
.\.venv\Scripts\python.exe scripts\check_llm_environment.py
```

The preflight checks both SDKs: the Python version, the installed version against
the pin, the import, client construction with a dummy key, and the resource. It sends
no request. A pass means the SDK loads here. It does not mean a live adapter works.

#### Offline evaluation matrix and lifecycle trace (Day 8)

The offline evaluation runs 240 cases over four synthetic scenarios and seeds 601,
602, and 603, with the fake provider only. Its 240 cases are split into two groups
with separate denominators:

| Group | Cases | Meaning | Met |
|-------|-------|---------|-----|
| Verified completion | 24 | Built to verify: the reference answer, and the reference after refused tool requests | 24/24 |
| Expected failure handling | 216 | Built to stop or reject safely: 168 faulty answers, 12 budget stops, 12 timeouts, 12 refusals, 12 failures after a successful tool call | 216/216 |

Metric definitions:

- **Provider turns:** each attempted provider invocation, counted by the
  orchestrator. An invocation that raises is counted. A stop before an invocation is
  not. Fake runs count turns, and they send no transport request.
- **Transport requests:** the provider requests the transport admitted and sent. The
  fake provider sends none, so this count is zero for every offline run.
- **Unsafe acceptance:** a faulty answer that verifies, or a verified claim whose text
  is not canonical. Both must be zero. The offline run measured zero.
- **Cost:** unknown for the fake provider. It is never reported as zero.
- **Tokens and latency:** tokens are estimates, one per two characters. Latency is
  local and varies from run to run.

The mid-run case scripts a provider failure (`timeout`) after one successful tool
call. It must end `provider_error`, keep exactly one tool success, and carry no
claims. It is a scripted stop, not a measured fault in a real provider.

A run's ordered trace is a list of events with a `sequence` number, a `kind`, a fixed
`outcome`, an `elapsed_ms` value, a `tool` name (allowlisted only, and never for an
unknown tool), and any `failure_codes`. The kinds are `provider_turn`, `admission`,
`tool_outcome`, `verification`, `provider_failure`, `budget_stop`, and `terminal`.
Every run ends with one `terminal` event. A trace holds no prompt, transcript, tool
argument or result, telemetry, claim text, replay block, or raw error text.

Default output is unchanged apart from the new `provider_turns` key. The trace is
left out of `AgentRun` JSON. To export it for the offline run, add `--json --trace`:

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_agent.py --json --trace
```

The matrix is deterministic. Two runs produce the same case outcomes, counts, and
event order. Only timing differs.

Measured on the offline run on 2026-11-15 (fake provider, not a live model):
verified completion 24/24, expected failure handling 216/216, attacks rejected
168/168 with 0 accepted, 1056 provider turns, 0 transport requests, 2352 trace
events, scenario agreement 12/12 (a label check, not causal accuracy).

#### Live validation: one successful synthetic investigation (Anthropic, 2026-11-13)

One live investigation with the Anthropic provider completed and verified. It is a
single synthetic case. It is not evidence of general causal accuracy, and it is not
evidence of production readiness. The OpenAI provider has not been run live.

Configuration, as run:

| Setting | Value |
|---------|-------|
| Provider | `anthropic` (Anthropic Messages API), with `--live` |
| Model | `claude-sonnet-5-5` (Claude Sonnet 5.5) |
| Answer format | `compact` |
| Scenario and seed | `cpu_saturation`, seed 601 |
| Maximum requests | 8 |
| Maximum tool calls | 6 |
| Total tokens | 30,000 |
| Maximum output tokens | 1,000 |
| Request timeout | 30 seconds (the default) |
| Deadline | 180 seconds |
| Retries | 0 |
| Pricing file | none |

Command:

```powershell
.\.venv\Scripts\python.exe scripts\run_investigation_agent.py --provider anthropic --live --answer-format compact --scenario cpu_saturation --seed 601 --max-requests 8 --max-tool-calls 6 --max-total-tokens 30000 --max-output-tokens 1000 --request-timeout 30 --max-seconds 180 --max-retries 0
```

Result:

| Measure | Value |
|---------|-------|
| Status | verified |
| Verification | verified |
| Provider requests | 5 |
| Completed tool calls | 4 |
| Verified claims | 13 |
| Read-only checks proposed | 5 |
| Input tokens | 20,952 |
| Output tokens | 747 |
| Total tokens | 21,699 |
| Latency | about 8.16 seconds |
| Provider failures | none |
| Budget stop | none |
| Cost | unknown, no pricing file |

What this run does not show: accuracy across other scenarios or seeds, repeat-run
stability, how often a live model's answers are rejected, retention or
data-handling suitability, or production readiness. The offline evaluation and this
live run measure different things, and they must not be combined into one accuracy
claim.

Earlier attempts are recorded under Compact final answer, below. They stopped
before a verified answer.

Before running it, confirm the preflight passes, both settings are present, the
model identifier is one you intend to pay for, and the data you would send is
acceptable (see Cost and usage, below). The run prints the agent run as JSON,
including its accounting. Record the exit code, status, stop reason, and token counts.
Do not record the key.

#### Tool calling

The model may request six read-only tools: `list_observations`,
`get_observation`, `list_hypotheses`, `get_hypothesis`, `list_uncertainties`, and
`list_suggested_checks`. The trusted registry in `agent_tools.py` defines them, and
each is bound to one report. No tool takes an incident or run ID.

The Anthropic adapter (`backend/app/services/agent_anthropic_provider.py`) works as
follows:

1. Each tool is declared with `input_schema`, generated from the Pydantic model that
   validates its arguments. The final-answer tool, `submit_answer`, uses the
   `AgentOutput` schema, or the `CompactAgentOutput` schema with
   `--answer-format compact` (see Compact final answer, below).
2. A model reply holds `tool_use` blocks, each with an `id`, a `name`, and an
   `input` object. Parallel tool use is disabled, so a reply normally holds one.
   Extra blocks in a reply are queued, and no extra request is sent for them.
3. Each tool request goes back in the conversation as an assistant `tool_use` block
   with its `id`. Its result follows as a user `tool_result` block with the same
   `tool_use_id`. A refused request sets `is_error`, and its message never repeats
   the model's input.
4. The SDK never executes a tool. Only the trusted dispatcher does, and only for
   names on the allowlist. An unknown name or invalid arguments are refused.
5. The final answer is the `submit_answer` input, or plain text. Either way it is
   parsed in the run's answer format and verified in full.

Stop reasons map as follows. `end_turn`, `tool_use`, and `stop_sequence` are accepted.
`refusal` ends the run as `refused`. `max_tokens` and `pause_turn` end it as
`incomplete`. Any other value is `malformed`.

#### Request budgets

Every live provider uses the same budget transport, `agent_transport.py`. Each
attempt, including a retry, passes the budget check before it is sent. Each attempt
counts as a request, and its timeout is capped to the time left before the deadline.

| Limit | Default | Hard maximum |
|-------|---------|--------------|
| Provider requests, including retries | 8 | 20 |
| Tool requests, including refused ones | 6 | 20 |
| Total tokens | 30,000 | 200,000 |
| Output tokens per response | 1,000 | 4,000 |
| Timeout per request | 30 s | 120 s |
| Run deadline | 180 s | 900 s |
| Automatic retries | 0 | 3 |

#### Compact final answer

The full answer, `AgentOutput`, repeats each claim's reference and event list.
`--answer-format compact` asks for `CompactAgentOutput` instead. It names records
by ID and nothing else:

- `observations`: observation IDs (`obs-...`).
- `hypotheses`: hypothesis kinds.
- `uncertainties`: missing-evidence and limitation positions, and the ranking caveat.
- `proposed_checks`: the same check and affected-hypothesis pairs as the full answer.

Trusted code rebuilds the rest. Each observation's event IDs come from the
report, and each claim's text is filled from the canonical record. The rebuilt
answer then goes through the same verifier, with the same rules and failure codes,
as a full answer. An ID the report does not hold is refused, not dropped. So are
another investigation's ID, a hypothesis the report did not generate, and a
malformed answer. A compact answer under the full format, or the reverse, is
malformed.

Measured offline on four synthetic incidents with a fake client, the compact answer
is 11 to 15 times smaller than the full answer without claim text. Both verify to
the same claims. These are character counts and estimates, not provider counts, and
the compact form has not been tested against a live model.

Limitations: missing-evidence entries and limitations have no ID, so the answer
refers to them by their position in the report. Suggested checks have no ID either,
so they are identified by check kind and affected hypothesis. A compact observation
reference always lists every event of its observation, so a model cannot cite a
subset. See architecture.md section 14.10.

Zero retries is the default, so a first smoke test sends each request once. A local
timeout does not cancel the request on the server, and the provider may still bill
it. The input estimate is one token per two characters, which is not a count.

#### Cost and usage

Each run records its model identifier, requests, completed tool calls, input,
output, and total tokens, end-to-end latency, final status, and stop reason. Cost
is estimated only from a pricing file you supply, keyed by model identifier, with
each entry giving its source and verification date:

```json
{
  "currency": "USD",
  "models": {
    "<model identifier>": {
      "input_usd_per_million_tokens": "<rate>",
      "output_usd_per_million_tokens": "<rate>",
      "source": "<where the rate came from>",
      "verified_on": "YYYY-MM-DD"
    }
  }
}
```

For reference, Anthropic's models overview (checked 2026-11-13) lists Claude Sonnet
5.5 at $2 per million input tokens and $10 per million output tokens. Check the
pricing page before relying on it. The overview also says Sonnet 5.5 uses adaptive
thinking by default. This adapter does not set a thinking parameter, so any thinking
tokens would count toward output usage. Whether that happens has not been verified.

Retention, from Anthropic's API and data retention page (checked 2026-11-13): for
organizations without a zero data retention (ZDR) agreement, conversation content
is not retained by default. The exception is models Anthropic calls Covered Models,
which require 30-day retention. The page lists the Fable and Mythos families as
Covered Models. It does not list Sonnet 5.5 in the part of the page I read, but that
should be confirmed with your Anthropic account team before any live use. ZDR is
enabled per organization through sales. It does not extend automatically to other
organizations. The page also describes exceptions, such as legal requirements and
safety flags, which should be read before live use. Retention is governed by the
provider's terms. This code cannot enforce it.

Sources: [Models overview](https://platform.claude.com/docs/en/models/overview),
[Pricing](https://platform.claude.com/docs/en/about-claude/pricing), and
[API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention).

#### Verification and safety boundaries

- The model selects records by ID. The server fills each claim's text from the
  canonical report, so a model cannot add its own sentence by omitting text.
- If a model does supply text, it must match the canonical text exactly. A paraphrase,
  an added sentence, or a causal claim is rejected.
- A single failing claim rejects the whole answer, and no claims are returned.
- `root_cause_assessment` must be `not_assessed`. Any other value is malformed.
- Simulator labels are never sent to a model or its tools. A test checks this.
- Credentials are read from the environment only. They are held as secret values, and
  they never appear in a log, a prompt, a result, or an error.

#### Limitations and unverified live behaviour

- The OpenAI adapter has not been run against its live API. The Anthropic adapter has
  one recorded single-case live run (see Live smoke test). No general live validation
  is claimed.
- `claude-sonnet-5-5` is confirmed as an API identifier on Anthropic's model overview.
  It has been used in the one recorded run only. Other scenarios and seeds have not
  been run live.
- Strict quoting means a real model may often be rejected. A rejected answer is
  reported as rejected and is never trimmed.
- The adaptive-thinking default and its effect on output tokens are unverified.
- Retention and zero-data-retention eligibility for Sonnet 5.5 need confirmation
  from your account team before any real telemetry is sent.
- The SDK preflight passes in this environment. An earlier pytest run of the OpenAI
  client was blocked by Windows Application Control, and that has not been reproduced.
- Cost is an estimate from your pricing file, not an invoice.

The agent cannot access ground-truth labels or evaluation data, and a test checks
that the agent modules never read labels.

## Configuration

Settings are read from environment variables when the app starts. Invalid
values stop startup with a message that names the problem.

| Variable | Values | Default | Effect |
|----------|--------|---------|--------|
| `AEGIS_ENVIRONMENT` | `development`, `production` | `production` | Deployment environment. An unset value fails closed. |
| `AEGIS_SIMULATION_API_ENABLED` | `true`, `false` | `false` | Registers `POST /api/v1/simulations`. |
| `AEGIS_DEMO_API_ENABLED` | `true`, `false` | `false` | Registers `GET /api/v1/demo/baselines`, a read-only listing of stored approved baselines. Development only. |
| `AEGIS_TRUSTED_BASELINES_PATH` | Path to a manifest | Unset | Loads approved baselines for incident detection. Development only. Unset trusts no baseline. |
| `AEGIS_INVESTIGATION_MAX_CITED_EVENTS` | Integer, 1 to 1000 | `100` | Caps the event IDs listed per cited evidence list in an investigation. See [Response bounds](#response-bounds). |

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
