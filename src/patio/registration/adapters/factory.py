from patio import envs
from patio.registration.adapters.base import RouterAdapter
from patio.registration.adapters.sglang import SGLangRouterAdapter
from patio.registration.adapters.vllm import VLLMProxyRouterAdapter


def create_router_adapter(
    topo_type: str,
    worker_info: dict,
    router_endpoint: str | None = None,
    api_key: str | None = None,
) -> RouterAdapter:
    if topo_type.lower() == "sglang":
        return SGLangRouterAdapter(worker_info, router_endpoint=router_endpoint)
    if topo_type.lower() == "vllm":
        endpoint = router_endpoint or envs.VLLM_PROXY_ENDPOINT
        if endpoint is None:
            group_name = envs.GROUP_NAME
            router_role_name = envs.ROUTER_ROLE_NAME
            router_port = envs.ROUTER_PORT
            if not group_name or not router_role_name or not router_port:
                raise ValueError(
                    "VLLM_PROXY_ENDPOINT or RBG_GROUP_NAME, ROUTER_ROLE_NAME, "
                    "and ROUTER_PORT are required"
                )
            endpoint = (
                f"{group_name}-{router_role_name}-0."
                f"s-{group_name}-{router_role_name}:{router_port}"
            )

        resolved_api_key = api_key or envs.VLLM_PROXY_API_KEY
        if not resolved_api_key:
            raise ValueError("VLLM_PROXY_API_KEY is required for xPyD-proxy")

        return VLLMProxyRouterAdapter(
            worker_info,
            proxy_endpoint=endpoint,
            api_key=resolved_api_key,
        )
    raise ValueError(f"Unsupported topo type: {topo_type}")
