from typing import Any

import requests

from patio.logger import init_logger
from patio.registration.adapters.base import RouterAdapter
from patio.registration.models import MembershipState

logger = init_logger(__name__)


class VLLMProxyRouterAdapter(RouterAdapter):
    def __init__(
        self,
        worker_info: dict,
        proxy_endpoint: str,
        api_key: str,
        timeout: float = 10.0,
    ) -> None:
        self.worker_info = worker_info.copy()
        self.proxy_endpoint = proxy_endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.instance = str(worker_info["instance"])
        self.worker_type = str(
            worker_info.get("type") or worker_info.get("worker_type")
        )
        self.session = requests.Session()

    @property
    def protocol(self) -> str:
        return "xpyd-proxy"

    def add(self) -> MembershipState:
        try:
            response = self.session.post(
                f"{self.proxy_endpoint}/instances/add",
                json={"type": self.worker_type, "instance": self.instance},
                headers=self._headers(),
                timeout=self.timeout,
            )
            if not 200 <= response.status_code < 300:
                logger.warning(
                    "vLLM proxy add failed: status=%s body=%s",
                    response.status_code,
                    response.text,
                )
                return MembershipState.UNKNOWN
            return MembershipState.PRESENT
        except requests.RequestException as exc:
            logger.warning("vLLM proxy add failed: %s", exc)
            return MembershipState.UNKNOWN

    def remove(self) -> MembershipState:
        try:
            response = self.session.post(
                f"{self.proxy_endpoint}/instances/remove",
                json={
                    "type": self.worker_type,
                    "instance": self.instance,
                    "timeout_seconds": 60,
                },
                headers=self._headers(),
                timeout=self.timeout,
            )
            if not 200 <= response.status_code < 300:
                logger.warning(
                    "vLLM proxy remove failed: status=%s body=%s",
                    response.status_code,
                    response.text,
                )
                return MembershipState.UNKNOWN
            return MembershipState.ABSENT
        except requests.RequestException as exc:
            logger.warning("vLLM proxy remove failed: %s", exc)
            return MembershipState.UNKNOWN

    def query(self) -> MembershipState:
        try:
            response = self.session.get(
                f"{self.proxy_endpoint}/status/instances",
                headers=self._headers(),
                timeout=self.timeout,
            )
            if response.status_code != 200:
                logger.warning(
                    "vLLM proxy query failed: status=%s body=%s",
                    response.status_code,
                    response.text,
                )
                return MembershipState.UNKNOWN

            instances = self._extract_instances(response.json())
            for instance in instances:
                address = instance.get("address") or instance.get("instance")
                if address == self.instance:
                    return MembershipState.PRESENT
            return MembershipState.ABSENT
        except (requests.RequestException, ValueError) as exc:
            logger.warning("vLLM proxy query failed: %s", exc)
            return MembershipState.UNKNOWN

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    @staticmethod
    def _extract_instances(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            instances = payload.get("instances", payload.get("data", []))
            if isinstance(instances, list):
                return [item for item in instances if isinstance(item, dict)]
        return []
