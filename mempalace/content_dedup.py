"""Write-time content deduplication for the palace (local fork patch).

Why this exists
---------------
``drawer_id`` is derived from ``(source_file, chunk_index)`` (see
``ids.make_drawer_id_from_chunk``), so the same text appearing in two
different source files always lands on two different ids and Chroma's
``upsert`` never collapses them.  Measured on this palace 2026-09-03:
36,113 extra rows across 11,947 groups of byte-identical documents, 98.8%
of them spanning more than one source file, growing ~1000 rows/day.  The
single biggest contributor is fan-out dispatch: one workflow spawns N
subagents, the identical dispatch prompt is written into N transcripts,
and mining files it N times.

What this module does
---------------------
Before a file's chunks are upserted, drop the chunks whose exact text is
already stored under a *different* source file, and record the skipped
source on the surviving drawer instead.

Two invariants matter and are easy to get wrong:

1. ``palace.file_already_mined`` only accepts a mined file as complete once
   it has seen ``chunk_total`` drawers for the matching mtime group.
   Callers MUST therefore stamp ``chunk_total`` with the number of chunks
   actually written, not ``len(chunks)`` -- otherwise every file holding a
   duplicate chunk looks permanently incomplete and is re-mined forever.
   ``partition_chunks`` returns the kept list precisely so the caller can
   use ``len(kept)``.

2. ``process_file`` deletes a source's existing drawers before re-inserting.
   The lookup below therefore runs *after* that delete, which is what makes
   re-mining safe: the file's own rows are gone, so its content is no longer
   "already present" and gets rewritten normally.

Set ``MEMPALACE_CONTENT_DEDUP=0`` to disable and restore stock behaviour.
"""

import hashlib
import logging
import os

logger = logging.getLogger(__name__)

# Metadata key holding the per-chunk hash.
#
# Deliberately NOT ``content_hash``: the convo miner already uses that key
# for a whole-*conversation* sha256 stamped on chunk_index 0 only, which
# ``palace.prefetch_content_hashes`` reads to detect a conversation
# re-exported under a new filename.  Writing a per-chunk hash into the same
# key would make that check compare a chunk digest against a conversation
# digest and mis-file transcripts.  A separate key keeps both mechanisms
# working and lets projects-mode and convos-mode drawers dedup against each
# other through one shared namespace.
CONTENT_HASH_KEY = "chunk_hash"

# Metadata keys on the surviving drawer recording which other sources held
# the identical chunk that was skipped.
ALSO_IN_KEY = "also_in_sources"
ALSO_IN_COUNT_KEY = "also_in_count"

# Chroma stores metadata in a SQLite TEXT column (no practical length limit;
# the largest existing value in this palace is 285,746 chars), but an
# unbounded path list would still be pointless to carry around. Past this
# many characters only the counter keeps rising.
ALSO_IN_MAX_CHARS = 4000

# Chroma rejects very large $in lists; chunk the lookup.
LOOKUP_BATCH = 200


