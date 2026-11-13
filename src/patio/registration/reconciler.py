import threading
import time

from patio.logger import init_logger
from patio.registration.adapters.base import RouterAdapter
from patio.registration.conditions import Condition
from patio.registration.metrics import (
    RECONCILE_ACTION_DURATION,
    RECONCILE_CONDITION,
    RECONCILE_MEMBER_STATE,
    RECONCILE_PASS_TOTAL,
    RECONCILE_QUIESCENT_STATE,
)
from patio.registration.models import (
    MembershipState,
    QuiescentState,
    ReconcileAction,
    ReconcileSnapshot,
)

logger = init_logger(__name__)

_MEMBERSHIP_VALUES = {
    MembershipState.PRESENT: 2,
    MembershipState.ABSENT: 1,
    MembershipState.UNKNOWN: 0,
}

_QUIESCENT_VALUES = {
    QuiescentState.YES: 2,
    QuiescentState.NO: 1,
    QuiescentState.UNKNOWN: 0,
}


class TrafficReconciler:
    def __init__(
        self,
        conditions: dict[str, Condition],
        router_adapter: RouterAdapter,
        engine_probe,
        engine_name: str,
        interval: float = 10.0,
    ) -> None:
        self.conditions = conditions
        self.router_adapter = router_adapter
        self.engine_probe = engine_probe
        self.engine_name = engine_name
        self.interval = interval

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._snapshot: ReconcileSnapshot | None = None

    @property
    def snapshot(self) -> ReconcileSnapshot | None:
        return self._snapshot

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="patio-traffic-reconciler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        process_condition = self.conditions.get("process")
        if process_condition is not None:
            process_condition.stop()

        self._stop_event.set()
        self._wake_event.set()
        self.run_once()

        if self._thread is not None:
            self._thread.join(timeout=self.interval + 1)

    def wake(self) -> None:
        self._wake_event.set()

    def run_once(self) -> ReconcileSnapshot:
        with self._run_lock:
            condition_values = {
                name: condition.value() for name, condition in self.conditions.items()
            }
            should_register = all(condition_values.values())

            member = self._query_membership()
            quiescent = self.engine_probe.quiescent()

            if should_register and member is MembershipState.ABSENT:
                action = ReconcileAction.ADD
                member = self._add()
            elif not should_register and member is MembershipState.PRESENT:
                action = ReconcileAction.REMOVE
                member = self._remove()
            else:
                action = ReconcileAction.NOOP

            snapshot = ReconcileSnapshot(
                conditions=condition_values,
                should_register=should_register,
                member=member,
                quiescent=quiescent,
                action=action,
            )
            self._snapshot = snapshot
            self._record_metrics(snapshot)
            logger.debug("Traffic reconcile snapshot: %s", snapshot)
            return snapshot

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("Traffic reconcile pass failed: %s", exc)

            self._wake_event.wait(timeout=self.interval)
            self._wake_event.clear()

    def _query_membership(self) -> MembershipState:
        started = time.perf_counter()
        try:
            return self.router_adapter.query()
        finally:
            RECONCILE_ACTION_DURATION.labels(operation="query").observe(
                time.perf_counter() - started
            )

    def _add(self) -> MembershipState:
        started = time.perf_counter()
        try:
            return self.router_adapter.add()
        finally:
            RECONCILE_ACTION_DURATION.labels(operation="add").observe(
                time.perf_counter() - started
            )

    def _remove(self) -> MembershipState:
        started = time.perf_counter()
        try:
            return self.router_adapter.remove()
        finally:
            RECONCILE_ACTION_DURATION.labels(operation="remove").observe(
                time.perf_counter() - started
            )

    def _record_metrics(self, snapshot: ReconcileSnapshot) -> None:
        result = "success" if snapshot.member is not MembershipState.UNKNOWN else "unknown"
        RECONCILE_PASS_TOTAL.labels(result=result).inc()

        for name, value in snapshot.conditions.items():
            RECONCILE_CONDITION.labels(condition=name).set(1 if value else 0)

        RECONCILE_MEMBER_STATE.labels(
            engine=self.engine_name,
            protocol=self.router_adapter.protocol,
        ).set(_MEMBERSHIP_VALUES[snapshot.member])

        RECONCILE_QUIESCENT_STATE.labels(
            engine=self.engine_name,
            source="engine-probe",
        ).set(_QUIESCENT_VALUES[snapshot.quiescent])
