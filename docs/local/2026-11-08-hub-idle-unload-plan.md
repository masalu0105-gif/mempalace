# MemPalace hub 閒置自動卸載索引（方案 C）— 可執行計畫書

- 日期：2026-11-08（owner 裁決：A 先做、C 等當天 19:00 額度重置後執行）
- 執行者：下一個 Claude Code 對話（你就是執行的人，不要再往下派）
- 範圍：只動本機 fork `C:\Users\User\mempalace`（分支 main，遠端 `fork` = masalu0105-gif/mempalace）與 `C:\Users\User\start-mempalace-remote.ps1`
- 不在範圍：Claude Code 的 `.claude.json`（方案 A 已於 11/8 16:33 改成 HTTP 直連 hub，備份 `~/.claude/backups/claude.json.bak-20261108-mempalace-http`）

## 0. 一句話

hub（`python -m mempalace.mcp_server --transport http`，pid 會變）平常把整座向量索引留在記憶體裡，佔 3.8 GB commit。
要改成：沒人查詢超過 N 分鐘就把索引與資料庫把手關掉（降到約 0.1–0.5 GB），下一次查詢再自動載回（實測 2.8 秒）。hub 程序本身不退出，監聽的 port 不中斷。

## 1. 11/8 量到的事實（決定設計用）

| 項目 | 數值 | 怎麼量的 |
|---|---|---|
| hub commit（閒置、索引已載入） | 3,817–3,822 MB；WS 只有 144 MB（Windows 已把頁換出） | `Get-CimInstance Win32_Process -Filter 'ProcessId=<hub pid>'` PrivatePageCount |
| 乾淨程序 `get_collection` 一打開 | commit 89 → 2,086 MB（1.8 秒）。索引在開集合時就載入，不是第一次 query | `docs/local/diag_hub_unload.py` 步驟 3 |
| `client.close()`＋`SharedSystemClient.clear_system_cache()`＋`gc.collect()` | commit 2,087 → 93 MB（2.3 秒）。**記憶體真的會還給系統** | 同上步驟 6 |
| embedder（minilm＋DirectML）載入 | +400 MB；`_EF_CACHE.clear()`＋gc 後只掉 4 MB，**清快取不夠** | 同上步驟 9–11 |
| 卸載後重開＋查一次 | 2.8 秒 | 同上步驟 12 |
| 現成工具 `mempalace_reconnect` | 關把手後**立刻重開**集合去數 drawers，所以 hub commit 不變（3822→3817）；它是「重連」不是「卸載」 | 對 hub 打 tools/call 前後量 |
| 索引檔 `data_level0.bin` | 1,146 MB（11/1 重建後 566 MB，一週長一倍；死資料可能又在累積，另案） | `ls .mempalace/palace/<uuid>/` |
| hub 的閒置退出 | 預設 `MEMPALACE_MCP_IDLE_HOURS=8`，啟動腳本沒設 → hub 8 小時沒人用就自己退出（11/7 01:34:58 EXIT code=0 就是這個） | `task-monitor\logs\Users_User_start-mempalace-remote.ps1_8a139a06.log` |
| hub 退出後誰拉回來 | 排程 `MemPalace-Remote-Serve` 直接跑 `start-mempalace-remote.ps1`（**沒有迴圈**）；`Documents\scheduled-task-ops\keep-mempalace-remote.ps1`（有迴圈）沒在跑、log 停在 11/1 17:45；Startup-Watch items.json 卻寫 keeper_script=keep-…。結果：hub 退出後要等 Startup-Watch 下一輪（最久 15 分）才重開（11/7 01:34→01:45 空窗 10 分） | `Get-ScheduledTask`、process 清單、items.json |

結論：
1. 卸載要用「reconnect 的前半段（關閉）」但**不做後半段（重開）**。
2. 卸載後 hub 應該落在 0.1–0.5 GB；若實測 >1 GB，代表 Python/Rust 配置器碎片不吐回，啟用第 6 節的後備方案。
3. 做 C 的同時要把 hub 的 8 小時閒置退出關掉（`MEMPALACE_MCP_IDLE_HOURS=0`），否則「卸載」沒意義，hub 還是會退出並留下 15 分鐘空窗。

