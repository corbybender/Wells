"""Semantic (embedding-based) code retrieval.

Sits alongside the structural indexer in ``wells-index``: every symbol the
Rust engine extracts into ``.wells_index/index.db`` is also embedded with a
local ONNX model (``BAAI/bge-small-en-v1.5``, 384-dim) and its vector is
stored in a ``sqlite-vec`` virtual table inside the same SQLite file.

This module degrades to a no-op when either ``fastembed`` or ``sqlite-vec``
is not installed — callers should check :data:`EMBED_AVAILABLE` (or just call
the functions, which return empty results on failure).

Schema added to ``index.db``::

    CREATE VIRTUAL TABLE vec_symbols USING vec0(embedding FLOAT[384]);
    -- rowid of vec_symbols == symbols.id
    CREATE TABLE vec_symbol_meta (
        symbol_id   INTEGER PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
        source_text TEXT    NOT NULL,
        embedded_at INTEGER NOT NULL
    );

Public surface:

* :data:`EMBED_AVAILABLE`  — whether both libs import.
* :func:`index_db_path`    — path to ``<workspace>/.wells_index/index.db``.
* :func:`ensure_embedded`  — embed any symbols not yet embedded (idempotent).
* :func:`embed_query`      — embed a natural-language query (BGE-prefixed).
* :func:`semantic_search`  — top-k cosine ranking over symbol vectors.
* :func:`file_aggregate_embedding` / :func:`file_cosine` — for repomap re-rank.
"""

from __future__ import annotations

import json
import struct
import threading
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Availability + constants
# --------------------------------------------------------------------------- #


def _check_available() -> bool:
    try:
        import fastembed  # noqa: F401
        import sqlite_vec  # noqa: F401
        return True
    except Exception:
        return False


EMBED_AVAILABLE: bool = _check_available()

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
_VEC_DIM = 384
# BGE retrieval query prefix (per the BGE paper) — used only for queries,
# never for document/symbol text.
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Lazily-created singletons.
_model: Any = None
_model_lock = threading.Lock()


def index_db_path(workspace: str | Path) -> Path:
    """Return ``<workspace>/.wells_index/index.db``."""
    return Path(workspace) / ".wells_index" / "index.db"


# --------------------------------------------------------------------------- #
# Low-level sqlite-vec helpers
# --------------------------------------------------------------------------- #


def _open_db(workspace: str | Path):
    """Open index.db with the sqlite-vec extension loaded. Returns the conn
    or ``None`` if unavailable / the db does not exist."""
    if not EMBED_AVAILABLE:
        return None
    db = index_db_path(workspace)
    if not db.exists():
        return None
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        conn.enable_load_extension(True)
        import sqlite_vec
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        conn.close()
        return None
    return conn


def _ensure_schema(conn) -> None:
    # If a previous version of this module created vec_symbols with the
    # default L2 metric, drop it so we can recreate with cosine. Detection
    # is by inspecting the original CREATE statement (cheap, one row).
    try:
        create_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_symbols';"
        ).fetchone()
        if create_sql and "cosine" not in (create_sql[0] or "").lower():
            conn.execute("DROP TABLE IF EXISTS vec_symbols;")
            conn.execute("DELETE FROM vec_symbol_meta;")
    except Exception:
        pass

    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_symbols "
        f"USING vec0(embedding FLOAT[{_VEC_DIM}] distance_metric=cosine);"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS vec_symbol_meta ("
        "    symbol_id   INTEGER PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,"
        "    source_text TEXT    NOT NULL,"
        "    embedded_at INTEGER NOT NULL"
        ");"
    )
    conn.commit()


def _serialize(vec: list[float]) -> bytes:
    """Pack a float vector as little-endian bytes (sqlite-vec's expected form)."""
    return struct.pack(f"<{len(vec)}f", *vec)


# --------------------------------------------------------------------------- #
# Model + text representation
# --------------------------------------------------------------------------- #