def is_enabled():
    """Content dedup is on unless explicitly disabled."""
    value = os.environ.get("MEMPALACE_CONTENT_DEDUP", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def compute_content_hash(text):
    """Hash one chunk's text.

    ``strip()`` matches the convo miner's existing whole-conversation hash so
    trailing-whitespace-only differences do not defeat the match.
    """
    return hashlib.sha256((text or "").strip().encode("utf-8", "replace")).hexdigest()


def _pointer_text(source_file, origin):
    """Body for the anchor row of a file that is an exact copy of another.

    Deliberately readable: if this row ever comes back from a search, it
    should explain itself rather than look like a truncated document.
    """
    where = origin or "another file already in the palace"
    return (
        "[mempalace] This file's content is byte-identical to content already "
        "filed from another source, so it was not filed a second time.\n"
        "This file: %s\n"
        "Content lives at: %s" % (source_file, where)
    )


def _lookup_existing(collection, hashes, source_file, wing=None):
    """Map hash -> surviving drawer id, for hashes already stored elsewhere.

    When ``wing`` is given only rows in that wing count: the same content
    mined into a second wing is filed there too, so wing-filtered search
    still finds it (same scope as upstream's file-level content_hash).

    Rows belonging to ``source_file`` itself are ignored: a re-mine deletes
    them first, and counting them would make a file dedup against its own
    previous generation.
    """
    found = {}
    unique = list(dict.fromkeys(hashes))
    for start in range(0, len(unique), LOOKUP_BATCH):
        batch = unique[start : start + LOOKUP_BATCH]
        try:
            where = {CONTENT_HASH_KEY: {"$in": batch}}
            if wing:
                where = {"$and": [where, {"wing": wing}]}
            res = collection.get(
                where=where,
                include=["metadatas"],
            )
        except Exception:
            logger.debug(
                "content-hash lookup failed; keeping this batch", exc_info=True
            )
            continue
        ids = res.get("ids") or []
        metas = res.get("metadatas") or []
        for drawer_id, meta in zip(ids, metas):
            meta = meta or {}
            if meta.get("source_file") == source_file:
                continue
            stored = meta.get(CONTENT_HASH_KEY)
            if stored and stored not in found:
                found[stored] = (drawer_id, meta.get("source_file") or "")
    return found


def partition_chunks(collection, chunks, source_file, content_key="content", wing=None):
    """Split ``chunks`` into (kept, skipped) by exact-content duplication.

    ``skipped`` entries are ``(chunk, surviving_drawer_id)``.  Duplicates
    *within* this file collapse too: the first occurrence is kept and later
    identical chunks are dropped with ``surviving_drawer_id=None`` (the row
    that survives is one of our own, written in this same pass).

    Every chunk gets its hash stamped under ``CONTENT_HASH_KEY`` so the
    caller can carry it into the drawer metadata.

    Never raises: any lookup failure degrades to "keep everything", which is
    exactly the stock behaviour.
    """
    if not chunks or not is_enabled():
        for chunk in chunks or []:
            chunk[CONTENT_HASH_KEY] = compute_content_hash(chunk.get(content_key, ""))
        return list(chunks or []), []

    hashes = [compute_content_hash(c.get(content_key, "")) for c in chunks]
    try:
        existing = _lookup_existing(collection, hashes, source_file, wing=wing)
    except Exception:
        logger.debug("content dedup lookup failed; keeping all chunks", exc_info=True)
        existing = {}

    kept = []
    skipped = []
    seen_here = set()
    origin_of_first = ""
    for chunk, digest in zip(chunks, hashes):
        chunk[CONTENT_HASH_KEY] = digest
        if digest in existing:
            drawer_id, origin = existing[digest]
            if not origin_of_first:
                origin_of_first = origin
            skipped.append((chunk, drawer_id))
            continue
        if digest in seen_here:
            skipped.append((chunk, None))
            continue
        seen_here.add(digest)
        kept.append(chunk)

    if not kept and chunks:
        # Every chunk of this file already exists elsewhere -- an exact copy,
        # e.g. a git worktree checkout or a duplicated vault note.  Filing
        # nothing would leave the source with zero drawers, and
        # ``file_already_mined`` reads "no drawers for this source_file" as
        # "never mined", so the file would be re-mined on every run forever.
        #
        # Keep one anchor row, but replace its text with a pointer rather than
        # re-filing a copy of the duplicated content: an anchor holding the
        # original text would itself be a duplicate row, defeating the purpose.
        # The pointer embeds this file's own path, so its hash is unique and
        # the row states plainly where the content actually lives.
        anchor = skipped.pop(0)[0]
        anchor[content_key] = _pointer_text(source_file, origin_of_first)
        anchor[CONTENT_HASH_KEY] = compute_content_hash(anchor[content_key])
        kept.append(anchor)
    return kept, skipped


def record_skipped_sources(collection, skipped, source_file):
    """Note on each surviving drawer that ``source_file`` also holds its text.

    Best-effort: a failure here loses provenance detail, never content, so it
    must not abort the mine.  Returns the number of drawers updated.
    """
    if not skipped:
        return 0

    counts = {}
    for _chunk, drawer_id in skipped:
        if drawer_id:
            counts[drawer_id] = counts.get(drawer_id, 0) + 1
    if not counts:
        return 0

    try:
        res = collection.get(ids=list(counts), include=["metadatas"])
    except Exception:
        logger.debug("could not read drawers for provenance update", exc_info=True)
        return 0

    ids = res.get("ids") or []
    metas = res.get("metadatas") or []
    new_ids = []
    new_metas = []
    for drawer_id, meta in zip(ids, metas):
        meta = dict(meta or {})
        listed = meta.get(ALSO_IN_KEY) or ""
        parts = [p for p in listed.split("|") if p]
        if source_file not in parts and len(listed) < ALSO_IN_MAX_CHARS:
            parts.append(source_file)
            meta[ALSO_IN_KEY] = "|".join(parts)
        try:
            previous = int(meta.get(ALSO_IN_COUNT_KEY) or 0)
        except (TypeError, ValueError):
            previous = 0
        meta[ALSO_IN_COUNT_KEY] = previous + counts.get(drawer_id, 0)
        new_ids.append(drawer_id)
        new_metas.append(meta)

    if not new_ids:
        return 0
    try:
        # update() rewrites metadata only -- documents and embeddings are
        # untouched, so this does not disturb the HNSW index.
        collection.update(ids=new_ids, metadatas=new_metas)
    except Exception:
        logger.debug("provenance update failed", exc_info=True)
        return 0
    return len(new_ids)
