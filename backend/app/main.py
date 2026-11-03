"""Application factory for the AEGIS AI backend.

Run locally from the repository root with the simulation API enabled. Both
variables are needed; the endpoint is refused in production and off by default:

    $env:AEGIS_ENVIRONMENT = "development"
    $env:AEGIS_SIMULATION_API_ENABLED = "true"
    python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --reload
"""

import os

from fastapi import FastAPI

from app import __version__
from app.config import Settings, load_settings
from app.routers import analytics, events, simulations, system
from app.services.event_store import DEFAULT_EVENT_CAPACITY, EventStore


def create_app(
    settings: Settings | None = None,
    event_capacity: int = DEFAULT_EVENT_CAPACITY,
) -> FastAPI:
    """Build and configure the FastAPI application.

    Routers are registered here so the entry point stays small and each
    feature area can be developed and tested in isolation. Shared state is
    created per application so tests never see each other's data. The
    simulation router is registered only when the settings allow it.
    """
    if settings is None:
        settings = load_settings(os.environ)
    application = FastAPI(
        title="AEGIS AI",
        description="Autonomous Enterprise Incident Intelligence Platform",
        version=__version__,
    )
    application.state.event_store = EventStore(max_events=event_capacity)
    application.include_router(system.router)
    if settings.simulation_api_enabled:
        application.include_router(simulations.router)
    application.include_router(events.router)
    application.include_router(analytics.router)
    return application


app = create_app()
