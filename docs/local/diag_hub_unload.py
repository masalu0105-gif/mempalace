import gc, os, sys, time, io
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
try:
    import psutil
    P = psutil.Process()
    def commit(): return P.memory_info().private // (1024*1024)
    def ws(): return P.memory_info().rss // (1024*1024)
except ImportError:
    import ctypes, ctypes.wintypes as wt
    class PMC(ctypes.Structure):
        _fields_=[("cb",wt.DWORD),("PageFaultCount",wt.DWORD),("PeakWorkingSetSize",ctypes.c_size_t),("WorkingSetSize",ctypes.c_size_t),("QuotaPeakPagedPoolUsage",ctypes.c_size_t),("QuotaPagedPoolUsage",ctypes.c_size_t),("QuotaPeakNonPagedPoolUsage",ctypes.c_size_t),("QuotaNonPagedPoolUsage",ctypes.c_size_t),("PagefileUsage",ctypes.c_size_t),("PeakPagefileUsage",ctypes.c_size_t),("PrivateUsage",ctypes.c_size_t)]
    def _pmc():
        m=PMC(); m.cb=ctypes.sizeof(PMC); ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(m), m.cb); return m
    def commit(): return _pmc().PrivateUsage // (1024*1024)
    def ws(): return _pmc().WorkingSetSize // (1024*1024)
def mark(label, t0=None):
    dt = f" {time.time()-t0:5.1f}s" if t0 else "       "
    print(f"[{label:<34}] commit={commit():5d} MB  ws={ws():5d} MB{dt}", flush=True)

PAL = r"C:\Users\User\.mempalace\palace"
mark("0 python start")
import chromadb
mark("1 import chromadb")
t=time.time(); client = chromadb.PersistentClient(path=PAL); mark("2 PersistentClient", t)
t=time.time(); col = client.get_collection("mempalace_drawers"); n=col.count(); mark(f"3 get_collection n={n}", t)
import random; vec=[random.random() for _ in range(384)]
t=time.time(); col.query(query_embeddings=[vec], n_results=3); mark("4 first vector query (HNSW load)", t)
t=time.time(); col.query(query_embeddings=[vec], n_results=3); mark("5 second vector query", t)
# --- unload path A: close client + clear shared system cache + gc
t=time.time()
try: client.close()
except Exception as e: print("close err", e)
try:
    from chromadb.api.client import SharedSystemClient
    SharedSystemClient.clear_system_cache()
except Exception as e: print("clear_system_cache err", e)
del col, client; gc.collect(); time.sleep(2); mark("6 close+clear_system_cache+gc", t)
# --- unload path B: Windows heap / allocator trims
try:
    import ctypes
    ctypes.windll.kernel32.SetProcessWorkingSetSize(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.c_size_t(-1), ctypes.c_size_t(-1))
    try: ctypes.cdll.msvcrt._heapmin()
    except Exception: pass
    mark("7 SetProcessWorkingSetSize")
except Exception as e: print("trim err", e)
# --- embedder
t=time.time()
from mempalace import embedding as E
ef = E.get_embedding_function(); mark("8 get_embedding_function", t)
t=time.time(); ef(["memory test sentence"]); mark("9 first embed (model+DML load)", t)
t=time.time(); ef(["memory test sentence 2"]); mark("10 second embed", t)
t=time.time()
with E._EF_CACHE_LOCK: E._EF_CACHE.clear()
del ef; gc.collect(); time.sleep(2); mark("11 clear _EF_CACHE + gc", t)
# --- reload to show reload cost
t=time.time(); client = chromadb.PersistentClient(path=PAL); col=client.get_collection("mempalace_drawers"); col.query(query_embeddings=[vec], n_results=3); mark("12 reload+query after unload", t)
