"""Loading and writing the local manifest of approved baseline runs (development only).

The incident pipeline accepts only a ``TrustedBaselines`` value. This module
builds one from a manifest on disk. It never builds one from a request.

Loading re-checks every entry. Each spec is simulated again and must pass the
offline synthetic validation, and each recorded run ID must match its spec. A
manifest that was edited, copied from another run, or lists a faulty run is
refused as a whole, so nothing becomes trusted because a file or caller says so.

The manifest is a local file for development. Settings refuse it in production,
and no endpoint writes to it. Approval happens offline, through
``scripts/approve_baselines.py``.
"""

import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import ValidationError

from app.services.baseline_trust import BaselineApproval, TrustedBaselines
from app.services.offline_validation import BaselineRejected, approve_baseline_specs
from app.services.simulation import SimulationSpec, run_id_for

MANIFEST_VERSION = 1


class TrustedBaselineError(ValueError):
    """A manifest cannot be read, has the wrong shape, or lists an invalid run."""


def load_trusted_baselines(path: Path) -> TrustedBaselines:
    """Read a manifest and re-verify every entry before trusting any of them.

    Raises TrustedBaselineError for any unreadable, malformed, mismatched, or
    faulty entry. The whole manifest is refused, so a partly valid file does not
    quietly leave some entries out.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TrustedBaselineError("trusted baseline manifest cannot be read") from error
    if not isinstance(document, dict) or document.get("version") != MANIFEST_VERSION:
        raise TrustedBaselineError("unsupported trusted baseline manifest version")
    entries = document.get("baselines")
    if not isinstance(entries, list):
        raise TrustedBaselineError("trusted baseline manifest has no baselines list")

    specs: list[SimulationSpec] = []
    for entry in entries:
        spec, recorded_run_id = _parse_entry(entry)
        if recorded_run_id != run_id_for(spec):
            raise TrustedBaselineError("a manifest run ID does not match its spec")
        specs.append(spec)
    try:
        return TrustedBaselines(tuple(approve_baseline_specs(specs)))
    except BaselineRejected as error:
        raise TrustedBaselineError(str(error)) from error


def _parse_entry(entry: object) -> tuple[SimulationSpec, str]:
    """Return the spec and recorded run ID of one manifest entry, or refuse it."""
    if not isinstance(entry, dict):
        raise TrustedBaselineError("a manifest entry is not an object")
    run_id = entry.get("run_id")
    if not isinstance(run_id, str):
        raise TrustedBaselineError("a manifest entry has no run_id")
    try:
        spec = SimulationSpec.model_validate(entry.get("spec"))
    except ValidationError as error:
        raise TrustedBaselineError("a manifest entry has an invalid spec") from error
    return spec, run_id


def manifest_text(approvals: Iterable[BaselineApproval]) -> str:
    """Serialize approvals to the manifest format, with provenance for each run."""
    document = {
        "version": MANIFEST_VERSION,
        "baselines": [
            {
                "run_id": approval.run_id,
                "method": approval.method,
                "checked_event_count": approval.checked_event_count,
                "spec": approval.spec.model_dump(mode="json"),
            }
            for approval in approvals
        ],
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"
