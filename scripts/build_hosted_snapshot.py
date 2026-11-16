"""Write the offline-approved snapshot that hosted demo mode loads at startup.

Run it from the repository root. It simulates the fixed demo runs, checks each
baseline against its labels, and writes the snapshot. The output is deterministic,
so running it again reproduces the committed file byte for byte.

    python scripts/build_hosted_snapshot.py

The default output is demo/hosted_snapshot.json. Any baseline that contains an
injected fault stops the script before anything is written.
"""

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services.hosted_demo import build_snapshot_document  # noqa: E402
from app.services.offline_validation import BaselineRejected  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "demo" / "hosted_snapshot.json"


def main(argv: Sequence[str] | None = None) -> int:
    """Build the snapshot and write it, or return non-zero on refusal."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    try:
        document = build_snapshot_document()
    except BaselineRejected as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Write LF explicitly: write_text would emit CRLF on Windows, which the
    # repository's .gitattributes rejects and which breaks the byte-for-byte check.
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    for entry in document["runs"]:
        scenario = entry["spec"]["scenario"]
        print(f"{entry['run_id']} {scenario} {entry['event_count']} events")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
