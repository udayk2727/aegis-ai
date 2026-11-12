"""The trusted-baseline registry that incident detection requires. Holds no labels.

Incident construction takes a ``TrustedBaselines`` value and refuses any baseline
run it does not contain. This module only stores and answers membership
questions. It does not check labels, and it does not load or verify manifests.
Those jobs belong to ``offline_validation`` and ``trusted_baselines``, which
produce the approvals held here.

An empty registry trusts nothing, so the default is always to refuse.
"""

from dataclasses import dataclass

from app.services.simulation import SimulationSpec


@dataclass(frozen=True)
class BaselineApproval:
    """A baseline run that passed offline validation, with the evidence for it."""

    run_id: str
    spec: SimulationSpec
    method: str
    checked_event_count: int


@dataclass(frozen=True)
class TrustedBaselines:
    """The set of baseline runs that incident detection may use. Empty by default."""

    approvals: tuple[BaselineApproval, ...] = ()

    @property
    def run_ids(self) -> frozenset[str]:
        """IDs of every approved run."""
        return frozenset(approval.run_id for approval in self.approvals)

    def is_approved(self, run_id: str) -> bool:
        """True only for a run that passed offline validation."""
        return run_id in self.run_ids