def _get_model():
    """Return a cached fastembed TextEmbedding singleton."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from fastembed import TextEmbedding
            _model = TextEmbedding(model_name=_MODEL_NAME)
    return _model


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns one 384-dim vector per input."""
    if not texts:
        return []
    model = _get_model()
    out: list[list[float]] = []
    for v in model.embed(texts):
        out.append([float(x) for x in v])
    return out


def _symbol_text(name: str, kind: str, file_path: str) -> str:
    """The string we embed for one symbol.

    The name dominates, but the kind and file context help disambiguate
    common names (e.g. ``init`` appears in every Python package).
    """
    kind_label = (kind or "symbol").replace("_", " ")
    # Strip extension and OS separators for a tidier token stream.
    rel = file_path.replace("\\", "/").rsplit(".", 1)[0]
    return f"{kind_label} {name} in {rel}"


# --------------------------------------------------------------------------- #
# Public: embedding the corpus
# --------------------------------------------------------------------------- #


def ensure_embedded(workspace: str | Path, *, max_per_run: int = 5000) -> dict:
    """Embed every indexed symbol that doesn't yet have a vector.

    Idempotent: rows already in ``vec_symbol_meta`` are skipped. Returns a
    dict with counts: ``{"embedded": int, "skipped": int, "total": int,
    "error": str | None}``.

    Symbol vectors become orphans when the structural index is rebuilt (the
    underlying ``symbols.id`` values change); orphans are harmless because
    ``semantic_search`` joins on ``symbols.id``, but they are pruned here
    opportunistically to keep the table small.
    """
    result = {"embedded": 0, "skipped": 0, "total": 0, "error": None}
    conn = _open_db(workspace)
    if conn is None:
        result["error"] = "embeddings-unavailable" if not EMBED_AVAILABLE else "no-index-db"
        return result
    try:
        _ensure_schema(conn)

        # Symbols without an embedding yet, joined to file path + name.
        rows = conn.execute(
            "SELECT s.id, n.name, s.kind, f.path, s.start_line, s.end_line "
            "FROM symbols s "
            "JOIN names   n ON n.id = s.name_id "
            "JOIN files   f ON f.id = s.file_id "
            "LEFT JOIN vec_symbol_meta m ON m.symbol_id = s.id "
            "WHERE m.symbol_id IS NULL "
            "ORDER BY s.id "
            "LIMIT ?;",
            [max_per_run],
        ).fetchall()

        pending = list(rows)
        result["total"] = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        if not pending:
            return result

        # Map integer kind code → label. The kinds are defined in
        # wells-index/src/graph.rs:SymbolKind. Keep the labels short and
        # descriptive so the model can use them.
        kind_labels = {
            0: "module", 1: "class", 2: "function", 3: "method",
            4: "variable", 5: "constant", 6: "struct", 7: "trait",
            8: "interface", 9: "enum",
        }

        texts = [
            _symbol_text(
                name=row[1],
                kind=kind_labels.get(row[2], "symbol"),
                file_path=row[3],
            )
            for row in pending
        ]
        vectors = _embed_texts(texts)
        if len(vectors) != len(pending):
            result["error"] = "embedding-count-mismatch"
            return result

        import time
        now = int(time.time())
        conn.executemany(
            "INSERT OR REPLACE INTO vec_symbols(rowid, embedding) VALUES (?, ?);",
            [(row[0], _serialize(vec)) for row, vec in zip(pending, vectors)],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO vec_symbol_meta(symbol_id, source_text, embedded_at) "
            "VALUES (?, ?, ?);",
            [(row[0], text, now) for row, text in zip(pending, texts)],
        )
        conn.commit()
        result["embedded"] = len(pending)
        result["skipped"] = max(0, result["total"] - len(pending))

        _prune_orphans(conn)
        return result
    except Exception as e:
        conn.rollback()
        result["error"] = f"{type(e).__name__}: {e}"
        return result
    finally:
        conn.close()


