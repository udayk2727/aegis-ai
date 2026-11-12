"""Helpers that run the deterministic simulator for incident tests.

Seeds 301 onward are used here. Seeds 201 to 205 were inspected while choosing
the default correlation window, so tests avoid them to keep the checks from
reusing the data that shaped the defaults.
"""

from app.domain.telemetry import GroundTruthLabel, ScenarioId, TelemetryEvent
from app.services.baseline_trust import TrustedBaselines
from app.services.incident_pipeline import ObservedRun
from app.services.offline_validation import approve_baseline_specs
from app.services.simulation import SimulationRun, SimulationSpec, run_simulation

TEST_TICKS = 288
BASELINE_SEEDS = (301, 302)
EVALUATION_SEED = 311
FAULT_SCENARIOS = tuple(
    scenario for scenario in ScenarioId if scenario is not ScenarioId.NORMAL_OPERATION
)


def simulate(scenario: ScenarioId, seed: int, ticks: int = TEST_TICKS) -> SimulationRun:
    """Run one deterministic simulation with the default cadence."""
    return run_simulation(SimulationSpec(scenario=scenario, seed=seed, ticks=ticks))


def observe(run: SimulationRun) -> ObservedRun:
    """Present a simulation run to the incident pipeline as plain values."""
    return ObservedRun(
        run_id=run.run_id,
        tick_interval_seconds=run.spec.tick_interval_seconds,
        events=run.events,
    )


def baseline_runs() -> list[SimulationRun]:
    """Fault-free runs used as normal behaviour for the detector."""
    return [simulate(ScenarioId.NORMAL_OPERATION, seed) for seed in BASELINE_SEEDS]


def trusted_baselines() -> TrustedBaselines:
    """Approve the baseline seeds through the offline check, for the test suite.

    This is the offline testing path. It runs the same synthetic validation a
    manifest would, so tests never construct an approval by hand.
    """
    return TrustedBaselines(
        tuple(
            approve_baseline_specs(
                SimulationSpec(
                    scenario=ScenarioId.NORMAL_OPERATION,
                    seed=seed,
                    ticks=TEST_TICKS,
                )
                for seed in BASELINE_SEEDS
            )
        )
    )


def with_flipped_labels(
    events: tuple[TelemetryEvent, ...],
) -> tuple[TelemetryEvent, ...]:
    """Return copies of the events with every ground-truth label inverted.

    Used to show that incidents do not depend on labels. ``model_copy`` skips
    validation, which is acceptable here because the copy only swaps the label.
    """
    flipped_events = []
    for event in events:
        is_anomalous = not event.label.is_anomalous
        scenario = (
            ScenarioId.DATABASE_LATENCY_SPIKE
            if is_anomalous
            else ScenarioId.NORMAL_OPERATION
        )
        label = GroundTruthLabel(scenario_id=scenario, is_anomalous=is_anomalous)
        flipped_events.append(event.model_copy(update={"label": label}))
    return tuple(flipped_events)
