"""Tests for the SDK preflight. Each check uses a stand-in, so no request is made."""

import importlib.metadata
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_llm_environment.py"
REQUIREMENTS = Path(__file__).resolve().parents[2] / "requirements-llm.txt"


def _load() -> ModuleType:
    """Import the preflight script by path, since scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location("check_llm_environment", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PREFLIGHT = _load()


class StandInResource:
    """A stand-in SDK resource that fails the test if it is ever used."""

    def create(self, **kwargs: Any) -> None:
        """Fail: the preflight must never send a request."""
        raise AssertionError("the preflight sent a request")


class StandInClient:
    """A stand-in SDK client. It has no network and both resource names."""

    def __init__(self) -> None:
        self.responses = StandInResource()
        self.messages = StandInResource()


def _by_name(checks: list[Any]) -> dict[str, Any]:
    """Index the checks by name."""
    return {check.name: check for check in checks}


@pytest.mark.parametrize("package", ["openai", "anthropic"])
def test_each_pin_is_read_as_an_exact_version(package: str) -> None:
    expected = {"openai": "2.54.0", "anthropic": "0.109.0"}[package]

    assert PREFLIGHT.pinned_version(REQUIREMENTS, package) == expected


def test_a_requirements_file_without_an_exact_pin_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "requirements.txt"
    path.write_text("openai>=2\n", encoding="utf-8")

    with pytest.raises(ValueError):
        PREFLIGHT.pinned_version(path, "openai")


@pytest.mark.parametrize("package", ["openai", "anthropic"])
def test_a_matching_environment_passes_the_sdk_checks_without_a_request(
    package: str,
) -> None:
    pinned = PREFLIGHT.pinned_version(REQUIREMENTS, package)

    checks = _by_name(
        PREFLIGHT.run_preflight(
            package,
            REQUIREMENTS,
            build_client=StandInClient,
            installed_version=lambda name: pinned,
        )
    )

    assert checks[f"{package} installed version"].passed
    assert checks[f"{package} client construction"].passed
    resource = PREFLIGHT.RESOURCE[package]
    assert checks[f"{package} {resource} resource"].passed


def test_a_version_that_differs_from_the_pin_fails_its_check() -> None:
    checks = _by_name(
        PREFLIGHT.run_preflight(
            "openai",
            REQUIREMENTS,
            build_client=StandInClient,
            installed_version=lambda name: "3.50.0",
        )
    )

    assert not checks["openai installed version"].passed
    assert "3.50.0" in checks["openai installed version"].detail


def test_a_client_that_cannot_be_built_reports_only_the_exception_class() -> None:
    def failing_build() -> Any:
        raise RuntimeError("secret-looking detail that must not be printed")

    checks = _by_name(
        PREFLIGHT.run_preflight(
            "anthropic",
            REQUIREMENTS,
            build_client=failing_build,
            installed_version=lambda name: "0.109.0",
        )
    )

    failed = checks["anthropic client construction"]
    assert not failed.passed
    assert failed.detail == "RuntimeError"
    assert "secret-looking" not in failed.detail


def test_a_resource_that_cannot_load_is_reported_by_class() -> None:
    class BlockedClient:
        """A client whose resource cannot be loaded."""

        @property
        def responses(self) -> Any:
            """Fail as a blocked native module would."""
            raise ImportError("blocked by a local policy")

    checks = _by_name(
        PREFLIGHT.run_preflight(
            "openai",
            REQUIREMENTS,
            build_client=BlockedClient,
            installed_version=lambda name: "2.54.0",
        )
    )

    assert not checks["openai responses resource"].passed
    assert checks["openai responses resource"].detail == "ImportError"


def test_a_missing_sdk_is_reported_without_building_a_client() -> None:
    def missing(name: str) -> str:
        """Raise as an absent package would."""
        raise importlib.metadata.PackageNotFoundError(name)

    built: list[object] = []

    def record_build() -> StandInClient:
        built.append(1)
        return StandInClient()

    checks = _by_name(
        PREFLIGHT.run_preflight(
            "anthropic",
            REQUIREMENTS,
            build_client=record_build,
            installed_version=missing,
        )
    )

    assert not checks["anthropic installed version"].passed
    assert built == []
