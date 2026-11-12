"""FastAPI dependencies shared by the feature routers."""

from fastapi import Request

from app.services.event_store import EventStore
from app.services.baseline_trust import TrustedBaselines


def get_event_store(request: Request) -> EventStore:
    """Return the event store owned by this application instance.

    The store lives on the app so each application, including each test
    application, starts empty.
    """
    store: EventStore = request.app.state.event_store
    return store


def get_trusted_baselines(request: Request) -> TrustedBaselines:
    """Return the baseline registry owned by this application instance."""
    trusted: TrustedBaselines = request.app.state.trusted_baselines
    return trusted