def _prune_orphans(conn) -> int:
    """Delete vec rows whose symbol_id no longer exists in ``symbols``."""
    cur = conn.execute(
        "DELETE FROM vec_symbols WHERE rowid NOT IN (SELECT id FROM symbols);"
    )
    n = cur.rowcount or 0
    conn.execute(
        "DELETE FROM vec_symbol_meta WHERE symbol_id NOT IN (SELECT id FROM symbols);"
    )
    conn.commit()
    return n


# --------------------------------------------------------------------------- #
# Public: querying
# --------------------------------------------------------------------------- #


def embed_query(query: str) -> list[float] | None:
    """Embed a natural-language query with the BGE retrieval prefix."""
    if not EMBED_AVAILABLE:
        return None
    try:
        out = _embed_texts([_QUERY_PREFIX + (query or "").strip()])
        return out[0] if out else None
    except Exception:
        return None


def semantic_search(
    workspace: str | Path,
    query: str,
    *,
    limit: int = 10,
    auto_embed: bool = True,
) -> list[dict]:
    """Return the top-``limit`` symbols whose embeddings match ``query``.

    Each result dict has: ``file_path``, ``name``, ``kind``, ``start_line``,
    ``end_line``, ``score`` (cosine *similarity* in ``[0, 1]`` — higher is
    better), and ``source_text`` (the text that was embedded).

    Returns an empty list when embeddings are unavailable, the corpus has
    not been embedded, or the query is empty.
    """
    if not EMBED_AVAILABLE or not (query or "").strip():
        return []

    conn = _open_db(workspace)
    if conn is None:
        return []
    try:
        _ensure_schema(conn)

        # Auto-embed on first use.
        total = conn.execute("SELECT COUNT(*) FROM vec_symbols").fetchone()[0]
        if auto_embed and total == 0:
            conn.close()
            stats = ensure_embedded(workspace)
            if stats.get("error"):
                return []
            conn = _open_db(workspace)
            if conn is None:
                return []
            _ensure_schema(conn)

        qvec = embed_query(query)
        if qvec is None:
            return []

        rows = conn.execute(
            "SELECT v.rowid, v.distance "
            "FROM vec_symbols v "
            "WHERE v.embedding MATCH ? AND k = ? "
            "ORDER BY v.distance;",
            (_serialize(qvec), max(1, int(limit))),
        ).fetchall()

        if not rows:
            return []

        kind_labels = {
            0: "module", 1: "class", 2: "function", 3: "method",
            4: "variable", 5: "constant", 6: "struct", 7: "trait",
            8: "interface", 9: "enum",
        }

        sym_ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(sym_ids))
        meta_rows = conn.execute(
            f"SELECT s.id, n.name, s.kind, f.path, s.start_line, s.end_line, m.source_text "
            f"FROM symbols s "
            f"JOIN names n ON n.id = s.name_id "
            f"JOIN files f ON f.id = s.file_id "
            f"JOIN vec_symbol_meta m ON m.symbol_id = s.id "
            f"WHERE s.id IN ({placeholders});",
            sym_ids,
        ).fetchall()
        meta = {r[0]: r for r in meta_rows}

        out: list[dict] = []
        for sym_id, distance in rows:
            m = meta.get(sym_id)
            if m is None:
                continue  # orphan vector with no live symbol; skip
            # sqlite-vec returns cosine *distance* in [0, 2]; similarity = 1 - distance.
            sim = max(0.0, 1.0 - float(distance))
            out.append({
                "file_path": m[3],
                "name": m[1],
                "kind": kind_labels.get(m[2], "symbol"),
                "start_line": m[4],
                "end_line": m[5],
                "score": round(sim, 4),
                "source_text": m[6],
            })
        out.sort(key=lambda d: -d["score"])
        return out[:limit]
    except Exception:
        return []
    finally:
        if conn is not None:
            conn.close()


# --------------------------------------------------------------------------- #
# Public: file-level aggregate (used by repomap re-rank)
# --------------------------------------------------------------------------- #


_FILE_VEC_CACHE: dict[tuple[str, str], list[float] | None] = {}
_FILE_VEC_LOCK = threading.Lock()


