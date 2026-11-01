"""Small authenticated client for forwarding JSON-RPC to a live Palace hub."""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Mapping

logger = logging.getLogger(__name__)

HUB_FORWARD_ENV = "MEMPALACE_HUB_FORWARD"
HUB_PROXY_TIMEOUT_S = 600.0
HUB_STRICT_ENV = "MEMPALACE_HUB_STRICT"


def hub_strict() -> bool:
    """True when a per-session stdio process must never serve the palace itself.

    Local fork (2026-11-01). One stdio process per agent session is cheap while
    every one of them proxies to the hub; the moment the hub is slow or down,
    each falls back to loading the whole HNSW index privately (4 GB per process
    on this palace) and never gives it back. With a dozen agent sessions open
    that exhausts Windows commit and takes every MCP server down with it.
    Strict mode trades that for a loud, retryable JSON-RPC error. The hub is
    re-discovered on every request, so a session heals itself as soon as the
    hub is back, without a restart. The forwarding kill switch wins: with
    ``MEMPALACE_HUB_FORWARD=0`` there is no hub to be strict about.
    """
    if _forwarding_disabled():
        return False
    return os.environ.get(HUB_STRICT_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def strict_refusal(request: Mapping, hub: str | None, reason: str):
    """JSON-RPC error for a request strict mode refuses to serve locally.

    ``None`` for notifications, which have no id to answer.
    """
    if request.get("id") is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {
            "code": -32000,
            "message": (
                f"palace hub unavailable ({reason}); "
                "strict mode refuses to load the palace in this process"
            ),
            "data": {
                "hub": hub,
                "hint": (
                    f"{HUB_STRICT_ENV}=1 forbids this session from opening the palace itself. "
                    "Start or wait for the MemPalace hub (mempalace serve) and retry: the hub "
                    f"is re-checked on every request. Set {HUB_STRICT_ENV}=0 to allow the "
                    "local fallback (loads the whole index into this process)."
                ),
            },
        },
    }


def _forwarding_disabled() -> bool:
    return os.environ.get(HUB_FORWARD_ENV, "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }


def discover_hub(palace_path: str | None) -> tuple[str, dict[str, str]] | None:
    """Return the authenticated endpoint for another live hub, if available."""

    if _forwarding_disabled() or not palace_path:
        return None
    try:
        from . import server_registry

        info = server_registry.read_live_serverinfo(palace_path)
        if not info or info.get("pid") == os.getpid():
            return None
        base_url = server_registry.client_base_url(info)
        headers = {"Content-Type": "application/json"}
        token = server_registry.load_server_token(palace_path)
    except Exception:
        logger.debug("hub discovery failed", exc_info=True)
        return None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return base_url, headers


def forward_json_rpc(
    base_url: str,
    headers: Mapping[str, str],
    request: Mapping,
    *,
    timeout: float = HUB_PROXY_TIMEOUT_S,
):
    """POST one JSON-RPC request to the hub; return ``None`` for an empty body."""

    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    http_request = urllib.request.Request(f"{base_url}/mcp", data=body, headers=dict(headers))
    with urllib.request.urlopen(http_request, timeout=timeout) as response:
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))
