"""Tests for environment-driven settings and the production guard."""

import pytest
from pydantic import ValidationError

from app.config import (
    DEMO_API_VARIABLE,
    SIMULATION_API_VARIABLE,
    TRUSTED_BASELINES_VARIABLE,
    ConfigurationError,
    Environment,
    Settings,
    load_settings,
)


def test_unset_environment_fails_closed() -> None:
    settings = load_settings({})

    assert settings.environment is Environment.PRODUCTION
    assert settings.simulation_api_enabled is False


def test_development_with_simulation_api_enabled() -> None:
    settings = load_settings(
        {"AEGIS_ENVIRONMENT": "development", SIMULATION_API_VARIABLE: "true"}
    )

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.simulation_api_enabled is True


def test_development_with_simulation_api_disabled() -> None:
    settings = load_settings(
        {"AEGIS_ENVIRONMENT": "development", SIMULATION_API_VARIABLE: "false"}
    )

    assert settings.simulation_api_enabled is False


def test_production_with_simulation_api_enabled_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="development-only"):
        load_settings(
            {"AEGIS_ENVIRONMENT": "production", SIMULATION_API_VARIABLE: "true"}
        )


def test_settings_object_cannot_hold_the_unsafe_combination() -> None:
    with pytest.raises(ValidationError):
        Settings(environment=Environment.PRODUCTION, simulation_api_enabled=True)


def test_demo_api_is_off_by_default() -> None:
    settings = load_settings({"AEGIS_ENVIRONMENT": "development"})

    assert settings.demo_api_enabled is False


def test_development_with_demo_api_enabled() -> None:
    settings = load_settings(
        {"AEGIS_ENVIRONMENT": "development", DEMO_API_VARIABLE: "true"}
    )

    assert settings.demo_api_enabled is True


def test_production_with_demo_api_enabled_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="demo API is development-only"):
        load_settings({"AEGIS_ENVIRONMENT": "production", DEMO_API_VARIABLE: "true"})


def test_settings_object_cannot_hold_demo_api_in_production() -> None:
    with pytest.raises(ValidationError):
        Settings(environment=Environment.PRODUCTION, demo_api_enabled=True)


def test_unknown_environment_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        load_settings({"AEGIS_ENVIRONMENT": "staging"})


def test_non_boolean_flag_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        load_settings(
            {"AEGIS_ENVIRONMENT": "development", SIMULATION_API_VARIABLE: "maybe"}
        )


def test_unknown_settings_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"environment": "development", "debug": True})


HOSTED_ON = {
    "AEGIS_ENVIRONMENT": "production",
    "AEGIS_HOSTED_DEMO_ENABLED": "true",
    "AEGIS_HOSTED_DEMO_SNAPSHOT_PATH": "demo/hosted_snapshot.json",
}


def test_hosted_demo_is_off_by_default() -> None:
    settings = load_settings({"AEGIS_ENVIRONMENT": "production"})

    assert settings.hosted_demo_enabled is False
    assert settings.hosted_demo_snapshot_path is None


def test_hosted_demo_with_production_and_a_snapshot_is_accepted() -> None:
    settings = load_settings(HOSTED_ON)

    assert settings.hosted_demo_enabled is True
    assert settings.environment is Environment.PRODUCTION


def test_hosted_demo_outside_production_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="AEGIS_ENVIRONMENT=production"):
        load_settings({**HOSTED_ON, "AEGIS_ENVIRONMENT": "development"})


def test_hosted_demo_without_a_snapshot_path_is_refused() -> None:
    environ = {
        key: value
        for key, value in HOSTED_ON.items()
        if key != "AEGIS_HOSTED_DEMO_SNAPSHOT_PATH"
    }

    with pytest.raises(ConfigurationError, match="snapshot path"):
        load_settings(environ)


def test_a_snapshot_path_without_hosted_mode_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="hosted demo mode is off"):
        load_settings(
            {
                "AEGIS_ENVIRONMENT": "production",
                "AEGIS_HOSTED_DEMO_SNAPSHOT_PATH": "demo/hosted_snapshot.json",
            }
        )


@pytest.mark.parametrize(
    "extra",
    [
        {SIMULATION_API_VARIABLE: "true"},
        {DEMO_API_VARIABLE: "true"},
        {TRUSTED_BASELINES_VARIABLE: "local/trusted_baselines.json"},
    ],
)
def test_hosted_demo_keeps_the_development_refusals(extra: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError, match="development-only"):
        load_settings({**HOSTED_ON, **extra})


@pytest.mark.parametrize(
    "variable", [SIMULATION_API_VARIABLE, DEMO_API_VARIABLE, TRUSTED_BASELINES_VARIABLE]
)
def test_ordinary_production_still_refuses_each_development_flag(variable: str) -> None:
    value = "true"
    if variable == TRUSTED_BASELINES_VARIABLE:
        value = "local/trusted_baselines.json"

    with pytest.raises(ConfigurationError, match="development-only"):
        load_settings({"AEGIS_ENVIRONMENT": "production", variable: value})