def _all_symbol_vectors(conn) -> dict[int, list[float]]:
    """Return {symbol_id: vector} for everything currently embedded.

    Cached per-process; cleared when the index is rebuilt.
    """
    rows = conn.execute(
        "SELECT rowid, embedding FROM vec_symbols;"
    ).fetchall()
    out: dict[int, list[float]] = {}
    for sym_id, blob in rows:
        try:
            vec = list(struct.unpack(f"<{_VEC_DIM}f", bytes(blob)))
        except struct.error:
            continue
        out[sym_id] = vec
    return out


def file_aggregate_embedding(workspace: str | Path, rel_path: str) -> list[float] | None:
    """Mean of all symbol vectors in ``rel_path`` (posix or native-sep ok).

    Returns ``None`` when embeddings are unavailable or the file has none.
    """
    if not EMBED_AVAILABLE:
        return None
    norm = rel_path.replace("\\", "/")
    key = (str(workspace), norm)
    with _FILE_VEC_LOCK:
        if key in _FILE_VEC_CACHE:
            return _FILE_VEC_CACHE[key]

    conn = _open_db(workspace)
    if conn is None:
        with _FILE_VEC_LOCK:
            _FILE_VEC_CACHE[key] = None
        return None
    try:
        import os
        candidates = [norm]
        if os.sep != "/":
            candidates.append(norm.replace("/", os.sep))

        placeholders = ",".join("?" * len(candidates))
        rows = conn.execute(
            f"SELECT s.id FROM symbols s "
            f"JOIN files f ON f.id = s.file_id "
            f"WHERE f.path IN ({placeholders});",
            candidates,
        ).fetchall()
        if not rows:
            with _FILE_VEC_LOCK:
                _FILE_VEC_CACHE[key] = None
            return None
        sym_ids = [r[0] for r in rows]
        ph = ",".join("?" * len(sym_ids))
        vec_rows = conn.execute(
            f"SELECT embedding FROM vec_symbols WHERE rowid IN ({ph});",
            sym_ids,
        ).fetchall()
        if not vec_rows:
            with _FILE_VEC_LOCK:
                _FILE_VEC_CACHE[key] = None
            return None
        acc = [0.0] * _VEC_DIM
        n = 0
        for (blob,) in vec_rows:
            try:
                v = struct.unpack(f"<{_VEC_DIM}f", bytes(blob))
            except struct.error:
                continue
            for i, x in enumerate(v):
                acc[i] += x
            n += 1
        if n == 0:
            with _FILE_VEC_LOCK:
                _FILE_VEC_CACHE[key] = None
            return None
        mean = [a / n for a in acc]
        with _FILE_VEC_LOCK:
            _FILE_VEC_CACHE[key] = mean
        return mean
    except Exception:
        with _FILE_VEC_LOCK:
            _FILE_VEC_CACHE[key] = None
        return None
    finally:
        conn.close()


def _cosine(a: list[float], b: list[float]) -> float:
    """Plain cosine similarity. Assumes equal length."""
    import math
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def file_cosine(workspace: str | Path, rel_path: str, query_vec: list[float]) -> float:
    """Cosine similarity between a file's aggregate vector and a query vector."""
    fv = file_aggregate_embedding(workspace, rel_path)
    if fv is None:
        return 0.0
    return max(0.0, _cosine(fv, query_vec))


def invalidate_file_cache(workspace: str | None = None) -> None:
    """Drop the file-aggregate cache (call after the structural index changes)."""
    with _FILE_VEC_LOCK:
        if workspace is None:
            _FILE_VEC_CACHE.clear()
        else:
            keys_to_drop = [k for k in _FILE_VEC_CACHE if k[0] == str(workspace)]
            for k in keys_to_drop:
                _FILE_VEC_CACHE.pop(k, None)


__all__ = [
    "EMBED_AVAILABLE",
    "index_db_path",
    "ensure_embedded",
    "embed_query",
    "semantic_search",
    "file_aggregate_embedding",
    "file_cosine",
    "invalidate_file_cache",
]
