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
    SecretStr,
    ValidationError,
    model_validator,
)

ENVIRONMENT_VARIABLE = "AEGIS_ENVIRONMENT"
SIMULATION_API_VARIABLE = "AEGIS_SIMULATION_API_ENABLED"
DEMO_API_VARIABLE = "AEGIS_DEMO_API_ENABLED"
TRUSTED_BASELINES_VARIABLE = "AEGIS_TRUSTED_BASELINES_PATH"
HOSTED_DEMO_VARIABLE = "AEGIS_HOSTED_DEMO_ENABLED"
HOSTED_SNAPSHOT_VARIABLE = "AEGIS_HOSTED_DEMO_SNAPSHOT_PATH"
INVESTIGATION_CAP_VARIABLE = "AEGIS_INVESTIGATION_MAX_CITED_EVENTS"
LIVE_MODEL_VARIABLE = "AEGIS_LLM_MODEL"
OPENAI_API_KEY_VARIABLE = "OPENAI_API_KEY"
ANTHROPIC_API_KEY_VARIABLE = "ANTHROPIC_API_KEY"


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
    demo_api_enabled: bool = False
    trusted_baselines_path: Path | None = None
    hosted_demo_enabled: bool = False
    hosted_demo_snapshot_path: Path | None = None
    investigation_max_cited_events: Annotated[int, Field(ge=1, le=1000)] = 100

    @model_validator(mode="after")
    def _hosted_demo_mode(self) -> Self:
        """Require an explicit production environment and a snapshot for hosted mode.

        Hosted mode is the one production configuration that loads synthetic data.
        It is off by default, and the snapshot path is accepted only with it on.
        """
        if self.hosted_demo_enabled:
            if self.environment is not Environment.PRODUCTION:
                raise ValueError(
                    "hosted demo mode runs with AEGIS_ENVIRONMENT=production"
                )
            if self.hosted_demo_snapshot_path is None:
                raise ValueError("hosted demo mode needs a snapshot path")
        elif self.hosted_demo_snapshot_path is not None:
            raise ValueError("a snapshot path is set but hosted demo mode is off")
        return self

    @model_validator(mode="after")
    def _development_only_features(self) -> Self:
        """Refuse development-only features when the environment is production.

        Hosted mode is production too, so these refusals still apply to it. The
        simulation endpoint, the demo flag, and the manifest stay refused, and the
        hosted demo serves its listing from the snapshot instead.
        """
        if self.environment is not Environment.PRODUCTION:
            return self
        if self.simulation_api_enabled:
            raise ValueError(
                "the simulation API has no authentication and is development-only; "
                "it cannot be enabled when the environment is production"
            )
        if self.demo_api_enabled:
            raise ValueError(
                "the demo API is development-only; "
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
    if DEMO_API_VARIABLE in environ:
        raw["demo_api_enabled"] = environ[DEMO_API_VARIABLE]
    if TRUSTED_BASELINES_VARIABLE in environ:
        raw["trusted_baselines_path"] = environ[TRUSTED_BASELINES_VARIABLE]
    if HOSTED_DEMO_VARIABLE in environ:
        raw["hosted_demo_enabled"] = environ[HOSTED_DEMO_VARIABLE]
    if HOSTED_SNAPSHOT_VARIABLE in environ:
        raw["hosted_demo_snapshot_path"] = environ[HOSTED_SNAPSHOT_VARIABLE]
    if INVESTIGATION_CAP_VARIABLE in environ:
        raw["investigation_max_cited_events"] = environ[INVESTIGATION_CAP_VARIABLE]
    try:
        return Settings.model_validate(raw)
    except ValidationError as error:
        reasons = "; ".join(str(problem["msg"]) for problem in error.errors())
        raise ConfigurationError(f"invalid configuration: {reasons}") from error


class LiveModelSettings(BaseModel):
    """Settings for the optional live model provider, used by local development only.

    Both values come from the environment and neither has a default. The key is
    held as a ``SecretStr``, so it does not appear in a repr or a dump.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: Annotated[
        str,
        Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$"),
    ]
    api_key: SecretStr


def load_live_model_settings(
    environ: Mapping[str, str],
    *,
    key_variable: str = OPENAI_API_KEY_VARIABLE,
) -> LiveModelSettings:
    """Build live-provider settings from the environment, failing closed.

    Error text names the variable that is missing or invalid, and never its value,
    so a bad configuration cannot print a credential. The key variable is a
    parameter so that each provider reads its own key and the same model setting.
    """
    model = environ.get(LIVE_MODEL_VARIABLE, "")
    api_key = environ.get(key_variable, "")
    if not model:
        raise ConfigurationError(f"{LIVE_MODEL_VARIABLE} is not set")
    if not api_key:
        raise ConfigurationError(f"{key_variable} is not set")
    try:
        return LiveModelSettings(model=model, api_key=SecretStr(api_key))
    except ValidationError:
        raise ConfigurationError(
            f"{LIVE_MODEL_VARIABLE} is not a valid model identifier"
        ) from None