## 2. 設計（主案 C1：程序內卸載，port 不中斷）

### 2.1 新環境變數
- `MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES`：浮點數，`0`、缺、亂值 = 關閉（預設關閉，不影響上游行為）。hub 設 `20`。
- 只在 `--transport http` 生效；stdio 已有 strict proxy，不需要。

### 2.2 抽出「關閉把手」函式
檔案 `mempalace/mcp_server/tools_diary.py`：
- 現況：`_tool_reconnect_locked()`（第 407–560 行左右）前半段關閉（第 421–488 行：`backend.close_palace`、`_client_cache.close()`、`_clear_chroma_system_cache()`、清 `_client_cache/_collection_cache/...`、`reset_hnsw_capacity_cache()`、關 `_kg_by_path`、`_logstream_by_path`），後半段 `_refresh_sqlite_integrity_status()` → `_get_collection()` 重開。
- 改法：把第 421–488 行搬成新函式 `_close_palace_handles() -> list[str]`（回傳 close_errors），`_tool_reconnect_locked()` 改成先呼叫它再做原本後半段。行為不變，既有 reconnect 測試要全過。
- 注意 mcp_server 是「fragments exec 進同一個 namespace」的寫法（見 pyproject `[tool.ruff.per-file-ignores]` 註解），新函式用 `global` 宣告同一組變數，照 `_tool_reconnect_locked` 的寫法抄。

### 2.3 新 watchdog
檔案 `mempalace/mcp_server/protocol.py`（放在 `_start_idle_exit_watchdog()` 旁，第 1254 行附近）：

```python
def _http_idle_unload_secs() -> float:
    raw = os.environ.get("MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES", "")
    try:
        return max(0.0, float(raw)) * 60 if raw else 0.0
    except ValueError:
        return 0.0

def _http_idle_unload_once(timeout: float) -> bool:
    """閒置達門檻就關掉 palace 把手。回傳是否真的卸載了。"""
    if _collection_cache is None and _client_cache is None:
        return False                      # 已經是卸載狀態
    if time.monotonic() - _last_request_time < timeout:
        return False
    if not _HTTP_MINE_LOCK.acquire(blocking=False):
        return False                      # mine 在跑，不碰
    try:
        with _http_embedding_lifecycle(), _HTTP_REQUEST_LOCK:   # 鎖順序照 reconnect：mine -> lifecycle -> request(exclusive)
            if time.monotonic() - _last_request_time < timeout:
                return False              # 等鎖期間有人來了
            before = _process_commit_mb()
            errors = _close_palace_handles()
            gc.collect()
            logger.info("idle-unload: palace handles closed after %.0fs idle; commit %d -> %d MB%s",
                        time.monotonic() - _last_request_time, before, _process_commit_mb(),
                        ("; errors: " + "; ".join(errors)) if errors else "")
            return True
    finally:
        _HTTP_MINE_LOCK.release()

def _start_http_idle_unload_watchdog() -> None:
    timeout = _http_idle_unload_secs()
    if timeout <= 0:
        return
    def _loop():
        while True:
            time.sleep(min(60.0, timeout / 4))
            try:
                _http_idle_unload_once(timeout)
            except Exception:
                logger.exception("idle-unload watchdog failed; will retry")
    threading.Thread(target=_loop, name="mcp-http-idle-unload", daemon=True).start()
```

