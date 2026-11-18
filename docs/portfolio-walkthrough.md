# Portfolio walkthrough

This page explains what AEGIS AI shows, how the Docker demo is put together, and
how to describe its two investigation paths. All data is synthetic. The steps for
running the demo are in the [demo guide](demo.md), and this page links to them
rather than repeating them.

## Architecture at a glance

```
 Browser (127.0.0.1:8080)
        |
        v
+--------------------------------------+
| web: nginx serving the React build   |
| proxies three GET routes only        |
+------------------+-------------------+
                   | /api/v1/... (GET)
                   v
+--------------------------------------+        +------------------------------+
| api: FastAPI, in-memory store        |<-------| seed: load_demo_seeds.py     |
|  - synthetic simulation engine       |  POST  | creates the demo runs and    |
|  - event store                       |        | reloads them after restarts  |
|  - detection and correlation         |        +------------------------------+
|  - deterministic investigation       |
+------------------+-------------------+
                   ^
                   | approved baselines (manifest on a shared volume)
+------------------+-------------------+
| approve-baselines: runs once per up  |
+--------------------------------------+

Separate workflow, run from the command line (not part of the dashboard):

 scripts/run_investigation_agent.py --provider anthropic --live
        |
        |  builds one simulated incident and its investigation report
        v
 Claude (Anthropic Messages API) --requests--> closed, read-only tool registry
        |                                       (reads the same report)
        v
 Python verifier checks every claim against the report
        |
        v
 Accepted report, or refusal
```

The dashboard path never calls a model. The agent path is a local experiment that
reuses the same synthetic data and the same report.

## What the dashboard shows

Once the demo is running, the dashboard at <http://127.0.0.1:8080> has two panels.

- **Incidents.** The list shows each incident's ID, when it was first detected, how
  many alerts and evidence items it groups, and the services it affects. The demo
  has one incident. Its alerts come from the API gateway and the orders service.
- **Investigation.** The selected incident's report shows:
  - the analysis status and the analysis window;
  - **Root cause**, which reads *Not assessed*;
  - **Ranked hypotheses**. CPU resource saturation ranks first and carries the
    **Unverified** badge. The ranking orders candidate explanations, and it does not
    name a cause;
  - **Verified observations**, which are facts computed from the stored events;
  - **Contradictory evidence**, **Missing evidence**, and **Suggested checks**;
  - **Limitations**.

The point to make when showing it: the dashboard can rank explanations and say
what evidence is missing, but it cannot confirm a cause.

## The Docker demo

`compose.yaml` runs four services on one machine. The only host port is the
dashboard, bound to `127.0.0.1:8080`.

| Service | What it does |
|---------|--------------|
| `approve-baselines` | Runs once per `up`. It writes the manifest for the two approved normal baselines (seeds 301 and 302) to a shared volume. |
| `api` | FastAPI with the development-only routes enabled. Its store is in memory. It has no host port. |
| `seed` | Creates the demo runs in the API, and creates them again when a restart empties the store. |
| `web` | nginx serving the built dashboard. It proxies only the three GET routes the dashboard uses. It reports healthy only when the baseline listing shows two baselines. |

Start, open, and stop commands are in [Docker Compose demo](demo.md#docker-compose-demo).
That section also lists the checks that the route boundary holds: unlisted paths and
other methods return `404` or `405`.

This is a local demo, not a deployment. The simulation endpoint has no
authentication, so the development routes stay inside the Compose network and on
loopback. A hosted variant, `compose.hosted.yaml`, serves a committed snapshot in
production mode with no seed service. It is a local check of that configuration.
Nothing is published. See [Hosted synthetic demo mode](demo.md#hosted-synthetic-demo-mode).

## Two investigations, and how to explain the difference

AEGIS AI has two investigation paths. They answer different questions, so keep
them apart when you describe the project.

| | Dashboard investigation | Claude-powered agent |
|---|---|---|
| Runs where | In the API, read by the dashboard through `GET /api/v1/incidents/{id}/investigation` | A local command-line script. It has no HTTP endpoint. |
| How it decides | Fixed Python rules over stored events | The model proposes read-only tool calls. A server-side verifier checks each claim before the answer is accepted. |
| Uses a model | No | Yes, with `--provider anthropic --live`. The OpenAI adapter exists but has not been run live. |
| Output | Hypotheses always marked Unverified; root cause always Not assessed | Verified, or refused |
| Evidence of quality | Offline evaluation of every investigation, `scripts/evaluate_investigations.py` | Offline evaluation with a scripted fake provider, plus one live run |

A suggested way to put it:

> The dashboard's investigation is deterministic. It calls no model, so the same
> synthetic run gives the same report every time. The Claude agent is a separate
> experiment on top of that report. The model can request approved read-only
> tools, and Python checks each claim against the evidence before it is accepted.

Two points to make clearly:

- **"Verified" is narrow.** For the agent, it means each accepted claim matched
  the stored evidence. It does not mean the root cause was proven. The dashboard
  shows the same limit as *Not assessed*.
- **The live run is one case.** The agent was run live once, with the Anthropic
  provider, on one synthetic incident: `cpu_saturation`, seed 601. The run
  verified, with 5 provider requests, 4 tool calls, and 13 verified claims. It
  says nothing about accuracy across other scenarios, seeds, or repeat runs. The
  full record is in [Live validation](architecture.md#149-llm-investigation-agent-fake-and-optional-live-provider),
  under *Live validation: one successful synthetic investigation*.

The offline evaluation measures a different thing. With the scripted fake provider,
the Day 8 baseline verified all 24 completion cases, handled all 216 expected
failure cases, and rejected all 168 deliberately faulty answers. Those numbers
test the verifier, not a live model. Do not combine them with the live run into one
accuracy claim.

## Run the checks

- Dashboard and API steps: [Dashboard](demo.md#dashboard) and
  [Demo workflow](demo.md#demo-workflow).
- Offline agent evaluation, with a walkthrough of the lifecycle trace:
  [Offline agent evaluation walkthrough](offline-evaluation-demo.md).
- Agent setup, providers, and the offline procedures:
  [Investigation agent](architecture.md#investigation-agent).

## Limits to state

- All data is synthetic, and the demo has one incident.
- The store is in memory. A restart clears it, and the seed service reloads it.
- The API and the dashboard have no authentication, and the simulation endpoint
  changes server state. Both are for local use only.
- There is no approval workflow and no remediation. The approval screens are not
  built.
- Nothing is deployed, and there is no public URL.
- The agent has one recorded live run. Its design, not its accuracy, is what this
  project establishes.
