import requests

from patio.engine.probes.base import EngineProbe
from patio.engine.probes.metrics import metric_state, parse_prometheus_metrics
from patio.registration.models import QuiescentState


class SGLangEngineProbe(EngineProbe):
    _PREFILL_METRICS = (
        "sglang:num_running_reqs",
        "sglang:num_queue_reqs",
        "sglang:num_prefill_bootstrap_queue_reqs",
        "sglang:num_prefill_inflight_queue_reqs",
    )
    _DECODE_METRICS = (
        "sglang:num_running_reqs",
        "sglang:num_queue_reqs",
        "sglang:num_decode_prealloc_queue_reqs",
        "sglang:num_decode_transfer_queue_reqs",
    )

    def __init__(
        self,
        worker_info: dict,
        endpoint: str,
        scrape_path: str = "/metrics",
        timeout: float = 2.0,
    ) -> None:
        self.worker_info = worker_info
        self.endpoint = endpoint.rstrip("/")
        self.scrape_path = scrape_path
        self.timeout = timeout
        self.session = requests.Session()

    def ready(self) -> bool:
        try:
            response = self.session.get(
                f"{self.endpoint}/health", timeout=self.timeout
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def quiescent(self) -> QuiescentState:
        worker_type = (
            self.worker_info.get("worker_type") or self.worker_info.get("type")
        )
        if worker_type == "prefill":
            required_metrics = self._PREFILL_METRICS
        elif worker_type == "decode":
            required_metrics = self._DECODE_METRICS
        else:
            return QuiescentState.UNKNOWN

        try:
            response = self.session.get(
                f"{self.endpoint}{self.scrape_path}", timeout=self.timeout
            )
            if response.status_code != 200:
                return QuiescentState.UNKNOWN
            metrics = parse_prometheus_metrics(response.text)
        except (requests.RequestException, ValueError):
            return QuiescentState.UNKNOWN

        all_present, all_zero = metric_state(metrics, required_metrics)
        if not all_present:
            return QuiescentState.UNKNOWN
        return QuiescentState.YES if all_zero else QuiescentState.NO
