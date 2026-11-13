from typing import Mapping

from prometheus_client.parser import text_string_to_metric_families


def parse_prometheus_metrics(text: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            values[sample.name] = float(sample.value)
    return values


def metric_state(
    metrics: Mapping[str, float],
    required_metrics: tuple[str, ...],
) -> tuple[bool, bool]:
    """Return (all_present, all_zero)."""
    if any(metric not in metrics for metric in required_metrics):
        return False, False
    return True, all(metrics[metric] == 0 for metric in required_metrics)
