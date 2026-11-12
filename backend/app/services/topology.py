"""Declared service dependencies used to decide which alerts may correlate.

The topology is a fixed description of the simulated system, not something
inferred from telemetry. An edge means one service calls another. Correlation
treats an edge as undirected, because a fault at either end can show up at the
other.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from app.services.simulation import API_GATEWAY, ORDERS_DB, ORDERS_SERVICE


@dataclass(frozen=True)
class ServiceTopology:
    """Directed call edges, stored as (caller, callee) pairs."""

    calls: frozenset[tuple[str, str]]

    def are_related(self, first: str, second: str) -> bool:
        """True when two services are the same or one calls the other directly."""
        return (
            first == second
            or (first, second) in self.calls
            or (second, first) in self.calls
        )

    def any_related(self, first: Iterable[str], second: Iterable[str]) -> bool:
        """True when some service in one set is related to some service in the other."""
        second_services = tuple(second)
        return any(
            self.are_related(left, right) for left in first for right in second_services
        )


DEFAULT_TOPOLOGY = ServiceTopology(
    calls=frozenset(
        {
            (API_GATEWAY, ORDERS_SERVICE),
            (ORDERS_SERVICE, ORDERS_DB),
        }
    ),
)
