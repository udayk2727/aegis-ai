"""Check whether this environment can use the pinned LLM SDKs, with no request.

This is a diagnostic only. For each SDK it checks the Python version, that the
installed package matches the version pinned in ``requirements-llm.txt``, that the
SDK imports, and that a client can be built with a dummy key and its resource
loaded. It sends no request, reads no real credential, and changes no security
setting. A failure is reported with the exception's class name only.

    python scripts/check_llm_environment.py

Exit code 0 means every check for every SDK passed. Exit code 1 means at least one
did not. A passing check here does not mean a live adapter works. Only a successful
live call proves that, and none has been made.
"""

import importlib.metadata
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "requirements-llm.txt"
DUMMY_KEY = "preflight-dummy-key-not-a-credential"
REQUIRED_PYTHON = (3, 13)
PACKAGES = ("openai", "anthropic")


@dataclass(frozen=True)
class Check:
    """One named check and its outcome. The detail never holds a secret."""

    name: str
    passed: bool
    detail: str


def pinned_version(requirements: Path, package: str) -> str:
    """Return the exact version of ``package`` pinned in the requirements file."""
    pattern = re.compile(rf"^{re.escape(package)}==(\d+\.\d+\.\d+)\s*$", re.MULTILINE)
    match = pattern.search(requirements.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"no exact {package} pin was found")
    return match.group(1)


def _build_client(package: str) -> Any:
    """Build an SDK client with a dummy key and no retries. Makes no request."""
    if package == "openai":
        from openai import OpenAI

        return OpenAI(api_key=DUMMY_KEY, max_retries=0, timeout=5.0)
    from anthropic import Anthropic

    return Anthropic(api_key=DUMMY_KEY, max_retries=0, timeout=5.0)


RESOURCE = {"openai": "responses", "anthropic": "messages"}


def run_preflight(
    package: str,
    requirements: Path = REQUIREMENTS,
    *,
    build_client: Callable[[], Any] | None = None,
    installed_version: Callable[[str], str] = importlib.metadata.version,
) -> list[Check]:
    """Run every check for one SDK. Later checks run even when an earlier one fails.

    ``build_client`` defaults to the real SDK. Tests pass a stand-in.
    """
    make_client = build_client or (lambda: _build_client(package))
    checks: list[Check] = []
    version_info = sys.version_info[:2]
    checks.append(
        Check(
            name=f"{package} python version",
            passed=version_info == REQUIRED_PYTHON,
            detail=f"{sys.version_info.major}.{sys.version_info.minor}",
        )
    )

    try:
        pinned = pinned_version(requirements, package)
    except (OSError, ValueError) as error:
        checks.append(Check(f"{package} pinned version", False, type(error).__name__))
        return checks
    checks.append(Check(f"{package} pinned version", True, pinned))

    try:
        installed = installed_version(package)
    except importlib.metadata.PackageNotFoundError as error:
        detail = type(error).__name__
        checks.append(Check(f"{package} installed version", False, detail))
        return checks
    checks.append(
        Check(
            f"{package} installed version",
            installed == pinned,
            f"installed {installed}",
        )
    )

    try:
        client = make_client()
    except Exception as error:
        detail = type(error).__name__
        checks.append(Check(f"{package} client construction", False, detail))
        return checks
    checks.append(Check(f"{package} client construction", True, "no request was sent"))

    resource = RESOURCE.get(package, "responses")
    try:
        getattr(client, resource)
    except Exception as error:
        detail = type(error).__name__
        checks.append(Check(f"{package} {resource} resource", False, detail))
        return checks
    checks.append(
        Check(f"{package} {resource} resource", True, "loaded without a request")
    )
    return checks


def main() -> int:
    """Print each check for each SDK, and return 0 only if all of them passed."""
    checks: list[Check] = []
    for package in PACKAGES:
        checks.extend(run_preflight(package))
    for check in checks:
        status = "PASS" if check.passed else "FAIL"
        print(f"{status}  {check.name}: {check.detail}")
    print("no API request was sent; this is not a live test")
    return 0 if all(check.passed for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
