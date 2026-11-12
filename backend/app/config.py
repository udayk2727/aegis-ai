"""Runtime configuration read from environment variables.

Settings fail closed. An unset environment is treated as production, so the
development-only simulation endpoint stays off unless someone opts in with
both an explicit development environment and an explicit enable flag.

The simulation endpoint has no authentication. The guard in ``Settings`` makes
it impossible to build a production configuration that exposes it, so the
endpoint cannot be switched on in production by accident. Lifting the guard
requires the authentication design in docs/architecture.md, not a config change.
"""

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

ENVIRONMENT_VARIABLE = "AEGIS_ENVIRONMENT"
SIMULATION_API_VARIABLE = "AEGIS_SIMULATION_API_ENABLED"
TRUSTED_BASELINES_VARIABLE = "AEGIS_TRUSTED_BASELINES_PATH"
INVESTIGATION_CAP_VARIABLE = "AEGIS_INVESTIGATION_MAX_CITED_EVENTS"


class Environment(StrEnum):
    """Deployment environment the process is running in."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class ConfigurationError(RuntimeError):
    """Raised when environment variables describe an unsafe or invalid setup."""


class Settings(BaseModel):
    """Validated runtime settings. An unsafe combination cannot be constructed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Environment = Environment.PRODUCTION
    simulation_api_enabled: bool = False
    trusted_baselines_path: Path | None = None
    investigation_max_cited_events: Annotated[int, Field(ge=1, le=1000)] = 100

    @model_validator(mode="after")
    def _development_only_features(self) -> Self:
        """Refuse development-only features when the environment is production."""
        if self.environment is not Environment.PRODUCTION:
            return self
        if self.simulation_api_enabled:
            raise ValueError(
                "the simulation API has no authentication and is development-only; "
                "it cannot be enabled when the environment is production"
            )
        if self.trusted_baselines_path is not None:
            raise ValueError(
                "trusted baselines from a local manifest are development-only; "
                "they cannot be loaded when the environment is production"
            )
        return self


def load_settings(environ: Mapping[str, str]) -> Settings:
    """Build settings from environment variables, failing closed on bad input."""
    raw: dict[str, Any] = {}
    if ENVIRONMENT_VARIABLE in environ:
        raw["environment"] = environ[ENVIRONMENT_VARIABLE]
    if SIMULATION_API_VARIABLE in environ:
        raw["simulation_api_enabled"] = environ[SIMULATION_API_VARIABLE]
    if TRUSTED_BASELINES_VARIABLE in environ:
        raw["trusted_baselines_path"] = environ[TRUSTED_BASELINES_VARIABLE]
    if INVESTIGATION_CAP_VARIABLE in environ:
        raw["investigation_max_cited_events"] = environ[INVESTIGATION_CAP_VARIABLE]
    try:
        return Settings.model_validate(raw)
    except ValidationError as error:
        reasons = "; ".join(str(problem["msg"]) for problem in error.errors())
        raise ConfigurationError(f"invalid configuration: {reasons}") from error
