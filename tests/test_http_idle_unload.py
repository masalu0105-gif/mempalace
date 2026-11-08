# tests/test_http_idle_unload.py
"""HTTP idle unload: a long-lived hub must hand the vector index back when idle.

Local fork feature (2026-11-08). The hub keeps ~3.8 GB committed while nobody
queries it. ``_http_idle_unload_once`` closes the palace handles after
``MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES`` of silence; the next request reopens
them lazily. These tests pin the guard conditions, not the memory numbers
(those are measured on the real palace in docs/local/2026-11-08-hub-idle-unload-plan.md).
"""

import logging
import threading
import time

import pytest

import mempalace.mcp_server as mcp


@pytest.fixture
def unload_env(monkeypatch):
    """Fresh locks, loaded-looking caches, and a recorder for the close call."""
    calls: list[str] = []

    def fake_close() -> list:
        calls.append("close")
        return []

    monkeypatch.setattr(mcp, "_HTTP_REQUEST_LOCK", mcp._RWLock())
    monkeypatch.setattr(mcp, "_HTTP_MINE_LOCK", threading.Lock())
    monkeypatch.setattr(mcp, "_HTTP_EMBEDDING_LIFECYCLE_LOCK", threading.Lock())
    monkeypatch.setattr(mcp, "_http_idle_unload_mark", -1.0)
    monkeypatch.setattr(mcp, "_close_palace_handles", fake_close)
    monkeypatch.setattr(mcp, "_process_commit_mb", lambda: -1)
    return calls


def test_idle_past_threshold_closes_handles_once(unload_env, monkeypatch):
    """The palace backend keeps its own handle cache, so the hub must unload on
    idle time alone and remember that it did; a second tick is a no-op until a
    new request has been handled."""
    monkeypatch.setattr(mcp, "_last_request_time", time.monotonic() - 100)

    assert mcp._http_idle_unload_once(timeout=10) is True
    assert unload_env == ["close"]
    assert mcp._http_idle_unload_mark == mcp._last_request_time

    assert mcp._http_idle_unload_once(timeout=10) is False
    assert unload_env == ["close"]


def test_new_request_after_unload_arms_the_next_unload(unload_env, monkeypatch):
    monkeypatch.setattr(mcp, "_last_request_time", time.monotonic() - 100)
    assert mcp._http_idle_unload_once(timeout=10) is True

    monkeypatch.setattr(mcp, "_last_request_time", time.monotonic() - 50)  # a request came
    assert mcp._http_idle_unload_once(timeout=10) is True
    assert unload_env == ["close", "close"]


def test_recent_request_keeps_handles(unload_env, monkeypatch):
    monkeypatch.setattr(mcp, "_last_request_time", time.monotonic())

    assert mcp._http_idle_unload_once(timeout=10) is False
    assert unload_env == []


def test_running_mine_blocks_unload_without_waiting(unload_env, monkeypatch):
    monkeypatch.setattr(mcp, "_last_request_time", time.monotonic() - 100)
    mcp._HTTP_MINE_LOCK.acquire()
    try:
        started = time.monotonic()
        assert mcp._http_idle_unload_once(timeout=10) is False
        assert time.monotonic() - started < 1.0, "must try the mine lock, not wait for it"
    finally:
        mcp._HTTP_MINE_LOCK.release()
    assert unload_env == []
    assert not mcp._HTTP_MINE_LOCK.locked(), "mine lock must be left as found"


def test_request_arriving_while_waiting_for_lock_cancels_unload(unload_env, monkeypatch):
    """A reader holds the request lease; it bumps the clock before releasing."""
    monkeypatch.setattr(mcp, "_last_request_time", time.monotonic() - 100)
    reader_in = threading.Event()
    writer_queued = threading.Event()

    def reader():
        with mcp._HTTP_REQUEST_LOCK.read_lock():
            reader_in.set()
            assert writer_queued.wait(3), "unload never queued for the write lease"
            mcp._last_request_time = time.monotonic()  # the request "handled" now

    t = threading.Thread(target=reader)
    t.start()
    assert reader_in.wait(3)

    result = {}

    def unload():
        result["value"] = mcp._http_idle_unload_once(timeout=10)

    u = threading.Thread(target=unload)
    u.start()
    deadline = time.monotonic() + 3
    while mcp._HTTP_REQUEST_LOCK._waiting_writers == 0:
        assert time.monotonic() < deadline, "unload never waited on the request lock"
        time.sleep(0.001)
    writer_queued.set()
    t.join(3)
    u.join(3)

    assert result["value"] is False
    assert unload_env == []


@pytest.mark.parametrize("raw", [None, "", "0", "-5", "abc"])
def test_disabled_env_starts_no_thread(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES", raising=False)
    else:
        monkeypatch.setenv("MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES", raw)

    assert mcp._http_idle_unload_secs() == 0.0
    mcp._start_http_idle_unload_watchdog()
    assert "mcp-http-idle-unload" not in {t.name for t in threading.enumerate()}


def test_env_minutes_parse():
    import os

    os.environ["MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES"] = "20"
    try:
        assert mcp._http_idle_unload_secs() == 1200.0
    finally:
        del os.environ["MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES"]


def test_watchdog_survives_a_failing_unload(monkeypatch, caplog):
    """One raising tick is logged; the loop keeps running and retries."""
    ticks = []
    done = threading.Event()

    def flaky(timeout):
        ticks.append(timeout)
        if len(ticks) == 1:
            raise RuntimeError("close blew up")
        done.set()
        return True

    monkeypatch.setenv("MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES", "0.001")  # 0.06 s
    monkeypatch.setattr(mcp, "_http_idle_unload_once", flaky)
    with caplog.at_level(logging.ERROR, logger=mcp.logger.name):
        mcp._start_http_idle_unload_watchdog()
        assert done.wait(5), "watchdog did not tick again after the exception"
    assert len(ticks) >= 2
    assert any("idle-unload watchdog failed" in r.getMessage() for r in caplog.records)
