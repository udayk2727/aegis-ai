"""Render offline telemetry charts to artifacts/plots/.

The charts come from in-process simulation runs and the analytics services, so
no API server, database, or network access is needed. Pandas and Matplotlib
are optional: install requirements-analytics.txt into the virtual environment
first (see README.md, "Running visualizations").

    python scripts/plot_telemetry.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from app.domain.telemetry import MetricName, ScenarioId, TelemetryEvent  # noqa: E402
from app.services.analytics import rolling_series  # noqa: E402
from app.services.baseline import build_baseline  # noqa: E402
from app.services.simulation import (  # noqa: E402
    AnomalyWindow,
    SimulationRun,
    SimulationSpec,
    run_simulation,
)

OUTPUT_DIR = REPO_ROOT / "artifacts" / "plots"
TICKS = 60
ROLLING_WINDOW = 5
# Okabe-Ito colors, chosen so the series stay distinct under the common
# color-vision deficiencies. Assigned in fixed order, never cycled.
SERIES_COLORS = ("#0072B2", "#D55E00", "#009E73")
FAULT_COLOR = "#999999"
GRID_COLOR = "#dddddd"


def to_frame(events: tuple[TelemetryEvent, ...]) -> pd.DataFrame:
    """Flatten events into a DataFrame with one row per observation."""
    return pd.DataFrame(
        [
            {
                "timestamp": event.timestamp,
                "service": event.service,
                "metric": event.metric_name.value,
                "value": event.value,
                "is_anomalous": event.label.is_anomalous,
            }
            for event in events
        ]
    )


def select(frame: pd.DataFrame, service: str, metric: MetricName) -> pd.DataFrame:
    """Return one series in time order."""
    rows = frame[(frame["service"] == service) & (frame["metric"] == metric.value)]
    return rows.sort_values("timestamp")


def style_axes(ax: plt.Axes, title: str, ylabel: str) -> None:
    """Apply the shared look: one y-axis, recessive grid, labelled units."""
    ax.set_title(title, loc="left")
    ax.set_ylabel(ylabel)
    ax.grid(True, color=GRID_COLOR, linewidth=0.6)
    ax.spines[["top", "right"]].set_visible(False)


def shade_fault(ax: plt.Axes, window: AnomalyWindow | None) -> None:
    """Mark the injected fault window. It is ground truth: shown, never fitted."""
    if window is None:
        return
    ax.axvspan(window.start, window.end, color=FAULT_COLOR, alpha=0.15, linewidth=0)
    ax.annotate(
        "injected fault window",
        xy=(window.start, 1.0),
        xycoords=("data", "axes fraction"),
        xytext=(4, -12),
        textcoords="offset points",
        fontsize=8,
        color="#555555",
    )


def save(fig: plt.Figure, name: str) -> Path:
    """Write the figure as PNG under the output directory and release it."""
    path = OUTPUT_DIR / name
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def plot_metric_over_time(
    run: SimulationRun,
    frame: pd.DataFrame,
    series: list[tuple[str, MetricName]],
    title: str,
    ylabel: str,
    name: str,
) -> Path:
    """Plot one or more service series over time, shading the fault window."""
    fig, ax = plt.subplots(figsize=(9, 4))
    for index, (service, metric) in enumerate(series):
        rows = select(frame, service, metric)
        ax.plot(
            rows["timestamp"],
            rows["value"],
            color=SERIES_COLORS[index],
            linewidth=1.2,
            label=service,
        )
    shade_fault(ax, run.anomaly_window)
    style_axes(ax, title, ylabel)
    if len(series) >= 2:
        ax.legend(frameon=False, fontsize=8)
    return save(fig, name)


def plot_distribution(frame: pd.DataFrame, name: str) -> Path:
    """Overlay normal and incident histograms of one metric on a shared scale."""
    rows = select(frame, "orders-db", MetricName.DB_QUERY_LATENCY_MS)
    normal = rows.loc[~rows["is_anomalous"], "value"]
    incident = rows.loc[rows["is_anomalous"], "value"]
    fig, ax = plt.subplots(figsize=(7, 4))
    low, high = rows["value"].min(), rows["value"].max()
    edges = [low + (high - low) * i / 30 for i in range(31)]
    ax.hist(
        normal,
        bins=edges,
        density=True,
        alpha=0.6,
        color=SERIES_COLORS[0],
        label="normal",
    )
    ax.hist(
        incident,
        bins=edges,
        density=True,
        alpha=0.6,
        color=SERIES_COLORS[1],
        label="incident",
    )
    style_axes(ax, "orders-db query latency: normal vs incident", "density")
    ax.set_xlabel("db_query_latency_ms")
    ax.legend(frameon=False, fontsize=8)
    return save(fig, name)


def plot_rolling_with_baseline(
    fault_run: SimulationRun,
    baseline_run: SimulationRun,
    name: str,
) -> Path:
    """Plot rolling mean against a baseline from a separate, normal run.

    The baseline comes from a different run than the one plotted, so the
    reference line is never fitted to the data it is compared with.
    """
    metric = MetricName.REQUEST_LATENCY_MS
    events = [
        event
        for event in fault_run.events
        if event.service == "api-gateway" and event.metric_name is metric
    ]
    points = rolling_series(events, ROLLING_WINDOW)
    baseline = build_baseline(baseline_run.events)
    reference = next(
        item.stats
        for item in baseline.series
        if item.key.service == "api-gateway" and item.key.metric_name is metric
    )

    timestamps = [point.timestamp for point in points]
    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(
        timestamps,
        [point.value for point in points],
        color="#bbbbbb",
        linewidth=0.8,
        label="observed",
    )
    ax.plot(
        timestamps,
        [point.mean for point in points],
        color=SERIES_COLORS[0],
        linewidth=1.6,
        label=f"rolling mean (window {ROLLING_WINDOW})",
    )
    ax.axhline(
        reference.mean,
        color=SERIES_COLORS[1],
        linestyle="--",
        linewidth=1.2,
        label="baseline mean (normal run)",
    )
    shade_fault(ax, fault_run.anomaly_window)
    style_axes(
        ax,
        "api-gateway request latency: rolling mean vs baseline",
        "request_latency_ms",
    )
    ax.legend(frameon=False, fontsize=8)
    return save(fig, name)


def main() -> None:
    """Generate every chart from fixed seeds, so reruns produce identical images."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    spike = run_simulation(
        SimulationSpec(
            scenario=ScenarioId.DATABASE_LATENCY_SPIKE, seed=7, ticks=TICKS
        ),
    )
    cpu = run_simulation(
        SimulationSpec(scenario=ScenarioId.CPU_SATURATION, seed=7, ticks=TICKS),
    )
    normal = run_simulation(
        SimulationSpec(scenario=ScenarioId.NORMAL_OPERATION, seed=3, ticks=TICKS),
    )
    spike_frame = to_frame(spike.events)
    cpu_frame = to_frame(cpu.events)

    written = [
        plot_metric_over_time(
            spike,
            spike_frame,
            [("orders-db", MetricName.DB_QUERY_LATENCY_MS)],
            "Database query latency over time",
            "milliseconds",
            "db_query_latency.png",
        ),
        plot_metric_over_time(
            spike,
            spike_frame,
            [("api-gateway", MetricName.REQUEST_LATENCY_MS)],
            "API response latency over time",
            "milliseconds",
            "api_response_latency.png",
        ),
        plot_metric_over_time(
            cpu,
            cpu_frame,
            [
                ("orders-service", MetricName.CPU_UTILIZATION_PERCENT),
                ("orders-db", MetricName.CPU_UTILIZATION_PERCENT),
            ],
            "CPU utilization over time (cpu_saturation)",
            "percent",
            "cpu_utilization.png",
        ),
        plot_distribution(spike_frame, "latency_distribution.png"),
        plot_rolling_with_baseline(spike, normal, "rolling_mean_baseline.png"),
    ]
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
