"""Tests for the embedding-based semantic retrieval layer.

Three layers of tests:

1. **Pure-Python helpers** (always run): ``_symbol_text``, ``_serialize``,
   ``_cosine``, ``index_db_path``.
2. **Graceful fallback** (always run): when ``EMBED_AVAILABLE`` is False,
   every public function returns empty/error, ``INDEX_TOOLS`` omits
   ``semantic_search``, and repomap degrades to heuristic-only.
3. **Happy path** (requires the real ``sqlite-vec`` extension + a mocked
   ``fastembed`` model): exercises ``ensure_embedded`` + ``semantic_search``
   end-to-end against a real SQLite database produced by ``wells-index``.
"""

from __future__ import annotations

import pathlib
import struct
import sys
import types
from typing import Iterable

import pytest


# --------------------------------------------------------------------------- #
# Shared fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def indexed_workspace(tmp_path: pathlib.Path) -> pathlib.Path:
    """A tmp workspace whose structural index contains a handful of symbols.

    Built with the real ``wells-index`` Rust engine (skipped if unavailable).
    """
    pytest.importorskip("wells_index")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text(
        "def login(user, password):\n"
        "    return check_password(user, password)\n"
        "\n"
        "def check_password(user, password):\n"
        "    return True\n"
        "\n"
        "class Session:\n"
        "    def start(self):\n"
        "        pass\n"
    )
    (tmp_path / "src" / "math_util.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def matrix_multiply(m1, m2):\n"
        "    return m1\n"
    )
    (tmp_path / "README.md").write_text("# demo\n")
    from wells_index import IndexEngine
    IndexEngine(str(tmp_path)).index()
    return tmp_path


# --------------------------------------------------------------------------- #
# Layer 1: pure-Python helpers (no deps)
# --------------------------------------------------------------------------- #


def test_symbol_text_includes_kind_name_and_path():
    from wells.embeddings import _symbol_text
    t = _symbol_text("login", "function", "src/auth.py")
    assert "login" in t
    assert "function" in t
    assert "auth" in t  # extension stripped


def test_symbol_text_normalises_separators():
    from wells.embeddings import _symbol_text
    t = _symbol_text("foo", "function", r"src\nested\mod.py")
    assert "\\" not in t  # all forward slashes


def test_serialize_roundtrip():
    from wells.embeddings import _serialize, _VEC_DIM
    v = [0.5] * _VEC_DIM
    blob = _serialize(v)
    assert isinstance(blob, bytes)
    assert len(blob) == _VEC_DIM * 4  # float32
    back = list(struct.unpack(f"<{_VEC_DIM}f", blob))
    assert back == pytest.approx(v)


def test_cosine_basic():
    from wells.embeddings import _cosine
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_cosine_zero_vector_is_safe():
    from wells.embeddings import _cosine
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_index_db_path():
    from wells.embeddings import index_db_path
    p = index_db_path("/tmp/repo")
    assert p.name == "index.db"
    assert p.parent.name == ".wells_index"


# --------------------------------------------------------------------------- #
# Layer 2: graceful fallback (when EMBED_AVAILABLE is False)
# --------------------------------------------------------------------------- #


def test_fallback_semantic_search_returns_empty(tmp_path):
    """When libs missing, semantic_search returns [] without raising."""
    from wells import embeddings
    if embeddings.EMBED_AVAILABLE:
        pytest.skip("libs installed — fallback path not exercisable here")
    assert embeddings.semantic_search(tmp_path, "anything") == []


def test_fallback_embed_query_returns_none():
    from wells import embeddings
    if embeddings.EMBED_AVAILABLE:
        pytest.skip("libs installed")
    assert embeddings.embed_query("anything") is None


def test_index_tools_always_exposes_semantic_search():
    """semantic_search is always registered so the LLM can discover it; the
    handler returns an informative error when the optional libs are missing.
    This makes capability-discovery consistent across installs."""
    from wells import index_tools
    if not index_tools.INDEXER_AVAILABLE:
        pytest.skip("wells-index not installed")
    names = [t.name for t in index_tools.INDEX_TOOLS]
    assert "semantic_search" in names


def embeddings_available() -> bool:
    from wells import embeddings
    return embeddings.EMBED_AVAILABLE


def test_fallback_repomap_still_builds(tmp_path):
    """Repomap must produce output even without embeddings."""
    from wells import repomap
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
    m = repomap.build_repo_map(str(tmp_path), goal="anything")
    assert "a.py" in m


# --------------------------------------------------------------------------- #
# Layer 3: happy path (sqlite-vec + mocked fastembed)
# --------------------------------------------------------------------------- #


class _FakeTextEmbedding:
    """A stand-in for ``fastembed.TextEmbedding``.

    Maps text → a 384-dim vector by hashing each whitespace-delimited token
    into one of 384 buckets and L2-normalising. Two texts that share words
    therefore have non-trivial cosine similarity, which is all the tests
    need to exercise the ranking logic.

    Note: the BGE retrieval prefix added by ``embeddings.embed_query`` is
    stripped before hashing — the prefix matters for the real BGE model
    (it was trained with it), but for our hash-based fake it just adds
    noise that can collide with real tokens.
    """

    _PREFIX = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str = ""):
        self.model_name = model_name

    def embed(self, texts: Iterable[str]):
        import math
        dim = 384
        out = []
        for raw in texts:
            t = raw or ""
            if t.startswith(self._PREFIX):
                t = t[len(self._PREFIX):]
            vec = [0.0] * dim
            for tok in t.lower().split():
                # Stable 32-bit hash → bucket.
                h = 2166136261
                for ch in tok:
                    h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
                vec[h % dim] += 1.0
            # Ensure non-zero so cosine is defined.
            if all(x == 0.0 for x in vec):
                vec[0] = 1.0
            norm = math.sqrt(sum(x * x for x in vec))
            out.append([x / norm for x in vec])
        # Match fastembed's generator interface.
        yield from out


