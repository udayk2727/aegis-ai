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
from app.routers import (
    analytics,
    demo,
    events,
    incidents,
    investigations,
    simulations,
    system,
)
from app.services.event_store import DEFAULT_EVENT_CAPACITY, EventStore
from app.services.baseline_trust import TrustedBaselines
from app.services.hosted_demo import (
    HostedDemoError,
    load_hosted_demo,
    store_hosted_runs,
)
from app.services.trusted_baselines import load_trusted_baselines


def create_app(
    settings: Settings | None = None,
    event_capacity: int = DEFAULT_EVENT_CAPACITY,
    trusted_baselines: TrustedBaselines | None = None,
) -> FastAPI:
    """Build and configure the FastAPI application.

    Routers are registered here so the entry point stays small and each
    feature area can be developed and tested in isolation. Shared state is
    created per application so tests never see each other's data. The
    simulation router is registered only when the settings allow it.

    Trusted baselines come from ``trusted_baselines`` when given, otherwise from
    the manifest named in the settings, otherwise none. With none, every incident
    request that names a baseline is refused. The demo listing is registered only
    when the settings enable it.

    In hosted demo mode the store and the approvals come from the offline-approved
    snapshot instead. A snapshot that is missing or does not verify raises
    HostedDemoError here, so the module import fails and the server never starts.
    """
    if settings is None:
        settings = load_settings(os.environ)
    event_store = EventStore(max_events=event_capacity)
    if settings.hosted_demo_enabled:
        if settings.hosted_demo_snapshot_path is None:
            raise HostedDemoError("hosted demo mode needs a snapshot path")
        hosted = load_hosted_demo(settings.hosted_demo_snapshot_path)
        store_hosted_runs(event_store, hosted)
        trusted_baselines = hosted.trusted
    elif trusted_baselines is None:
        trusted_baselines = (
            load_trusted_baselines(settings.trusted_baselines_path)
            if settings.trusted_baselines_path is not None
            else TrustedBaselines()
        )
    application = FastAPI(
        title="AEGIS AI",
        description="Autonomous Enterprise Incident Intelligence Platform",
        version=__version__,
    )
    application.state.event_store = event_store
    application.state.trusted_baselines = trusted_baselines
    application.state.investigation_max_cited_events = (
        settings.investigation_max_cited_events
    )
    application.include_router(system.router)
    if settings.simulation_api_enabled:
        application.include_router(simulations.router)
    application.include_router(events.router)
    application.include_router(analytics.router)
    application.include_router(incidents.router)
    application.include_router(investigations.router)
    if settings.demo_api_enabled or settings.hosted_demo_enabled:
        application.include_router(demo.router)
    return application


app = create_app()
