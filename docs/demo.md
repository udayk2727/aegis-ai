# AEGIS AI demo and Windows setup

Commands for running AEGIS AI locally on Windows PowerShell: the initial setup,
the offline verification pipeline, the read-only demo workflow, and the React
dashboard. All data is synthetic. Every section here is for development only.
For the project overview, see the [README](../README.md). For a guided tour of
the dashboard and the two investigation paths, see the
[portfolio walkthrough](portfolio-walkthrough.md).

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
optional file; see [Running visualizations](architecture.md#running-visualizations).

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

## Verifying the pipeline (Windows PowerShell)

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
evaluation of every investigation (see [Investigation](architecture.md#investigation-usage-and-reference)):

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
[Investigation agent](architecture.md#investigation-agent).

## Dashboard

The local React dashboard in `frontend/` reads the demo endpoints above. It only
reads. The data is prepared by the same steps as the demo workflow, against the
same running API.

**Which port each workflow uses.** The two workflows use different ports, so
check which one you are following:

| Workflow | Port | Where it is set |
|---|---|---|
| Demo workflow (curl-style commands and Swagger UI) | 8000 | Commands in the Demo workflow section |
| Dashboard (this section) | 8001 | Vite proxy default, `AEGIS_API_TARGET` |

If you run the API on 8000 and want the dashboard, set
`$env:AEGIS_API_TARGET = "http://127.0.0.1:8000"` before `npm run dev`. Use one
port for the whole session. The dashboard only sees data in the API it proxies to.

**Start the API and prepare the data.** Run these in the first PowerShell window.
The API stays in the foreground.

```powershell
cd C:\Projects\aegis-ai
.\.venv\Scripts\python.exe scripts\approve_baselines.py --seeds 301 302 --ticks 288
$env:AEGIS_ENVIRONMENT = "development"
$env:AEGIS_SIMULATION_API_ENABLED = "true"
$env:AEGIS_DEMO_API_ENABLED = "true"
$env:AEGIS_TRUSTED_BASELINES_PATH = "local\trusted_baselines.json"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001
```

In a second PowerShell window, create the two approved normal baselines and the
CPU saturation fault run in the running API. Each run has 288 ticks:

```powershell
$base = "http://127.0.0.1:8001/api/v1"
$json = "application/json"
Invoke-RestMethod -Method Post -Uri "$base/simulations" -ContentType $json -Body '{"scenario":"normal_operation","seed":301,"ticks":288}' | Out-Null
Invoke-RestMethod -Method Post -Uri "$base/simulations" -ContentType $json -Body '{"scenario":"normal_operation","seed":302,"ticks":288}' | Out-Null
Invoke-RestMethod -Method Post -Uri "$base/simulations" -ContentType $json -Body '{"scenario":"cpu_saturation","seed":311,"ticks":288}' | Out-Null
```

**Start the dashboard.** In a third PowerShell window, install the locked
dependencies once, then start the Vite development server:

```powershell
cd C:\Projects\aegis-ai\frontend
npm ci
npm run dev
```

Open <http://127.0.0.1:5173>. To point the proxy at another API address, set
`$env:AEGIS_API_TARGET` before `npm run dev`. The default is
`http://127.0.0.1:8001`.

**Checks.** From `frontend`, run `npm test`, `npm run lint`, and `npm run build`.
The tests use mocked fetch and do not need the API. The API must be running for
the browser checks above.

**Limitations.** The store is in memory, so restarting the API clears the runs,
and the three simulation steps must be repeated. The dashboard is development
only and is not authenticated. It shows synthetic data. Hypotheses are always
`unverified`, and root cause is always `not_assessed`.


## Docker Compose demo

This runs the API, the built dashboard, and a seed service in containers on this
machine. It shows the same synthetic data as the sections above, and it needs only
Docker with Compose v2. It is a local demo, not a deployment. The stack enables the
development-only routes, and the simulation endpoint has no authentication.

Run these commands from the repository root in PowerShell.

**Start.** Builds the images, approves the two normal baselines into a Docker
volume, and starts the API, the dashboard, and the seed service. The seed service
waits for the API's health probe and creates the demo runs, so no separate seed
command is needed:

```powershell
cd C:\Projects\aegis-ai
docker compose up --build -d
docker compose logs seed
```

The seed log ends with `reloaded the demo runs after the listing was short`. The
seed service stays running. It is the only place that creates the demo runs.

**Open.** The dashboard is published on the loopback address only:

```powershell
Start-Process http://127.0.0.1:8080
```

The incident list shows one incident. Open it. The first hypothesis, CPU resource
saturation, is ranked first and marked **Unverified**, and the root cause reads
**Not assessed**. To check the data without a browser, run this through the same
port:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/v1/demo/baselines   # total: 2
```

**Restart.** To restart only the API, run `docker compose restart api`. The API
keeps its store in memory, so the restart empties it. The seed service checks the
listing every five seconds and loads the runs again when the listing is short. In
testing, the listing returned to two baselines about six seconds after the restart,
and the incident was back with it. Two restarts behaved the same way.

While the API is down, the proxy answers `502`. The dashboard then shows
"The API is temporarily unavailable" with a Retry button. A page that is already
open keeps the data it loaded, so press Retry or reload the page after a restart.

**Stop.** Stops and removes the containers and network, and keeps the volume:

```powershell
docker compose down
```

To remove the `baselines` volume as well, run `docker compose down -v`. The next
`up` regenerates the manifest.

**Logs and shell.** `docker compose logs api`, `docker compose logs seed`, and
`docker compose exec api sh`.

### Health

`GET /healthz` on the dashboard port answers `ok` from nginx alone. It is a
liveness probe only. The `web` container's Compose health check is a readiness
check: it reads `GET /api/v1/demo/baselines` through the proxy and passes only when
`total` is `2`. So `web` reports unhealthy while the API is down or its store is
empty. In testing, with the seed stopped and the API restarted, `web` turned
unhealthy about 30 seconds later and recovered within about 10 seconds of the seed
restarting.

### The public boundary

The dashboard proxy forwards only the three GET routes the dashboard calls:

| Route | Used for |
|---|---|
| `GET /api/v1/demo/baselines` | Finding the approved baselines |
| `GET /api/v1/incidents` | The incident list |
| `GET /api/v1/incidents/{incident_id}/investigation` | One investigation |

Every other path under `/api/` returns `404`. A non-GET method on the three routes
returns `405`. The paths `/docs`, `/redoc`, `/openapi.json`, and `/health` return
`404`, so the backend documentation is not served. Other unknown paths still return
the dashboard page, as a single-page app does. The API has no host port. The `api`
and `seed` services are reachable only on the Compose network.

To check the boundary, run these from PowerShell. Each prints one status code:

```powershell
curl.exe -s -o NUL -w "%{http_code}\n" -X POST http://127.0.0.1:8080/api/v1/simulations   # 404
curl.exe -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8080/api/v1/simulations            # 404
curl.exe -s -o NUL -w "%{http_code}\n" -X POST http://127.0.0.1:8080/api/v1/demo/baselines # 405
curl.exe -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8080/api/v1/events                # 404
curl.exe -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8080/api/v1/demo/baselines       # 200
```

**Notes.**

- `docker compose up` runs the approval step on every start, and it writes the
  same manifest each time.
- The development settings are set in `compose.yaml` only. The backend image sets
  none of them, and it installs only `requirements.txt`.
- The image build context excludes `local/`, host virtual environments,
  `node_modules`, build output, `.env` files, and credential files. See
  `.dockerignore`.
- `nginx.conf` is copied into the `web` image at build time. After you edit it, run
  `docker compose up --build -d web`. A restart alone does not load the change.

### What a host must provide (planned, not deployed)

Nothing here is deployed. These are the requirements a host must meet before the
demo can be shared over HTTPS. None is set up yet.

- **HTTPS.** The dashboard serves plain HTTP on `127.0.0.1:8080`. A host needs a
  TLS-terminating proxy or load balancer in front of the `web` container's port
  8080. Only that port should be forwarded, and the `api` and `seed` services must
  stay unpublished.
- **Health checks.** Point the host's probe at `GET /healthz`. For data readiness,
  check `GET /api/v1/demo/baselines` for `total: 2`. A `502` from the proxy means
  the API is down or restarting.
- **Container networking.** Compose's default network connects the services by
  name. The `web` container resolves `api` through Docker's embedded DNS
  (`127.0.0.11`), so the host must run Docker Engine with Compose v2 and keep that
  resolver. The `seed` container reaches `http://api:8000` on the same network.
- **Restart policy.** `api`, `web`, and `seed` use `restart: unless-stopped`.
  `approve-baselines` runs once per `up` and is not restarted.
- **Owner decision: hosting.** Hosted demo mode (below) resolves the code-level guard
  conflict for the synthetic demo. Choosing a host, setting up TLS and networking, and
  publishing the demo remain owner decisions. None of them is done.
- **Still missing.** Authentication, rate limiting, and persistent storage. The
  dashboard and API are unauthenticated, and the store is in memory.

## Hosted synthetic demo mode

This mode runs the API as `AEGIS_ENVIRONMENT=production` with an offline-approved
snapshot, for the synthetic demo only. It is off by default, and it is a single
scoped exception to the production guards. The policy is in
[architecture §14.13](architecture.md#1413-day-12-hosted-synthetic-demo-mode-scoped-policy-decision).
It is not a deployment: nothing is published, and no host is set up.

**What it serves.** Two approved normal-operation baselines (seeds 301 and 302) and
the CPU saturation fault run (seed 311), each with 288 ticks. The snapshot is
`demo/hosted_snapshot.json`, and it is loaded and verified at API startup. There is
no seed step, no approval step, and no simulation endpoint.

**Regenerate and check the snapshot (offline).** From the repository root:

```powershell
cd C:\Projects\aegis-ai
.\.venv\Scripts\python.exe scripts\build_hosted_snapshot.py
git diff --exit-code -- demo\hosted_snapshot.json
```

The second command shows no difference when the committed snapshot is current.
Running the script again reproduces it byte for byte.

**Start (local check of the hosted configuration).** The local stack and the hosted
stack both use port 8080, so stop the local one first. `docker compose down` keeps
its volume:

```powershell
cd C:\Projects\aegis-ai
docker compose down
docker compose -f compose.hosted.yaml up --build -d
```

No seed command is needed. Check that `web` is healthy, which means both baselines
are loaded:

```powershell
docker inspect -f "{{.State.Health.Status}}" aegis-hosted-demo-web-1
Invoke-RestMethod http://127.0.0.1:8080/api/v1/demo/baselines   # total: 2
```

Open <http://127.0.0.1:8080>. The CPU incident is listed, and its rank-one hypothesis
is marked **Unverified** with root cause **Not assessed**.

**Restart the API.** `docker compose -f compose.hosted.yaml restart api` reloads the
snapshot at startup. The listing returns within a few seconds, with no manual step.

**Stop.**

```powershell
docker compose -f compose.hosted.yaml down
```

**Startup refusal.** The API refuses to start, and the container exits with an error,
when the snapshot is missing, unreadable, or does not verify. It also refuses hosted
mode outside production, and the development flags under production. Each refusal
names the setting, and none prints the snapshot. To confirm that the production
refusal holds for the image, run it once with `AEGIS_ENVIRONMENT=production` and
`AEGIS_SIMULATION_API_ENABLED=true`; it exits with code 1.

**Verify the boundary.** These match the three-route GET allowlist:

```powershell
curl.exe -s -o NUL -w "%{http_code}\n" -X POST http://127.0.0.1:8080/api/v1/simulations   # 404
curl.exe -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8080/docs                          # 404
curl.exe -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8080/api/v1/events                 # 404
curl.exe -s -o NUL -w "%{http_code}\n" -X POST http://127.0.0.1:8080/api/v1/demo/baselines # 405
curl.exe -s -o NUL -w "%{http_code}\n" http://127.0.0.1:8080/api/v1/demo/baselines        # 200
```

The API has no host port in either stack. The two Compose files differ only in the
mode they set: `compose.yaml` is the local development demo, and
`compose.hosted.yaml` is the hosted synthetic demo with no seed service.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and every pull request:

- **Backend** (Python 3.13): installs `requirements.txt`, `requirements-analytics.txt`,
  and `requirements-llm.txt`, then runs `python -m pytest` and
  `python scripts/evaluate_agent.py`. The evaluator's default run is offline. It
  exits `1` if an attack is accepted and `2` if any case misses its expectation,
  so either one fails the job.
- **Frontend** (Node 22): `npm ci`, `npm test`, `npm run lint`, and `npm run build`.

No job reads a provider key or makes a paid request.

## Configuration

Settings are read from environment variables when the app starts. Invalid
values stop startup with a message that names the problem.

| Variable | Values | Default | Effect |
|----------|--------|---------|--------|
| `AEGIS_ENVIRONMENT` | `development`, `production` | `production` | Deployment environment. An unset value fails closed. |
| `AEGIS_SIMULATION_API_ENABLED` | `true`, `false` | `false` | Registers `POST /api/v1/simulations`. |
| `AEGIS_DEMO_API_ENABLED` | `true`, `false` | `false` | Registers `GET /api/v1/demo/baselines`, a read-only listing of stored approved baselines. Development only. |
| `AEGIS_TRUSTED_BASELINES_PATH` | Path to a manifest | Unset | Loads approved baselines for incident detection. Development only. Unset trusts no baseline. |
| `AEGIS_INVESTIGATION_MAX_CITED_EVENTS` | Integer, 1 to 1000 | `100` | Caps the event IDs listed per cited evidence list in an investigation. See [Response bounds](architecture.md#response-bounds). |

**Production guard.** The simulation endpoint has no authentication, so the
settings refuse to enable it in production. Setting
`AEGIS_ENVIRONMENT=production` together with `AEGIS_SIMULATION_API_ENABLED=true`
stops the app from starting. When the endpoint is disabled, its route does not
exist, so requests to it return `404`. This guard cannot be lifted by a
configuration change; it needs the authentication design in
[architecture §5](architecture.md#5-security-architecture-planned).

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
[CLAUDE.md](../CLAUDE.md).

## Contributing

1. Read [CLAUDE.md](../CLAUDE.md) before making changes.
2. Keep each change small and covered by tests.
3. Run `python -m pytest` before opening a pull request.
4. Update documentation when behavior or scope changes.