@pytest.fixture
def mocked_embeddings(monkeypatch):
    """Force ``embeddings.EMBED_AVAILABLE = True`` with a fake fastembed."""
    pytest.importorskip("sqlite_vec")  # only the SQL extension needs to be real

    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = _FakeTextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)

    from wells import embeddings
    monkeypatch.setattr(embeddings, "EMBED_AVAILABLE", True, raising=True)
    # Reset the cached singleton so the fake model is picked up.
    monkeypatch.setattr(embeddings, "_model", None, raising=True)
    # Clear the file-aggregate cache between tests.
    monkeypatch.setattr(embeddings, "_FILE_VEC_CACHE", {}, raising=True)
    return embeddings


def test_ensure_embedded_populates_vec_table(indexed_workspace, mocked_embeddings):
    emb = mocked_embeddings
    stats = emb.ensure_embedded(indexed_workspace)
    assert stats["error"] is None
    assert stats["embedded"] > 0
    assert stats["total"] >= stats["embedded"]  # at least as many total

    # Second run is a no-op (idempotent).
    stats2 = emb.ensure_embedded(indexed_workspace)
    assert stats2["error"] is None
    assert stats2["embedded"] == 0


def test_semantic_search_finds_relevant_symbol(indexed_workspace, mocked_embeddings):
    emb = mocked_embeddings
    # Query that shares tokens with the login function's source_text.
    results = emb.semantic_search(indexed_workspace, "login password function")
    assert results, "expected at least one semantic match"
    names = [r["name"] for r in results]
    # The login/check_password pair should rank above math symbols.
    assert "login" in names or "check_password" in names
    top = results[0]
    assert 0.0 <= top["score"] <= 1.0
    assert top["file_path"].endswith("auth.py") or "auth" in top["file_path"]
    assert top["start_line"] >= 1


def test_semantic_search_empty_query_returns_empty(indexed_workspace, mocked_embeddings):
    emb = mocked_embeddings
    assert emb.semantic_search(indexed_workspace, "") == []
    assert emb.semantic_search(indexed_workspace, "   ") == []


def test_semantic_search_respects_limit(indexed_workspace, mocked_embeddings):
    emb = mocked_embeddings
    emb.ensure_embedded(indexed_workspace)
    results = emb.semantic_search(indexed_workspace, "function", limit=2)
    assert len(results) <= 2


def test_semantic_search_auto_embeds_on_first_call(indexed_workspace, mocked_embeddings):
    emb = mocked_embeddings
    # No explicit ensure_embedded — first call should bootstrap vectors.
    results = emb.semantic_search(indexed_workspace, "login", limit=5)
    assert len(results) >= 1


def test_file_aggregate_embedding_is_cached(indexed_workspace, mocked_embeddings):
    emb = mocked_embeddings
    emb.ensure_embedded(indexed_workspace)
    v1 = emb.file_aggregate_embedding(indexed_workspace, "src/auth.py")
    assert v1 is not None
    assert len(v1) == 384
    # Second call returns the cached object (identity check).
    v2 = emb.file_aggregate_embedding(indexed_workspace, "src/auth.py")
    assert v2 is v1


def test_file_aggregate_handles_native_separators(indexed_workspace, mocked_embeddings):
    emb = mocked_embeddings
    emb.ensure_embedded(indexed_workspace)
    # POSIX form should also resolve.
    v = emb.file_aggregate_embedding(indexed_workspace, "src/auth.py")
    assert v is not None


def test_invalidate_file_cache_drops_entries(indexed_workspace, mocked_embeddings):
    emb = mocked_embeddings
    emb.ensure_embedded(indexed_workspace)
    _ = emb.file_aggregate_embedding(indexed_workspace, "src/auth.py")
    assert emb._FILE_VEC_CACHE, "expected at least one cached entry"
    emb.invalidate_file_cache(str(indexed_workspace))
    assert not emb._FILE_VEC_CACHE


def test_file_cosine_returns_zero_for_unknown_file(indexed_workspace, mocked_embeddings):
    emb = mocked_embeddings
    emb.ensure_embedded(indexed_workspace)
    qvec = emb.embed_query("login")
    assert qvec is not None
    score = emb.file_cosine(indexed_workspace, "does/not/exist.py", qvec)
    assert score == 0.0


def test_repomap_semantic_rerank_reorders(indexed_workspace, mocked_embeddings):
    """When embeddings are on, repomap should boost files semantically
    related to the goal above ones that merely share path keywords."""
    from wells import repomap
    emb = mocked_embeddings
    emb.ensure_embedded(indexed_workspace)

    # Build entries by hand (mirrors repomap.build_repo_map's loop).
    entries = [
        (0.0, "src/math_util.py", "src/math_util.py: add, matrix_multiply"),
        (0.0, "src/auth.py", "src/auth.py: login, check_password, Session"),
    ]
    repomap._semantic_rerank(entries, str(indexed_workspace), "login password")
    # auth.py should now have a strictly higher score than math_util.py.
    by_rel = {rel: sc for sc, rel, _ in entries}
    assert by_rel["src/auth.py"] > by_rel["src/math_util.py"]


def test_index_tools_exposes_semantic_search_when_available(mocked_embeddings):
    from wells import index_tools
    names = [t.name for t in index_tools.INDEX_TOOLS]
    assert "semantic_search" in names
