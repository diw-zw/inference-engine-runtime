from patio import envs
from patio.registration.adapters.factory import create_router_adapter
from patio.registration.adapters.sglang import SGLangRouterAdapter
from patio.registration.adapters.vllm import VLLMProxyRouterAdapter
from patio.registration.models import MembershipState


class FakeRouterSession:
    def __init__(self, responses: dict[tuple[str, str], object]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict | None]] = []

    def get(
        self,
        url: str,
        headers: dict | None = None,
        timeout: float = 1,
    ):
        self.calls.append(("GET", url, {"headers": headers}))
        return self.responses[("GET", url)]

    def post(self, url: str, json: dict | None = None, headers: dict | None = None, timeout: float = 1):
        self.calls.append(("POST", url, {"json": json, "headers": headers}))
        return self.responses[("POST", url)]

    def delete(self, url: str, timeout: float = 1):
        self.calls.append(("DELETE", url, None))
        return self.responses[("DELETE", url)]


class FakeRouterResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_sgl_router_adapter_add_query_and_remove():
    adapter = SGLangRouterAdapter(
        {"worker_type": "prefill", "port": 30000},
        router_endpoint="http://router",
        worker_endpoint="10.0.0.8:30000",
        timeout=1,
    )
    adapter.session = FakeRouterSession(
        {
            ("POST", "http://router/workers"): FakeRouterResponse(
                202, {"worker_id": "worker-1"}
            ),
            ("GET", "http://router/workers"): FakeRouterResponse(
                200,
                {
                    "workers": [
                        {"url": "http://10.0.0.8:30000", "worker_id": "worker-1"}
                    ]
                },
            ),
            ("DELETE", "http://router/workers/worker-1"): FakeRouterResponse(
                202, {}
            ),
        }
    )

    assert adapter.add() is MembershipState.PRESENT
    assert adapter.query() is MembershipState.PRESENT
    assert adapter.worker_id == "worker-1"
    assert adapter.remove() is MembershipState.ABSENT


def test_vllm_router_adapter_add_query_and_remove():
    adapter = VLLMProxyRouterAdapter(
        {"type": "decode", "instance": "10.0.0.4:8200"},
        proxy_endpoint="http://proxy",
        api_key="secret",
        timeout=1,
    )
    adapter.session = FakeRouterSession(
        {
            ("POST", "http://proxy/instances/add"): FakeRouterResponse(
                200, {"message": "added"}
            ),
            ("GET", "http://proxy/status/instances"): FakeRouterResponse(
                200, {"instances": [{"address": "10.0.0.4:8200"}]}
            ),
            ("POST", "http://proxy/instances/remove"): FakeRouterResponse(
                200, {"message": "removed"}
            ),
        }
    )

    assert adapter.add() is MembershipState.PRESENT
    assert adapter.query() is MembershipState.PRESENT
    assert adapter.remove() is MembershipState.ABSENT

    post_calls = [call for call in adapter.session.calls if call[0] == "POST"]
    assert all(call[2]["headers"] == {"X-API-Key": "secret"} for call in post_calls)


def test_create_router_adapter_rejects_missing_vllm_api_key(monkeypatch):
    monkeypatch.setattr(envs, "VLLM_PROXY_ENDPOINT", "http://proxy")
    monkeypatch.setattr(envs, "VLLM_PROXY_API_KEY", None)

    try:
        create_router_adapter("vLLM", {"type": "decode", "instance": "x:1"})
    except ValueError as exc:
        assert "VLLM_PROXY_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected ValueError for missing vLLM API key")
