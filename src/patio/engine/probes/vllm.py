import requests

from patio.engine.probes.base import EngineProbe
from patio.engine.probes.metrics import metric_state, parse_prometheus_metrics
from patio.registration.models import QuiescentState


class VLLMEngineProbe(EngineProbe):
    _METRICS = (
        "vllm:num_requests_running",
        "vllm:num_requests_waiting",
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
        try:
            response = self.session.get(
                f"{self.endpoint}{self.scrape_path}", timeout=self.timeout
            )
            if response.status_code != 200:
                return QuiescentState.UNKNOWN
            metrics = parse_prometheus_metrics(response.text)
        except (requests.RequestException, ValueError):
            return QuiescentState.UNKNOWN

        all_present, all_zero = metric_state(metrics, self._METRICS)
        if not all_present:
            return QuiescentState.UNKNOWN
        return QuiescentState.YES if all_zero else QuiescentState.NO
