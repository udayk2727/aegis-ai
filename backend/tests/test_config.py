"""Tests for environment-driven settings and the production guard."""

import pytest
from pydantic import ValidationError

from app.config import (
    SIMULATION_API_VARIABLE,
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
