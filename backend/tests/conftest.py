"""Shared pytest fixtures for backend tests."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.config import Environment, Settings
from app.main import create_app

DEVELOPMENT_SETTINGS = Settings(
    environment=Environment.DEVELOPMENT,
    simulation_api_enabled=True,
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Provide a TestClient bound to a fresh development application instance.

    Settings are passed explicitly so results never depend on the environment
    variables of the machine running the tests.
    """
    with TestClient(create_app(DEVELOPMENT_SETTINGS)) as test_client:
        yield test_client