- `_process_commit_mb()`：venv 有 psutil 7.2.2，`psutil.Process().memory_info().private // 2**20`；import 失敗回 -1。
- 在 `mempalace/mcp_server/runtime.py` 的 `_run_http_loop()`（第 387–441 行）`_start_idle_exit_watchdog()` 之後加一行 `_start_http_idle_unload_watchdog()`。
- **不碰 writer lease**：HTTP 模式是啟動時拿的終身 lease（`_acquire_writer_lease_for_http_startup`），不能叫 `_release_mcp_writer_lock()`。
- 下一次請求：`_get_collection()`／`_get_client()`（`_session.py` 第 92–260 行）看到 cache 為 None 會自動重開，不用改。
- `_last_request_time` 只在 `handle_request()`（protocol.py 第 740 行）與 logstream SSE 迴圈更新；`GET /healthz` 不更新，所以 Startup-Watch 每 15 分的健檢不會讓 hub 永遠「忙碌」。

### 2.4 embedder 的 400 MB（第二階段，可選）
- `_EF_CACHE.clear()` 實測無效（507→503 MB）。要釋放得找出 `_MempalaceONNX`（`embedding.py` 第 275–340 行，繼承 chromadb 的 `ONNXMiniLM_L6_V2`）持有的 onnxruntime `InferenceSession` 屬性並顯式 `del`，再 gc；DirectML 的顯存配置是否歸還要另測。
- 先不做，C1 驗收通過後若 owner 要再排。

### 2.5 hub 啟動腳本
`C:\Users\User\start-mempalace-remote.ps1`（改前備份為 `.bak-20261108-idle-unload`），在 `Set-Location` 之後加：

```powershell
$env:MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES = '20'   # 20 分沒人查就卸載索引（方案 C，2026-11-08）
$env:MEMPALACE_MCP_IDLE_HOURS = '0'              # 關掉 8 小時自殺；卸載取代它，避免 15 分鐘空窗
$env:OPENBLAS_NUM_THREADS = '1'
```

### 2.6 順便要 owner 裁決的不一致（不在 C 內自動改）
排程 `MemPalace-Remote-Serve` 應改指 `Documents\scheduled-task-ops\keep-mempalace-remote.ps1`（20 秒重啟迴圈），讓 items.json 的 keeper_loop 邏輯對得上。動排程要登記到 vault `20-專案/排程管理/` 登記簿。

## 3. 測試

新檔 `tests/test_http_idle_unload.py`，寫法仿 `tests/test_idle_watchdog_exit.py`（monkeypatch 模組屬性）與 `tests/test_http_embedding_lifecycle.py`（替換 `_HTTP_REQUEST_LOCK = mcp._RWLock()`、`_HTTP_EMBEDDING_LIFECYCLE_LOCK`）：

1. 閒置超過門檻、cache 非 None → `_close_palace_handles` 被叫一次，回傳 True。
2. cache 已是 None → 不叫、回傳 False（不重複卸載）。
3. `_HTTP_MINE_LOCK` 被別的執行緒持有 → 回傳 False、不叫。
4. 等鎖期間 `_last_request_time` 被更新 → 不卸載（先在另一執行緒持 read lock，釋放前更新時間）。
5. env 缺／`0`／`abc` → `_start_http_idle_unload_watchdog()` 不建執行緒（檢查 `threading.enumerate()` 沒有 `mcp-http-idle-unload`）。
6. `_close_palace_handles` 丟例外 → watchdog 迴圈記 log 不死（下一輪還會跑）。
7. 既有測試全過：`tests/test_hub_reconnect_during_mine.py`、`tests/test_http_embedding_lifecycle.py`、`tests/test_idle_watchdog_exit.py`、`tests/test_mcp_http_transport.py`、`tests/test_hub_strict.py`、`tests/test_mcp_writer_diagnostics.py`。

指令（在 `C:\Users\User\mempalace`）：
```
.\.venv\Scripts\python.exe -m pytest tests\test_http_idle_unload.py tests\test_hub_reconnect_during_mine.py tests\test_http_embedding_lifecycle.py tests\test_idle_watchdog_exit.py tests\test_mcp_http_transport.py tests\test_hub_strict.py tests\test_mcp_writer_diagnostics.py -q
.\.venv\Scripts\python.exe -m ruff check mempalace\mcp_server\protocol.py mempalace\mcp_server\tools_diary.py mempalace\mcp_server\runtime.py tests\test_http_idle_unload.py
.\.venv\Scripts\python.exe -m ruff format --check <同上四檔>
```

