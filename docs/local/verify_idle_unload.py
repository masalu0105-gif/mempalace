"""Real-palace acceptance for the HTTP idle unload (plan section 4).

Starts a READ-ONLY test hub on 127.0.0.1:8766 with a 1-minute idle threshold,
queries once, measures the process tree's committed memory, waits past the
threshold, measures again, then queries again and times the lazy reopen.
Safe beside the production hub: loopback needs no token and --read-only takes
no writer lease.

Usage (from C:\\Users\\User\\mempalace):
    .venv\\Scripts\\python.exe docs\\local\\verify_idle_unload.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

import psutil

PORT = 8766
BASE = f"http://127.0.0.1:{PORT}"
PALACE = os.path.expanduser("~/.mempalace/palace")
IDLE_MINUTES = "1"
WAIT_AFTER_IDLE = 100  # seconds; threshold is 60 s, tick every 15 s


def rpc(method, params, rid=1, timeout=120):
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        BASE + "/mcp",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    # transport may answer plain JSON or SSE "data:" lines
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    return json.loads(raw)


def tree_commit_mb(pid):
    total = 0
    biggest = (0, 0)
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return 0, (0, 0)
    for p in procs:
        try:
            mb = p.memory_info().private // (1024 * 1024)
        except psutil.Error:
            continue
        total += mb
        if mb > biggest[1]:
            biggest = (p.pid, mb)
    return total, biggest


def search_once(label):
    t0 = time.monotonic()
    res = rpc("tools/call", {"name": "mempalace_search", "arguments": {"query": "mempalace hub idle unload"}}, rid=3)
    dt = time.monotonic() - t0
    content = res.get("result", {}).get("content", [])
    text = content[0].get("text", "") if content else json.dumps(res)[:300]
    try:
        n = len(json.loads(text).get("results", []))
    except Exception:
        n = -1
    print(f"[{label}] search took {dt:.2f}s, results={n}")
    return dt, n


def main():
    env = dict(os.environ)
    env["MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES"] = IDLE_MINUTES
    env["MEMPALACE_MCP_IDLE_HOURS"] = "0"
    env["PYTHONIOENCODING"] = "utf-8"
    log = open(os.path.join(os.path.dirname(__file__), "verify_idle_unload.stderr.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "mempalace.mcp_server", "--transport", "http", "--host", "127.0.0.1",
         "--port", str(PORT), "--palace", PALACE, "--read-only"],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    try:
        for _ in range(60):
            time.sleep(1)
            try:
                with urllib.request.urlopen(BASE + "/healthz", timeout=2) as r:
                    if r.read().strip() == b"ok":
                        break
            except Exception:
                pass
        else:
            print("FAIL: healthz never ok")
            return 2
        print("hub pid", proc.pid, "healthz ok; commit at start:", tree_commit_mb(proc.pid))
        rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "verify", "version": "0"}})
        search_once("first")
        time.sleep(3)
        loaded = tree_commit_mb(proc.pid)
        print("commit after first search (loaded):", loaded)
        print(f"waiting {WAIT_AFTER_IDLE}s for idle unload ...")
        time.sleep(WAIT_AFTER_IDLE)
        unloaded = tree_commit_mb(proc.pid)
        print("commit after idle:", unloaded)
        dt, n = search_once("reopen")
        time.sleep(2)
        print("commit after reopen:", tree_commit_mb(proc.pid))
        ok_mem = unloaded[1][1] < 1000
        ok_time = dt < 10 and n > 0
        print("GATE unload<1000MB:", "PASS" if ok_mem else "FAIL", "| reopen<10s with results:", "PASS" if ok_time else "FAIL")
        return 0 if (ok_mem and ok_time) else 1
    finally:
        for p in psutil.Process(proc.pid).children(recursive=True):
            try:
                p.kill()
            except psutil.Error:
                pass
        proc.kill()
        log.close()
        with open(log.name, encoding="utf-8", errors="replace") as f:
            lines = [l for l in f if "idle-unload" in l or "listening" in l or "Error" in l]
        print("--- hub log (filtered) ---")
        print("".join(lines[-10:]))


if __name__ == "__main__":
    sys.exit(main())
