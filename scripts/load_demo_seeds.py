"""Load the synthetic demo runs into a running API and check the baseline listing.

Run it after the API is up. It waits for ``GET /health``, creates the two approved
normal baselines and the CPU saturation fault run, and confirms that the demo
listing shows both baselines. Re-running it is safe: the API answers a run it
already holds with 200 and stores nothing new. A restart clears the in-memory
store, so run this again after one.

    python scripts/load_demo_seeds.py --base-url http://127.0.0.1:8000

Uses only the standard library. Exit code 0 means the runs are loaded and the
listing shows the two baselines. Exit code 1 means the API did not become ready,
a request failed, or the listing was not what the demo expects.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

BASELINE_SPECS: tuple[dict[str, Any], ...] = (
    {"scenario": "normal_operation", "seed": 301, "ticks": 288},
    {"scenario": "normal_operation", "seed": 302, "ticks": 288},
)
FAULT_SPEC: dict[str, Any] = {"scenario": "cpu_saturation", "seed": 311, "ticks": 288}
EXPECTED_BASELINES = len(BASELINE_SPECS)
READY_ATTEMPTS = 60
READY_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 10.0
ACCEPTED_CREATE_STATUSES = (200, 201)

Request = Callable[[str, str, dict[str, Any] | None], tuple[int, Any]]


class SeedError(RuntimeError):
    """A seed step failed. The message names the step and never a response body."""


def _http(method: str, url: str, body: dict[str, Any] | None) -> tuple[int, Any]:
    """Send one JSON request and return its status and parsed body.

    A connection failure returns status 0, so the readiness loop can retry it.
    """
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {} if body is None else {"Content-Type": "application/json"}
    http_request = urllib.request.Request(
        url, data=data, method=method, headers=headers
    )
    try:
        with urllib.request.urlopen(
            http_request, timeout=REQUEST_TIMEOUT_SECONDS
        ) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, None
    except OSError:
        return 0, None


def wait_until_ready(
    base_url: str,
    request: Request,
    sleep: Callable[[float], None],
    attempts: int = READY_ATTEMPTS,
) -> bool:
    """Poll the health probe until it answers 200, or give up after ``attempts``."""
    for _ in range(attempts):
        status, _body = request("GET", f"{base_url}/health", None)
        if status == 200:
            return True
        sleep(READY_INTERVAL_SECONDS)
    return False


def load_runs(base_url: str, request: Request) -> list[str]:
    """Create the baseline and fault runs in order, and return their run IDs."""
    run_ids: list[str] = []
    for spec in (*BASELINE_SPECS, FAULT_SPEC):
        status, body = request("POST", f"{base_url}/api/v1/simulations", spec)
        if status not in ACCEPTED_CREATE_STATUSES or not isinstance(body, dict):
            raise SeedError(
                f"simulation {spec['scenario']} seed {spec['seed']} "
                f"failed with status {status}"
            )
        run_ids.append(str(body["run_id"]))
    return run_ids


def count_baselines(base_url: str, request: Request) -> int:
    """Return how many approved baselines the demo listing shows."""
    status, body = request("GET", f"{base_url}/api/v1/demo/baselines", None)
    if status != 200 or not isinstance(body, dict):
        raise SeedError(f"the demo listing returned status {status}")
    return int(body["total"])


def main(argv: Sequence[str] | None = None) -> int:
    """Wait for the API, load the demo runs, and check the listing."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")

    if not wait_until_ready(base_url, _http, time.sleep):
        print("the API did not become ready", file=sys.stderr)
        return 1
    try:
        run_ids = load_runs(base_url, _http)
        baselines = count_baselines(base_url, _http)
    except SeedError as error:
        print(str(error), file=sys.stderr)
        return 1
    if baselines != EXPECTED_BASELINES:
        print(
            f"the demo listing shows {baselines} baselines, "
            f"expected {EXPECTED_BASELINES}",
            file=sys.stderr,
        )
        return 1
    print(f"loaded {len(run_ids)} runs; the demo listing shows {baselines} baselines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
