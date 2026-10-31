"""Event retrieval endpoint over the bounded in-memory store.

GET /api/v1/events returns one page of stored events. Every query parameter
is typed and bounded, and the page size is capped so one request cannot
return an unbounded payload.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.domain.telemetry import (
    MetricName,
    RunId,
    ScenarioId,
    ServiceName,
    TelemetryEvent,
)
from app.routers.dependencies import get_event_store
from app.services.event_store import EventFilter, EventStore

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500

router = APIRouter(prefix="/api/v1", tags=["events"])


class EventPage(BaseModel):
    """Response body for GET /api/v1/events."""

    items: list[TelemetryEvent]
    total: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=MAX_PAGE_SIZE)]
    offset: Annotated[int, Field(ge=0)]


@router.get("/events", response_model=EventPage)
def list_events(
    store: Annotated[EventStore, Depends(get_event_store)],
    run_id: RunId | None = None,
    scenario_id: ScenarioId | None = None,
    service: ServiceName | None = None,
    metric_name: MetricName | None = None,
    is_anomalous: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EventPage:
    """Return a page of stored events that match the optional filters."""
    event_filter = EventFilter(
        run_id=run_id,
        scenario_id=scenario_id,
        service=service,
        metric_name=metric_name,
        is_anomalous=is_anomalous,
    )
    items, total = store.query(event_filter, offset=offset, limit=limit)
    return EventPage(items=items, total=total, limit=limit, offset=offset)
