"""FastAPI dependencies shared by the feature routers."""

from fastapi import Request

from app.services.event_store import EventStore


def get_event_store(request: Request) -> EventStore:
    """Return the event store owned by this application instance.

    The store lives on the app so each application, including each test
    application, starts empty.
    """
    store: EventStore = request.app.state.event_store
    return store
