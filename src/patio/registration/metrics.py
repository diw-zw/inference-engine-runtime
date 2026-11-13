from prometheus_client import Counter, Gauge, Histogram

from patio.metrics.metrics import patio_registry

RECONCILE_PASS_TOTAL = Counter(
    "patio_reconcile_pass_total",
    "Total traffic reconcile passes",
    ["result"],
    registry=patio_registry,
)

RECONCILE_CONDITION = Gauge(
    "patio_reconcile_condition",
    "Current traffic reconcile condition value",
    ["condition"],
    registry=patio_registry,
)

RECONCILE_MEMBER_STATE = Gauge(
    "patio_reconcile_member_state",
    "Current router membership state",
    ["engine", "protocol"],
    registry=patio_registry,
)

RECONCILE_QUIESCENT_STATE = Gauge(
    "patio_reconcile_quiescent_state",
    "Current engine quiescent state",
    ["engine", "source"],
    registry=patio_registry,
)

RECONCILE_ACTION_DURATION = Histogram(
    "patio_reconcile_action_duration_seconds",
    "Traffic reconcile action duration",
    ["operation"],
    registry=patio_registry,
)