## 4. 真機驗收（先用唯讀測試 hub，不碰正式 hub）

loopback 不要 token、`--read-only` 不拿 writer lease，可以和正式 hub 並存：
```
$env:MEMPALACE_HTTP_IDLE_UNLOAD_MINUTES='1'; $env:MEMPALACE_MCP_IDLE_HOURS='0'
.\.venv\Scripts\python.exe -m mempalace.mcp_server --transport http --host 127.0.0.1 --port 8766 --palace C:\Users\User\.mempalace\palace --read-only
```
1. 打一次 `tools/call mempalace_search {"query":"test"}` → 量該 pid commit（期望約 2.5 GB）。
2. 等 ≥ 90 秒 → 再量（**驗收門檻：< 1,000 MB**；乾淨程序實測 93 MB）。stderr 應有一行 `idle-unload: ... commit N -> M MB`。
3. 再打一次 search → 計時（**門檻 < 10 秒**；實測 2.8 秒）→ 結果要有 results。
4. 殺掉測試 hub。

量 commit 的指令：
```
powershell -NoProfile -Command "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=<pid>'; [math]::Round($p.PrivatePageCount/1MB)"
```

## 5. 切到正式 hub 的步驟（會有 < 1 分鐘空窗，期間所有 agent 的 MemPalace 查詢會失敗一次）

0. 前置檢查：commit 剩餘 ≥ 10 GB；`task-monitor\logs\Users_User_start-mempalace-remote.ps1_*.log` 最後 10 分鐘沒有 `mempalace_mine has been inside the palace write path`；`git status` 只剩原本那幾個未追蹤雜檔（`.gitignore.bak-20260929`、`backup_palace_20261013.txt` 等，**不要加進 commit**）。
1. `git checkout -b feat/http-idle-unload`（從 main）。改碼、寫測試、跑第 3 節。
2. 第 4 節真機驗收過。
3. 改 `start-mempalace-remote.ps1`（先備份）。
4. 重啟正式 hub（照 11/1 的經驗）：
   - `Disable-ScheduledTask Startup-Watch`（否則它每 15 分會把 hub 拉回來干擾）
   - `Stop-ScheduledTask MemPalace-Remote-Serve`；用 PID 殺掉 hub 那一串（pythonw run_hidden → powershell → python serve → python mcp_server），**不要用命令列字串比對殺程序**（會殺到自己，10/6、10/20 踩過）
   - 確認 `.mempalace\server\<id>\serverinfo.json` 的 pid 已不在、port 8765 沒人聽
   - `Start-ScheduledTask MemPalace-Remote-Serve`；等 `curl http://100.114.165.34:8765/healthz` 回 ok
   - `Enable-ScheduledTask Startup-Watch`
5. 驗收：等 20 分 → 量 hub commit（門檻 < 1,000 MB）→ 開一個新 Claude 對話（或 `claude -p --model haiku` 叫 `mempalace_status`）→ 回 drawers 數且 < 10 秒。
6. commit 只加改過的檔（`git add mempalace/mcp_server/protocol.py mempalace/mcp_server/tools_diary.py mempalace/mcp_server/runtime.py tests/test_http_idle_unload.py docs/local/`），merge 回 main，`git push -u fork main`，`git rev-parse HEAD fork/main` 要一致。
7. 寫記憶：`~/.claude/projects/C--/memory/project_mempalace_hub_idle_unload_20261108.md` 更新狀態；排程若有動，登記 vault 排程登記簿。

回滾：還原 `start-mempalace-remote.ps1.bak-20261108-idle-unload`、`git checkout main`、重做第 4 步。

## 6. 後備方案 C2（C1 卸載後 commit 仍 > 1 GB 才用）

