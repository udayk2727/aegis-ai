"""Approve synthetic baseline runs offline and write a trusted-baseline manifest.

This is the only way a baseline becomes trusted for incident detection. Each
candidate is a normal-operation simulation spec. It is simulated, checked
against its ground-truth labels, and recorded with its run ID and check method.
Any candidate that contains an injected fault stops the script with no manifest
written, so a faulty baseline cannot reach the file.

    python scripts/approve_baselines.py --seeds 101 102 103 104 --ticks 288

The default output is local/trusted_baselines.json. The local/ directory is
git-ignored. Point the application at the manifest with
AEGIS_TRUSTED_BASELINES_PATH, in development only. The application re-checks
every entry when it loads the manifest.

Approval by synthetic labels shows that a run contains no injected fault. It
does not show that the run is representative of production traffic. The
simulator's normal behaviour is stationary and is not a model of real systems.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.domain.telemetry import ScenarioId  # noqa: E402
from app.services.offline_validation import (  # noqa: E402
    BaselineRejected,
    approve_baseline_specs,
)
from app.services.simulation import SimulationSpec  # noqa: E402
from app.services.trusted_baselines import manifest_text  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "local" / "trusted_baselines.json"
DEFAULT_SEEDS = (101, 102, 103, 104)
DEFAULT_TICKS = 288


def main(argv: Sequence[str] | None = None) -> int:
    """Approve the candidates and write the manifest, or return non-zero on refusal."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--ticks", type=int, default=DEFAULT_TICKS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    specs = [
        SimulationSpec(
            scenario=ScenarioId.NORMAL_OPERATION,
            seed=seed,
            ticks=args.ticks,
        )
        for seed in args.seeds
    ]
    try:
        approvals = approve_baseline_specs(specs)
    except (BaselineRejected, ValueError) as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(manifest_text(approvals), encoding="utf-8")
    for approval in approvals:
        print(f"approved {approval.run_id} ({approval.checked_event_count} events)")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
