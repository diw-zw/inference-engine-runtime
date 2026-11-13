import os
from typing import Any

import requests

from patio import envs
from patio.logger import init_logger
from patio.registration.adapters.base import RouterAdapter
from patio.registration.models import MembershipState

logger = init_logger(__name__)


class SGLangRouterAdapter(RouterAdapter):
    def __init__(
        self,
        worker_info: dict,
        router_endpoint: str | None = None,
        worker_endpoint: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.worker_info = worker_info.copy()
        self.router_endpoint = self._normalize_endpoint(
            router_endpoint or self._router_endpoint()
        )
        self.worker_endpoint = worker_endpoint or self._worker_endpoint()
        self.timeout = timeout or envs.TOPO_REGISTER_TIMEOUT
        self.worker_id: str | None = None
        self.session = requests.Session()

    @property
    def protocol(self) -> str:
        return "sglang"

    def add(self) -> MembershipState:
        payload = self.worker_info.copy()
        payload["url"] = f"http://{self.worker_endpoint}"
        payload.pop("port", None)

        try:
            response = self.session.post(
                f"{self.router_endpoint}/workers",
                json=payload,
                timeout=self.timeout,
            )
            if response.status_code != 202:
                logger.warning(
                    "SGLang router add failed: status=%s body=%s",
                    response.status_code,
                    response.text,
                )
                return MembershipState.UNKNOWN

            worker_id = response.json().get("worker_id")
            if not worker_id:
                logger.warning("SGLang router add response has no worker_id")
                return MembershipState.UNKNOWN

            self.worker_id = str(worker_id)
            return MembershipState.PRESENT
        except (requests.RequestException, ValueError) as exc:
            logger.warning("SGLang router add failed: %s", exc)
            return MembershipState.UNKNOWN

    def remove(self) -> MembershipState:
        if self.worker_id is None:
            if self.query() is not MembershipState.PRESENT:
                return MembershipState.ABSENT
            if self.worker_id is None:
                logger.warning("SGLang router remove could not resolve worker_id")
                return MembershipState.UNKNOWN

        try:
            response = self.session.delete(
                f"{self.router_endpoint}/workers/{self.worker_id}",
                timeout=self.timeout,
            )
            if response.status_code != 202:
                logger.warning(
                    "SGLang router remove failed: status=%s body=%s",
                    response.status_code,
                    response.text,
                )
                return MembershipState.UNKNOWN
            return MembershipState.ABSENT
        except requests.RequestException as exc:
            logger.warning("SGLang router remove failed: %s", exc)
            return MembershipState.UNKNOWN

    def query(self) -> MembershipState:
        try:
            response = self.session.get(
                f"{self.router_endpoint}/workers", timeout=self.timeout
            )
            if response.status_code != 200:
                logger.warning(
                    "SGLang router query failed: status=%s body=%s",
                    response.status_code,
                    response.text,
                )
                return MembershipState.UNKNOWN

            workers = self._extract_workers(response.json())
            for worker in workers:
                endpoint = self._worker_endpoint_from_record(worker)
                worker_id = worker.get("worker_id") or worker.get("id")
                if endpoint == self.worker_endpoint:
                    if worker_id:
                        self.worker_id = str(worker_id)
                    return MembershipState.PRESENT
            return MembershipState.ABSENT
        except (requests.RequestException, ValueError) as exc:
            logger.warning("SGLang router query failed: %s", exc)
            return MembershipState.UNKNOWN

    def _router_endpoint(self) -> str:
        group_name = envs.GROUP_NAME
        router_role_name = envs.ROUTER_ROLE_NAME
        router_port = envs.ROUTER_PORT
        if not group_name or not router_role_name or not router_port:
            raise ValueError("RBG_GROUP_NAME, ROUTER_ROLE_NAME, and ROUTER_PORT are required")

        return (
            f"{group_name}-{router_role_name}-0."
            f"s-{group_name}-{router_role_name}:{router_port}"
        )

    def _worker_endpoint(self) -> str:
        port = self.worker_info.get("port", "30000")
        pod_ip = os.getenv("POD_IP")
        if pod_ip:
            return f"{pod_ip}:{port}"

        group_name = envs.GROUP_NAME
        role_name = envs.ROLE_NAME
        role_index = envs.ROLE_INDEX
        if not group_name or not role_name or not role_index:
            raise ValueError("POD_IP or RBG_ROLE_NAME and RBG_ROLE_INDEX are required")

        return f"{group_name}-{role_name}-{role_index}.s-{group_name}-{role_name}:{port}"

    @staticmethod
    def _normalize_endpoint(endpoint: str) -> str:
        return endpoint.rstrip("/")

    @staticmethod
    def _extract_workers(payload: Any) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            workers = payload.get("workers", payload.get("data", []))
            if isinstance(workers, list):
                return [item for item in workers if isinstance(item, dict)]
        return []

    @staticmethod
    def _worker_endpoint_from_record(worker: dict) -> str | None:
        endpoint = worker.get("url") or worker.get("endpoint") or worker.get("worker_url")
        if endpoint is None:
            return None
        return str(endpoint).removeprefix("http://").split("/", 1)[0]
