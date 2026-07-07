# -*- coding: utf-8 -*-
"""
Tests for the SGLang topology client helpers.
"""
from importlib import reload

from patio import envs
from patio.topo.client.sgl_topo_client import get_sgl_router_endpoint


def test_get_sgl_router_endpoint_uses_generic_router_env(monkeypatch):
    monkeypatch.setenv("GROUP_NAME", "demo")
    monkeypatch.setenv("ROUTER_ROLE_NAME", "router")
    monkeypatch.setenv("ROUTER_PORT", "8000")
    reload(envs)

    endpoint = get_sgl_router_endpoint({})

    assert endpoint == "demo-router-0.s-demo-router:8000"


def test_get_sgl_router_endpoint_uses_legacy_sgl_router_env(monkeypatch):
    monkeypatch.setenv("GROUP_NAME", "demo")
    monkeypatch.delenv("ROUTER_ROLE_NAME", raising=False)
    monkeypatch.delenv("ROUTER_PORT", raising=False)
    monkeypatch.setenv("SGL_ROUTER_ROLE_NAME", "legacy-router")
    monkeypatch.setenv("SGL_ROUTER_PORT", "8001")
    reload(envs)

    endpoint = get_sgl_router_endpoint({})

    assert endpoint == "demo-legacy-router-0.s-demo-legacy-router:8001"


def test_get_sgl_router_endpoint_prefers_generic_router_env(monkeypatch):
    monkeypatch.setenv("GROUP_NAME", "demo")
    monkeypatch.setenv("ROUTER_ROLE_NAME", "router")
    monkeypatch.setenv("ROUTER_PORT", "8000")
    monkeypatch.setenv("SGL_ROUTER_ROLE_NAME", "legacy-router")
    monkeypatch.setenv("SGL_ROUTER_PORT", "8001")
    reload(envs)

    endpoint = get_sgl_router_endpoint({})

    assert endpoint == "demo-router-0.s-demo-router:8000"