hub 改成閒置就整個退出、由迴圈 20 秒內重啟：`MEMPALACE_MCP_IDLE_HOURS=0.5`（接受小數，見 `_guards.py` `_mcp_idle_timeout_secs`）＋排程改跑 `keep-mempalace-remote.ps1`。
- 好處：程序退出保證記憶體歸零，不管配置器碎片。
- 代價：每次重啟要重拿 writer lease、重寫 serverinfo；空窗期間 Claude Code 的 HTTP 連線會重試 5 次（1 秒起倍增，約 31 秒內），Codex/Roti/Rojak 的 stdio proxy 打到空窗會回錯誤一次。

## 7. 風險

- 卸載時拿 exclusive request lock 約 2–3 秒，剛好來的查詢會等這幾秒。
- `_clear_chroma_system_cache()` 是 process-global（chroma.py 第 3141 行註解明寫「never on steady-state hot path」）；我們在閒置時叫、每次卸載一次。#2002 相關的 reconnect 測試要全過才算安全。
- 配置器碎片：hub 跑過 mine 後能不能回到 ~100 MB 未知，所以第 4、5 節的門檻設 1,000 MB，不達就走 C2。
- 這份 fork 的 main 領先上游很多本地 commit，merge 上游時 `protocol.py`／`tools_diary.py` 可能衝突；新碼集中成獨立函式、少改既有行，降低衝突面。

## 8. 時程估計
改碼＋測試 1.5–2 小時；真機驗收 40 分（含等閒置）；切正式 hub 10 分。

## 9. 執行紀錄（2026-11-08 20:50–21:30，執行者：Claude Code 新對話）

- 開工時發現 hub 沒在跑：機器 20:39 重開，排程 20:39:34 啟動時 Tailscale IP 還沒就緒，`WinError 10049` 綁位址失敗但退出碼 0；Startup-Watch 開機 8 分鐘內跳過，20:47 才拉回。啟動腳本已加「先等 IP 出現（最多 120 秒）」。
- 程式：`_close_palace_handles()` 抽出、`_http_idle_unload_once()`／`_start_http_idle_unload_watchdog()` 加進 protocol.py、runtime 掛鉤；commit `b1ac305c`，merge `fcf44151`，push fork main 一致。
- 與第 2.3 節設計不同的一點：**不能用 `_collection_cache is None` 判斷「已卸載」**。實測 hub 載了 3 GB 索引時 `_collection_cache` 仍是 None（把手在 palace backend 自己的快取裡），watchdog 因此每輪都跳過。改成記「上次卸載時的 `_last_request_time`」，相同就表示沒有新請求、不重複卸載。
- 測試：`tests/test_http_idle_unload.py` 8 條＋計畫書列的 6 個既有測試檔，共 122 passed；ruff check／format 全過。
- 唯讀測試 hub（8766，門檻 1 分）：載入 3,132 MB → 卸載後 1,112 MB → 重開搜尋 2.62 秒有結果。門檻 1,000 MB 差 112 MB；該程序一啟動就 707 MB（套件＋embedder），索引那 2.4 GB 還回 2.0 GB。
- 正式 hub（8765，門檻 20 分）：21:07:47 搜尋後 2,498 MB → 21:28 卸載 2,492 → 499 MB（log：`idle-unload: palace handles closed after 1259s idle`）→ 21:30 新 `claude -p` 搜尋正常回結果 → 2,501 MB。**門檻 <1,000 MB 通過**。正式 hub 啟動基線只有 106 MB（測試 hub 的 707 MB 差在啟動方式，沒深究）。
- owner 裁決（21:20）：不走 C2 後備方案，C1 定案。
- 未做：第 2.4 節 embedder 400 MB；第 2.6 節排程改指 keep 迴圈仍待裁決；`data_level0.bin` 長大另案。
- 回滾方式不變：還原 `start-mempalace-remote.ps1.bak-20261108-idle-unload`、`git revert fcf44151`、照第 5 節第 4 步重啟。
