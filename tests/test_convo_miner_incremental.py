# tests/test_convo_miner_incremental.py
"""Incremental re-mine of a changed transcript (local fork, 2026-11-08).

A Claude Code transcript only grows. Stock behaviour deletes and re-embeds
every chunk of the file whenever its mtime moves; on the 2026-11-08 palace
that was 73k chunks a night, each leaving a dead HNSW slot (41% of the index).
With ``MEMPALACE_CONVO_INCREMENTAL`` on (default), a chunk whose drawer id
(file + mode + chunk_index) still holds the same ``chunk_hash`` keeps its row
and vector and only gets fresh bookkeeping metadata.
"""

import os
import shutil
import tempfile
import time
from pathlib import Path

import chromadb
import pytest

from mempalace.content_dedup import CONTENT_HASH_KEY
from mempalace.convo_miner import _plan_incremental_writes, mine_convos

BASE = (
    "> What is the plan?\nStart with the schema, then the API. FIRST_MARKER\n\n"
    "> Any risks?\nMigration ordering is the main one. SECOND_MARKER\n"
)
GROWN = BASE + "\n> One more exchange?\nYes, resolved by locking the order. THIRD_MARKER\n"


def _bump_mtime(path, seconds=60):
    t = time.time() + seconds
    os.utime(path, (t, t))


def _rows(palace_path, source_file):
    client = chromadb.PersistentClient(path=palace_path)
    col = client.get_collection("mempalace_drawers")
    got = col.get(where={"source_file": source_file}, include=["documents", "metadatas"])
    rows = {
        rid: (doc, meta) for rid, doc, meta in zip(got["ids"], got["documents"], got["metadatas"])
    }
    del col, client
    return rows


@pytest.fixture
def workdir():
    tmpdir = tempfile.mkdtemp()
    try:
        yield tmpdir
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_grown_file_keeps_unchanged_chunks_and_writes_only_the_tail(workdir, capsys, monkeypatch):
    monkeypatch.delenv("MEMPALACE_CONVO_INCREMENTAL", raising=False)
    src = Path(workdir) / "src"
    src.mkdir()
    convo = src / "session.txt"
    convo.write_text(BASE)
    palace = os.path.join(workdir, "palace")
    resolved = str(convo.resolve())

    mine_convos(str(src), palace, wing="test")
    capsys.readouterr()
    before = _rows(palace, resolved)
    assert len(before) >= 2
    first_filed_at = {rid: meta["filed_at"] for rid, (_, meta) in before.items()}

    convo.write_text(GROWN)
    _bump_mtime(convo)
    new_mtime = os.path.getmtime(convo)
    mine_convos(str(src), palace, wing="test")
    out = capsys.readouterr().out
    after = _rows(palace, resolved)

    # Every original chunk survived under its original id and document.
    for rid, (doc, _) in before.items():
        assert rid in after, "unchanged chunk was deleted and rewritten"
        assert after[rid][0] == doc
    new_ids = set(after) - set(before)
    assert new_ids, "the appended exchange was not filed"
    assert all("THIRD_MARKER" in after[rid][0] for rid in new_ids)
    # Only the tail counted as filed; kept rows keep their first ingest stamp.
    assert f"Drawers filed: {len(new_ids)}" in out
    for rid in before:
        assert after[rid][1]["filed_at"] == first_filed_at[rid]
    # But kept rows carry the new pass's bookkeeping so the next mine skips the file.
    for rid in after:
        assert abs(after[rid][1]["source_mtime"] - new_mtime) < 0.001
        assert after[rid][1]["chunk_total"] == len(after)

    mine_convos(str(src), palace, wing="test")
    assert "Files skipped (already filed): 1" in capsys.readouterr().out


def test_incremental_off_rewrites_every_chunk(workdir, capsys, monkeypatch):
    monkeypatch.setenv("MEMPALACE_CONVO_INCREMENTAL", "0")
    src = Path(workdir) / "src"
    src.mkdir()
    convo = src / "session.txt"
    convo.write_text(BASE)
    palace = os.path.join(workdir, "palace")
    resolved = str(convo.resolve())

    mine_convos(str(src), palace, wing="test")
    capsys.readouterr()
    before = _rows(palace, resolved)

    convo.write_text(GROWN)
    _bump_mtime(convo)
    mine_convos(str(src), palace, wing="test")
    out = capsys.readouterr().out
    after = _rows(palace, resolved)

    assert f"Drawers filed: {len(after)}" in out, "stock path must count every chunk"
    # Stock behaviour purges and re-stamps every row (ids may change too: the
    # room is classified from the whole file and can flip as it grows).
    first_stamp = max(meta["filed_at"] for _, meta in before.values())
    assert all(meta["filed_at"] > first_stamp for _, meta in after.values())


