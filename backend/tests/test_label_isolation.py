"""Static checks that ground-truth labels stay out of detection and correlation.

The checks read each module's syntax tree. A label read in any of these modules
fails the test, even if the value happens not to change any output.
"""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"

# Modules that decide what an incident is. They may use predictions, values,
# timestamps, and series identity, but never the ground-truth label.
DETECTION_AND_CORRELATION = (
    "services/detection.py",
    "services/detectors.py",
    "services/incident.py",
    "services/correlation.py",
    "services/evidence.py",
    "services/timeline.py",
    "services/topology.py",
    "services/incident_pipeline.py",
    "routers/incidents.py",
    "routers/incident_access.py",
    "domain/investigation_records.py",
    "services/investigation_catalog.py",
    "services/investigation_observations.py",
    "services/hypotheses.py",
    "services/investigation.py",
    "services/investigation_provenance.py",
    "routers/investigations.py",
    "domain/agent_records.py",
    "services/agent_provider.py",
    "services/agent_tools.py",
    "services/agent_verification.py",
    "services/agent_orchestration.py",
    "services/agent_fake_provider.py",
    "services/agent_openai_provider.py",
    "services/agent_budget.py",
    "services/agent_transport.py",
    "services/agent_anthropic_provider.py",
)
LABEL_ATTRIBUTES = {"label", "is_anomalous", "scenario_id", "anomaly_window"}
LABEL_NAMES = {"GroundTruthLabel", "ScenarioId"}

# Label-aware modules. Detection and correlation must not reach any of them, even
# indirectly, through the application's own imports.
OFFLINE_ONLY_MODULES = {
    "app.services.offline_validation",
    "app.services.correlation_validation",
    "app.services.trusted_baselines",
    "app.services.baseline",
    "app.services.evaluation",
    "app.services.experiment",
    "app.services.investigation_evaluation",
}


def _syntax_tree(relative: str) -> ast.Module:
    """Parse one application module."""
    return ast.parse((APP / relative).read_text(encoding="utf-8"))


@pytest.mark.parametrize("relative", DETECTION_AND_CORRELATION)
def test_detection_and_correlation_never_read_a_label(relative: str) -> None:
    tree = _syntax_tree(relative)

    attribute_reads = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in LABEL_ATTRIBUTES
    }
    name_reads = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id in LABEL_NAMES
    }
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in LABEL_NAMES
    }

    assert attribute_reads == set(), f"{relative} reads {attribute_reads}"
    assert name_reads == set(), f"{relative} uses {name_reads}"
    assert imported == set(), f"{relative} imports {imported}"


def _app_imports(module: str) -> set[str]:
    """Return the application modules that one application module imports directly."""
    path = APP.parent / (module.replace(".", "/") + ".py")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    return {name for name in imported if name.startswith("app.")}


def _reachable(start: str) -> set[str]:
    """Return every application module reachable from ``start`` by imports."""
    seen: set[str] = set()
    pending = [start]
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(_app_imports(module))
    return seen


@pytest.mark.parametrize("relative", DETECTION_AND_CORRELATION)
def test_detection_and_correlation_never_reach_label_aware_modules(
    relative: str,
) -> None:
    start = "app." + relative.removesuffix(".py").replace("/", ".")

    reached = _reachable(start)

    assert reached.isdisjoint(OFFLINE_ONLY_MODULES), (
        f"{relative} reaches {sorted(reached & OFFLINE_ONLY_MODULES)}"
    )
