"""The offline-approved snapshot that hosted demo mode loads at API startup.

The snapshot names the fixed synthetic runs the demo shows: two normal-operation
baselines and one CPU saturation fault run. For each run it records the spec, the
run ID, the event count, a SHA-256 digest of the events, and, for a baseline, the
approval that offline validation gave it. It holds no events. The simulator is
deterministic, so startup regenerates each run from its spec and compares the
result with the recorded digest. The approval is checked again with the same
offline validation the manifest uses.

Startup refuses the whole snapshot on any mismatch. The snapshot is only ever a
description of synthetic runs; it cannot add a run that the simulator does not
produce from its spec.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.domain.telemetry import ScenarioId
from app.services.baseline_trust import BaselineApproval, TrustedBaselines
from app.services.event_store import EventStore
from app.services.offline_validation import (
    BaselineRejected,
    validate_synthetic_baseline,
)
from app.services.simulation import (
    SimulationRun,
    SimulationSpec,
    run_id_for,
    run_simulation,
)

SNAPSHOT_VERSION = 1
HOSTED_SPECS: tuple[SimulationSpec, ...] = (
    SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=301, ticks=288),
    SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=302, ticks=288),
    SimulationSpec(scenario=ScenarioId.CPU_SATURATION, seed=311, ticks=288),
)


class HostedDemoError(RuntimeError):
    """The snapshot is missing, malformed, or does not match its regenerated runs.

    The message names the problem and never the file's contents, so a startup
    failure cannot print synthetic data or a path into an API response.
    """


@dataclass(frozen=True)
class HostedDemo:
    """The runs and approvals that hosted demo mode serves once startup succeeds."""

    runs: tuple[SimulationRun, ...]
    trusted: TrustedBaselines


def events_sha256(run: SimulationRun) -> str:
    """Digest a run's events in order, one canonical JSON line per event."""
    digest = hashlib.sha256()
    for event in run.events:
        digest.update(event.model_dump_json().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _entry_for(run: SimulationRun) -> dict[str, Any]:
    """Describe one run in the snapshot, with an approval only for a baseline."""
    approval: dict[str, Any] | None = None
    if run.spec.scenario is ScenarioId.NORMAL_OPERATION:
        checked = validate_synthetic_baseline(run)
        approval = {
            "method": checked.method,
            "checked_event_count": checked.checked_event_count,
        }
    return {
        "run_id": run.run_id,
        "spec": run.spec.model_dump(mode="json"),
        "event_count": len(run.events),
        "anomalous_event_count": run.anomalous_event_count,
        "events_sha256": events_sha256(run),
        "approval": approval,
    }


def build_snapshot_document() -> dict[str, Any]:
    """Simulate the fixed specs and describe them, refusing any faulty baseline.

    Used by ``scripts/build_hosted_snapshot.py``. Output is deterministic, so the
    committed snapshot can be regenerated and compared byte for byte.
    """
    runs = [run_simulation(spec) for spec in HOSTED_SPECS]
    return {
        "version": SNAPSHOT_VERSION,
        "data_origin": "synthetic",
        "runs": [_entry_for(run) for run in runs],
    }


def load_hosted_demo(path: Path) -> HostedDemo:
    """Read the snapshot, regenerate its runs, and verify every recorded field.

    Raises HostedDemoError for a missing, unreadable, or malformed snapshot, for a
    spec outside the fixed set, and for any run whose ID, event count, digest, or
    approval differs from what the simulator and offline validation produce now.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HostedDemoError("hosted demo snapshot cannot be read") from error
    if not isinstance(document, dict) or document.get("version") != SNAPSHOT_VERSION:
        raise HostedDemoError("unsupported hosted demo snapshot version")
    if document.get("data_origin") != "synthetic":
        raise HostedDemoError("hosted demo snapshot is not marked synthetic")
    entries = document.get("runs")
    if not isinstance(entries, list) or len(entries) != len(HOSTED_SPECS):
        raise HostedDemoError("hosted demo snapshot does not list the fixed runs")

    runs: list[SimulationRun] = []
    approvals: list[BaselineApproval] = []
    for expected_spec, entry in zip(HOSTED_SPECS, entries, strict=True):
        run = _regenerate(expected_spec, entry)
        runs.append(run)
        recorded = entry.get("approval")
        if expected_spec.scenario is ScenarioId.NORMAL_OPERATION:
            approvals.append(_verify_approval(run, recorded))
        elif recorded is not None:
            raise HostedDemoError("a fault run carries an approval")
    return HostedDemo(runs=tuple(runs), trusted=TrustedBaselines(tuple(approvals)))


def _regenerate(expected_spec: SimulationSpec, entry: object) -> SimulationRun:
    """Simulate one fixed spec and check its recorded identity and digest."""
    if not isinstance(entry, dict):
        raise HostedDemoError("a hosted demo snapshot entry is not an object")
    try:
        spec = SimulationSpec.model_validate(entry.get("spec"))
    except ValidationError as error:
        raise HostedDemoError(
            "a hosted demo snapshot entry has an invalid spec"
        ) from error
    if spec != expected_spec:
        raise HostedDemoError("a hosted demo snapshot spec is outside the fixed set")
    run = run_simulation(spec)
    if entry.get("run_id") != run_id_for(spec) or run.run_id != run_id_for(spec):
        raise HostedDemoError("a hosted demo run ID does not match its spec")
    if entry.get("event_count") != len(run.events):
        raise HostedDemoError("a hosted demo event count does not match the simulator")
    if entry.get("anomalous_event_count") != run.anomalous_event_count:
        raise HostedDemoError(
            "a hosted demo anomaly count does not match the simulator"
        )
    if entry.get("events_sha256") != events_sha256(run):
        raise HostedDemoError("a hosted demo event digest does not match the simulator")
    return run


def _verify_approval(run: SimulationRun, recorded: object) -> BaselineApproval:
    """Re-run offline validation on a baseline and compare the recorded approval."""
    try:
        approval = validate_synthetic_baseline(run)
    except BaselineRejected as error:
        raise HostedDemoError(
            "a hosted demo baseline fails offline validation"
        ) from error
    if not isinstance(recorded, dict) or recorded != {
        "method": approval.method,
        "checked_event_count": approval.checked_event_count,
    }:
        raise HostedDemoError(
            "a hosted demo approval does not match offline validation"
        )
    return approval


def store_hosted_runs(store: EventStore, demo: HostedDemo) -> None:
    """Put every verified run into the store, and check the approvals against it.

    Each approved run must be present with the same spec and event count as its
    approval. A missing or differing run means the store and approvals disagree,
    and startup refuses rather than serve a partial demo.
    """
    for run in demo.runs:
        store.add_run(run)
    stored = {run.run_id: run for run in store.runs()}
    for approval in demo.trusted.approvals:
        run = stored.get(approval.run_id)
        if run is None or run.spec != approval.spec:
            raise HostedDemoError("an approved baseline is not stored with its spec")
        if len(run.events) != approval.checked_event_count:
            raise HostedDemoError("an approved baseline differs from its approval")
