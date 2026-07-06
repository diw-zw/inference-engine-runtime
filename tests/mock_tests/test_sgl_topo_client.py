# -*- coding: utf-8 -*-
"""
Tests for the SGLang topology client helpers.
"""

from patio.topo.client.sgl_topo_client import get_sgl_router_endpoint


def test_get_sgl_router_endpoint_uses_generic_router_env(monkeypatch):
    monkeypatch.setenv("GROUP_NAME", "demo")
    monkeypatch.setenv("ROUTER_ROLE_NAME", "router")
    monkeypatch.setenv("ROUTER_PORT", "8000")

    endpoint = get_sgl_router_endpoint({})

    assert endpoint == "demo-router-0.s-demo-router:8000"