def test_edit_in_the_middle_rewrites_only_changed_chunks(workdir, capsys, monkeypatch):
    monkeypatch.delenv("MEMPALACE_CONVO_INCREMENTAL", raising=False)
    src = Path(workdir) / "src"
    src.mkdir()
    convo = src / "session.txt"
    convo.write_text(GROWN)
    palace = os.path.join(workdir, "palace")
    resolved = str(convo.resolve())

    mine_convos(str(src), palace, wing="test")
    capsys.readouterr()
    before = _rows(palace, resolved)
    changed_before = {rid for rid, (doc, _) in before.items() if "SECOND_MARKER" in doc}
    assert changed_before

    convo.write_text(GROWN.replace("SECOND_MARKER", "SECOND_MARKER_EDITED"))
    _bump_mtime(convo)
    mine_convos(str(src), palace, wing="test")
    out = capsys.readouterr().out
    after = _rows(palace, resolved)

    assert set(after) == set(before), "same chunk positions, same ids"
    assert f"Drawers filed: {len(changed_before)}" in out
    for rid in before:
        if rid in changed_before:
            assert "SECOND_MARKER_EDITED" in after[rid][0]
            assert after[rid][1]["filed_at"] != before[rid][1]["filed_at"]
        else:
            assert after[rid][0] == before[rid][0]
            assert after[rid][1]["filed_at"] == before[rid][1]["filed_at"]


def test_legacy_row_without_digest_is_rewritten(workdir, capsys, monkeypatch):
    monkeypatch.delenv("MEMPALACE_CONVO_INCREMENTAL", raising=False)
    src = Path(workdir) / "src"
    src.mkdir()
    convo = src / "session.txt"
    convo.write_text(BASE)
    palace = os.path.join(workdir, "palace")
    resolved = str(convo.resolve())

    mine_convos(str(src), palace, wing="test")
    capsys.readouterr()
    before = _rows(palace, resolved)

    # Strip the digest from one row: a pre-TASK-0091 drawer.
    victim = sorted(before)[0]
    client = chromadb.PersistentClient(path=palace)
    col = client.get_collection("mempalace_drawers")
    meta = dict(before[victim][1])
    meta[CONTENT_HASH_KEY] = None  # chroma: None deletes the key
    col.update(ids=[victim], metadatas=[meta])
    del col, client

    convo.write_text(GROWN)
    _bump_mtime(convo)
    mine_convos(str(src), palace, wing="test")
    out = capsys.readouterr().out
    after = _rows(palace, resolved)

    first_stamp = max(meta["filed_at"] for _, meta in before.values())
    rewritten = {rid for rid, (_, meta) in after.items() if meta["filed_at"] > first_stamp}
    kept = set(after) - rewritten
    # The legacy row and the appended tail were written; the other row was kept.
    assert f"Drawers filed: {len(rewritten)}" in out
    assert len(rewritten) == 2 and len(kept) == 1
    assert victim not in kept, "a row without a digest must never be reused"
    victim_doc = before[victim][0]
    victim_now = [meta for doc, meta in after.values() if doc == victim_doc]
    assert len(victim_now) == 1 and victim_now[0][CONTENT_HASH_KEY]


def test_plan_incremental_writes_pure():
    planned = [
        ("a", "doc a", {CONTENT_HASH_KEY: "ha", "filed_at": "new"}),
        ("b", "doc b", {CONTENT_HASH_KEY: "hb", "filed_at": "new"}),
        ("c", "doc c", {CONTENT_HASH_KEY: "hc", "filed_at": "new"}),
    ]
    # Stored: "a" unchanged (same digest, same id); "b" edited (digest differs);
    # "z" was removed from the file.
    existing = {
        "by_digest": {"ha": [("a", "old")], "stale": [("b", "old")], "hz": [("z", "old")]},
        "ids": ["a", "b", "z"],
    }
    reuse, to_write, stale = _plan_incremental_writes(planned, existing)
    assert [rid for rid, _ in reuse] == ["a"]
    assert reuse[0][1]["filed_at"] == "old"
    assert [rid for rid, _, _ in to_write] == ["b", "c"]
    assert sorted(stale) == ["b", "z"]

    # Same content moved: digest "hb" used to live under id "old_b"; it keeps
    # that id and only gets the new metadata.
    existing = {"by_digest": {"hb": [("old_b", "old")]}, "ids": ["old_b"]}
    reuse, to_write, stale = _plan_incremental_writes(planned, existing)
    assert reuse == [("old_b", {CONTENT_HASH_KEY: "hb", "filed_at": "old"})]
    assert [rid for rid, _, _ in to_write] == ["a", "c"]
    assert stale == []

    # Collision guard: a chunk to write now sits at the position whose id a
    # reused row owns -> the reuse is dropped and both are written.
    existing = {"by_digest": {"hb": [("a", "old")]}, "ids": ["a"]}
    reuse, to_write, stale = _plan_incremental_writes(planned, existing)
    assert reuse == []
    assert sorted(rid for rid, _, _ in to_write) == ["a", "b", "c"]
    assert stale == ["a"]

    reuse, to_write, stale = _plan_incremental_writes(planned, {})
    assert reuse == [] and len(to_write) == 3 and stale == []
