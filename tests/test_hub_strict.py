"""MEMPALACE_HUB_STRICT: a per-session stdio process never loads the palace itself.

Local fork (2026-11-01). Without the flag, a hub that is down or slow makes
every agent session fall back to a private 4 GB copy of the HNSW index; with a
dozen sessions open that exhausts Windows commit. Strict mode must answer with
a retryable JSON-RPC error instead, never import or call the local server, and
start forwarding again the moment a hub is discoverable.
"""

import urllib.error

import pytest

from mempalace import hub_client, mcp_proxy

REQUEST = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/call",
    "params": {"name": "mempalace_search", "arguments": {"query": "x"}},
}
NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}
FORWARDED = {"jsonrpc": "2.0", "id": 7, "result": {"content": []}}


class _NeverLoad:
    """Stand-in for mcp_proxy._LocalServer that fails the test if touched."""

    def load(self):
        raise AssertionError("strict mode must not load the local server")


def _refused_to_connect(*args, **kwargs):
    raise urllib.error.URLError("connection refused")


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setenv(hub_client.HUB_STRICT_ENV, "1")
    monkeypatch.delenv(hub_client.HUB_FORWARD_ENV, raising=False)


class TestFlag:
    @pytest.mark.parametrize(
        "value,expected",
        [("1", True), ("true", True), ("on", True), ("0", False), ("", False), ("maybe", False)],
    )
    def test_accepted_values(self, monkeypatch, value, expected):
        monkeypatch.setenv(hub_client.HUB_STRICT_ENV, value)
        monkeypatch.delenv(hub_client.HUB_FORWARD_ENV, raising=False)
        assert hub_client.hub_strict() is expected

    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv(hub_client.HUB_STRICT_ENV, raising=False)
        assert hub_client.hub_strict() is False

    def test_forwarding_kill_switch_wins(self, monkeypatch):
        monkeypatch.setenv(hub_client.HUB_STRICT_ENV, "1")
        monkeypatch.setenv(hub_client.HUB_FORWARD_ENV, "0")
        assert hub_client.hub_strict() is False

    def test_refusal_shape(self):
        out = hub_client.strict_refusal(dict(REQUEST), "http://hub", "boom")
        assert out["id"] == 7
        assert out["error"]["code"] == -32000
        assert "strict" in out["error"]["message"]
        assert out["error"]["data"]["hub"] == "http://hub"
        assert hub_client.HUB_STRICT_ENV in out["error"]["data"]["hint"]

    def test_refusal_swallows_notifications(self):
        assert hub_client.strict_refusal(dict(NOTIFICATION), None, "x") is None


class TestThinProxy:
    def test_no_hub_refuses_without_loading(self, strict, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: None)
        out = mcp_proxy._handle(dict(REQUEST), "/p", _NeverLoad())
        assert out["error"]["code"] == -32000
        assert out["error"]["data"]["hub"] is None

    def test_unreachable_hub_refuses_without_loading(self, strict, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: ("http://hub", {}))
        monkeypatch.setattr(mcp_proxy, "_forward", _refused_to_connect)
        out = mcp_proxy._handle(dict(REQUEST), "/p", _NeverLoad())
        assert out["error"]["code"] == -32000
        assert out["error"]["data"]["hub"] == "http://hub"

    def test_notification_without_hub_is_swallowed(self, strict, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: None)
        assert mcp_proxy._handle(dict(NOTIFICATION), "/p", _NeverLoad()) is None

    def test_live_hub_still_forwards(self, strict, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: ("http://hub", {}))
        monkeypatch.setattr(mcp_proxy, "_forward", lambda *a: FORWARDED)
        assert mcp_proxy._handle(dict(REQUEST), "/p", _NeverLoad()) is FORWARDED

    def test_hub_coming_back_heals_without_restart(self, strict, monkeypatch):
        state = {"hub": None}
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: state["hub"])
        monkeypatch.setattr(mcp_proxy, "_forward", lambda *a: FORWARDED)
        local = _NeverLoad()
        assert "error" in mcp_proxy._handle(dict(REQUEST), "/p", local)
        state["hub"] = ("http://hub", {})
        assert mcp_proxy._handle(dict(REQUEST), "/p", local) is FORWARDED

    def test_hub_http_error_still_reported_as_proxy_error(self, strict, monkeypatch):
        monkeypatch.setattr(mcp_proxy, "_hub_target", lambda p: ("http://hub", {}))

        def boom(*a):
            raise urllib.error.HTTPError("http://hub/mcp", 500, "boom", {}, None)

        monkeypatch.setattr(mcp_proxy, "_forward", boom)
        out = mcp_proxy._handle(dict(REQUEST), "/p", _NeverLoad())
        assert out["error"]["code"] == -32000
        assert "proxy failed" in out["error"]["message"]


class TestFullServerStdio:
    """The full server (`python -m mempalace.mcp_server`) honours the same flag."""

    @pytest.fixture
    def never_local(self, monkeypatch):
        from mempalace import mcp_server

        def never(request):
            raise AssertionError("strict mode must not handle the request locally")

        monkeypatch.setattr(mcp_server, "handle_request", never)
        return mcp_server

    def test_no_hub_refuses(self, strict, never_local, monkeypatch):
        monkeypatch.setattr(never_local, "_hub_proxy_target", lambda: None)
        out = never_local._dispatch_stdio_request(dict(REQUEST))
        assert out["error"]["code"] == -32000
        assert "strict" in out["error"]["message"]

    def test_unreachable_hub_refuses(self, strict, never_local, monkeypatch):
        monkeypatch.setattr(never_local, "_hub_proxy_target", lambda: ("http://hub", {}))
        monkeypatch.setattr(never_local, "_forward_request_to_hub", _refused_to_connect)
        out = never_local._dispatch_stdio_request(dict(REQUEST))
        assert out["error"]["code"] == -32000
        assert out["error"]["data"]["hub"] == "http://hub"

    def test_flag_off_keeps_local_fallback(self, monkeypatch):
        from mempalace import mcp_server

        monkeypatch.delenv(hub_client.HUB_STRICT_ENV, raising=False)
        monkeypatch.setattr(mcp_server, "_hub_proxy_target", lambda: None)
        monkeypatch.setattr(mcp_server, "handle_request", lambda request: {"served": "locally"})
        assert mcp_server._dispatch_stdio_request(dict(REQUEST)) == {"served": "locally"}
