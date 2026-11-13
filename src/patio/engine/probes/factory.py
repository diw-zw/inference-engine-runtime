import os

from patio import envs
from patio.engine.probes.base import EngineProbe
from patio.engine.probes.sglang import SGLangEngineProbe
from patio.engine.probes.vllm import VLLMEngineProbe


def create_engine_probe(
    worker_info: dict,
    engine: str | None = None,
    endpoint: str | None = None,
) -> EngineProbe:
    engine_name = engine or envs.INFERENCE_ENGINE
    probe_endpoint = endpoint or envs.INFERENCE_ENGINE_ENDPOINT
    if probe_endpoint is None:
        default_port = "30000" if engine_name.lower() == "sglang" else "8000"
        port = worker_info.get("port", default_port)
        host = os.getenv("POD_IP", "localhost")
        probe_endpoint = f"http://{host}:{port}"

    if engine_name.lower() == "sglang":
        return SGLangEngineProbe(worker_info, probe_endpoint)
    if engine_name.lower() == "vllm":
        return VLLMEngineProbe(worker_info, probe_endpoint)
    raise ValueError(f"Unsupported inference engine: {engine_name}")
