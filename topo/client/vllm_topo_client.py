# -*- coding: utf-8 -*-
# @Author: zibai.gj
import os
import traceback
from typing import Optional

import requests

from patio.logger import init_logger
from patio.topo import utils
from patio.topo.client.base_topo_client import GroupTopoClient

logger = init_logger(__name__)


def get_vllm_proxy_endpoint(worker_info: dict) -> Optional[str]:
    rbg_group_name = os.getenv("GROUP_NAME")
    if rbg_group_name is None:
        raise Exception("GROUP_NAME is not set")

    router_role_name = os.getenv("ROUTER_ROLE_NAME")
    if router_role_name is None:
        raise Exception("ROUTER_ROLE_NAME is not set")

    router_port = os.getenv("ROUTER_PORT")
    if router_port is None:
        raise Exception("ROUTER_PORT is not set")

    return f"{rbg_group_name}-{router_role_name}-0.s-{rbg_group_name}-{router_role_name}:{router_port}"


def get_worker_instance(worker_info: dict) -> str:
    if worker_info.get("instance"):
        return worker_info["instance"]

    port = worker_info.get("port", "8000")
    worker_endpoint = os.getenv("POD_IP")
    if worker_endpoint is not None:
        return f"{worker_endpoint}:{port}"

    rbg_group_name = os.getenv("GROUP_NAME")
    if rbg_group_name is None:
        raise Exception("GROUP_NAME is not set")

    role_name = os.getenv("ROLE_NAME")
    if role_name is None:
        raise Exception("ROLE_NAME is not set")

    role_index = os.getenv("ROLE_INDEX")
    if role_index is None:
        raise Exception("ROLE_INDEX is not set")

    return f"{rbg_group_name}-{role_name}-{role_index}.s-{rbg_group_name}-{role_name}:{port}"


def get_health_check_endpoint(worker_info: dict) -> str:
    port = worker_info.get("port", "8000")
    local_url = os.getenv("POD_IP")
    if local_url is None:
        local_url = "localhost"
    return f"{local_url}:{port}"


class VLLMProxyTopoClient(GroupTopoClient):
    def __init__(self, worker_info: dict):
        self.health_check_endpoint = get_health_check_endpoint(worker_info)
        self.vllm_proxy_endpoint = get_vllm_proxy_endpoint(worker_info)

    def wait_engine_ready(self, worker_info: dict) -> bool:
        def f():
            health_check_url = f"http://{self.health_check_endpoint}/health"
            resp = requests.get(health_check_url)
            if resp.status_code == 200:
                logger.info("Health check OK, inference engine is now ready.")
            else:
                raise Exception(
                    f"health check failed, url: {health_check_url}, "
                    f"status_code: {resp.status_code}, content: {resp.text}"
                )

        try:
            utils.retry(f, retry_times=60, interval=3)
            return True
        except Exception as e:
            logger.error(f"failed to check if worker engine is ready: {e}")
            traceback.print_exc()
            return False

    def register(self, url: str, worker_info: dict, file_path: Optional[str] = None) -> bool:
        try:
            worker_type = worker_info.get("type") or worker_info.get("worker_type")
            if worker_type is None:
                raise Exception("worker type is not set")

            payload = {
                "type": worker_type,
                "instance": get_worker_instance(worker_info),
            }

            def f():
                instance_add_url = f"http://{self.vllm_proxy_endpoint}/instances/add"
                resp = requests.post(
                    instance_add_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                if 200 <= resp.status_code < 300:
                    logger.info(f"registered vLLM instance successfully. instance: {payload['instance']}")
                else:
                    raise Exception(
                        f"register failed, url: {instance_add_url}, "
                        f"status_code: {resp.status_code}, content: {resp.text}"
                    )

            utils.retry(f, retry_times=60, interval=3)
            return True
        except Exception as e:
            logger.error(f"failed to register vLLM instance: {e}")
            traceback.print_exc()
            return False

    def unregister(self):
        logger.info("vLLM proxy unregister is not supported, skipping unregister")
        return True
