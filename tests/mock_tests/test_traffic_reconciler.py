from pathlib import Path

from patio.engine.probes.sglang import SGLangEngineProbe
from patio.engine.probes.vllm import VLLMEngineProbe
from patio.registration import (
    ManualDrainCondition,
    MembershipState,
    ProcessCondition,
    QuiescentState,
    RBGCondition,
    ReconcileAction,
    RoleInstanceState,
    TrafficReconciler,
)
from patio.registration.adapters.base import RouterAdapter


class FakeRouterAdapter(RouterAdapter):
    def __init__(self, initial: MembershipState = MembershipState.ABSENT) -> None:
        self.membership = initial
        self.calls: list[str] = []

    @property
    def protocol(self) -> str:
        return "fake"

    def add(self) -> MembershipState:
        self.calls.append("add")
        self.membership = MembershipState.PRESENT
        return self.membership

    def remove(self) -> MembershipState:
        self.calls.append("remove")
        self.membership = MembershipState.ABSENT
        return self.membership

    def query(self) -> MembershipState:
        self.calls.append("query")
        return self.membership


class FakeEngineProbe:
    def __init__(
        self,
        ready: bool = True,
        quiescent: QuiescentState = QuiescentState.YES,
    ) -> None:
        self.ready_value = ready
        self.quiescent_value = quiescent

    def ready(self) -> bool:
        return self.ready_value

    def quiescent(self) -> QuiescentState:
        return self.quiescent_value


class AlwaysTrueCondition:
    def value(self) -> bool:
        return True


class AlwaysFalseCondition:
    def value(self) -> bool:
        return False


def build_reconciler(
    condition_value: bool,
    membership: MembershipState,
) -> tuple[TrafficReconciler, FakeRouterAdapter]:
    condition = AlwaysTrueCondition() if condition_value else AlwaysFalseCondition()
    router = FakeRouterAdapter(membership)
    reconciler = TrafficReconciler(
        conditions={"test": condition},
        router_adapter=router,
        engine_probe=FakeEngineProbe(),
        engine_name="fake",
        interval=0.01,
    )
    return reconciler, router


def test_reconciler_adds_when_conditions_allow_and_member_is_absent():
    reconciler, router = build_reconciler(True, MembershipState.ABSENT)

    snapshot = reconciler.run_once()

    assert snapshot.action is ReconcileAction.ADD
    assert snapshot.member is MembershipState.PRESENT
    assert "add" in router.calls


def test_reconciler_removes_when_conditions_disallow_and_member_is_present():
    reconciler, router = build_reconciler(False, MembershipState.PRESENT)

    snapshot = reconciler.run_once()

    assert snapshot.action is ReconcileAction.REMOVE
    assert snapshot.member is MembershipState.ABSENT
    assert "remove" in router.calls


def test_reconciler_does_nothing_when_state_matches():
    reconciler, router = build_reconciler(True, MembershipState.PRESENT)

    snapshot = reconciler.run_once()

    assert snapshot.action is ReconcileAction.NOOP
    assert snapshot.member is MembershipState.PRESENT
    assert router.calls == ["query"]


def test_process_condition_stops():
    condition = ProcessCondition()
    assert condition.value() is True

    condition.stop()

    assert condition.value() is False


def test_manual_drain_condition_parses_downward_api_file(tmp_path: Path):
    path = tmp_path / "annotations"
    path.write_text('patio.workloads.x-k8s.io/drain="true"\n', encoding="utf-8")
    condition = ManualDrainCondition(str(path))

    assert condition.value() is False

    path.write_text('patio.workloads.x-k8s.io/drain="false"\n', encoding="utf-8")
    assert condition.value() is True


def test_manual_drain_condition_missing_file_is_not_draining(tmp_path: Path):
    condition = ManualDrainCondition(str(tmp_path / "missing"))

    assert condition.value() is True


def test_rbg_condition_requires_running_and_post_ready():
    condition = RBGCondition(lambda: RoleInstanceState("Running", None))
    assert condition.value() is True

    condition = RBGCondition(lambda: RoleInstanceState("Running", "PostReady"))
    assert condition.value() is True

    condition = RBGCondition(lambda: RoleInstanceState("Updating", None))
    assert condition.value() is False

    condition = RBGCondition(lambda: RoleInstanceState("Running", "PreUpdate"))
    assert condition.value() is False


def test_sgl_engine_probe_uses_prefill_metrics():
    probe = SGLangEngineProbe(
        {"worker_type": "prefill"}, "http://engine", timeout=1
    )
    probe.session = FakeMetricsSession(
        metric_names=(
            "sglang:num_running_reqs",
            "sglang:num_queue_reqs",
            "sglang:num_prefill_bootstrap_queue_reqs",
            "sglang:num_prefill_inflight_queue_reqs",
        ),
        values=(0, 0, 0, 0),
    )

    assert probe.ready() is True
    assert probe.quiescent() is QuiescentState.YES


def test_sgl_engine_probe_requires_prefill_transfer_metrics():
    probe = SGLangEngineProbe(
        {"worker_type": "prefill"}, "http://engine", timeout=1
    )
    probe.session = FakeMetricsSession(
        metric_names=(
            "sglang:num_running_reqs",
            "sglang:num_queue_reqs",
        ),
        values=(0, 0),
    )

    assert probe.quiescent() is QuiescentState.UNKNOWN


def test_vllm_engine_probe_uses_running_and_waiting_only():
    probe = VLLMEngineProbe({"type": "decode"}, "http://engine", timeout=1)
    probe.session = FakeMetricsSession(
        metric_names=(
            "vllm:num_requests_running",
            "vllm:num_requests_waiting",
            "vllm:num_requests_swapped",
        ),
        values=(0, 0, 1),
    )

    assert probe.ready() is True
    assert probe.quiescent() is QuiescentState.YES


class FakeMetricsSession:
    def __init__(self, metric_names: tuple[str, ...], values: tuple[float, ...]):
        self.metric_names = metric_names
        self.values = values

    def get(self, url: str, timeout: float):
        if url.endswith("/health"):
            return FakeResponse(200, "")
        assert url.endswith("/metrics")
        text = "\n".join(
            f"{name} {value}" for name, value in zip(self.metric_names, self.values)
        )
        return FakeResponse(200, text)


class FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
