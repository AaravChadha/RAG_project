"""Tests for ``retrieval.theory`` — substring + embedding-fallback matching.

The substring path is exercised directly. The embedding path is tested by
swapping in a fake model + fake topic vectors (we don't actually pull the
80 MB sentence-transformers model into CI). This keeps tests fast and
hermetic while still covering the contract: substring hits short-circuit
the embedding path, paraphrase queries hit embedding, embedding misses
fall through to the no-match envelope, and graceful degradation when
``sentence-transformers`` isn't available.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from retrieval import theory  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_theory_state(monkeypatch):
    """Reset module-level caches between tests so each starts fresh.

    The theory module caches both the loaded ``theory.json`` and the
    lazily-initialised embedding fallback. Without a reset, the state
    bleeds between tests — particularly bad for ``_EMBEDDING_INITIALISED``
    which short-circuits the init-once guard.
    """
    monkeypatch.setattr(theory, "_THEORY_CACHE", None)
    monkeypatch.setattr(theory, "_EMBEDDING_MODEL", None)
    monkeypatch.setattr(theory, "_TOPIC_VECTORS", None)
    monkeypatch.setattr(theory, "_EMBEDDING_INITIALISED", False)
    monkeypatch.setattr(theory, "_EMBEDDING_AVAILABLE", False)
    yield


# ---------------------------------------------------------------------------
# Substring fast-path
# ---------------------------------------------------------------------------

def test_substring_exact_alias_match() -> None:
    """An exact-alias query hits the substring path and returns the entry."""
    result = theory.get_education_content("what is sip")
    assert result["matched"] is True
    assert result["topic_id"] == "what_is_sip"


def test_substring_bidirectional_match() -> None:
    """``_matches`` is bidirectional — short query inside a longer alias matches."""
    # "SIP" is shorter than any alias but should match "what is sip" via
    # substring-in-either-direction.
    result = theory.get_education_content("SIP")
    assert result["matched"] is True
    assert result["topic_id"] == "what_is_sip"


def test_substring_no_match_returns_envelope() -> None:
    """A query with no substring match AND no embedding model produces no-match."""
    # Without sentence-transformers loaded (autouse fixture resets state),
    # this query that doesn't match any alias substring should fall through
    # to the no-match envelope.
    result = theory.get_education_content("nonexistent gibberish xyzzy")
    assert result["matched"] is False
    assert result["topic_queried"] == "nonexistent gibberish xyzzy"
    assert isinstance(result["available_topics"], list)
    assert result["available_topics"], "available_topics should list FAQ entries"


def test_bad_arguments_topic_empty() -> None:
    """An empty topic string produces a bad_arguments error envelope."""
    result = theory.get_education_content("")
    assert result.get("error") == "bad_arguments"


def test_bad_arguments_topic_non_string() -> None:
    """A non-string topic produces a bad_arguments error envelope."""
    result = theory.get_education_content(None)  # type: ignore[arg-type]
    assert result.get("error") == "bad_arguments"


# ---------------------------------------------------------------------------
# Embedding fallback with a fake model (no real sentence-transformers needed)
# ---------------------------------------------------------------------------

class _FakeModel:
    """Minimal stand-in for SentenceTransformer used in tests.

    Returns a deterministic 4-D vector keyed off whether the input
    mentions our test topic clusters. Lets us drive the cosine-similarity
    path without pulling in the real 80 MB model.

    Unknown text returns the zero vector — cosine 0 with any topic, so
    the fallback's threshold check correctly reports "no match" rather
    than accidentally clustering everything-without-keywords together.
    """

    def encode(self, text, normalize_embeddings=True):
        import numpy as np
        text_lower = text.lower() if isinstance(text, str) else ""
        # MF cluster.
        if "mf" in text_lower or "mutual fund" in text_lower:
            return np.array([1.0, 0.0, 0.0, 0.0])
        # SIP cluster.
        if "sip" in text_lower:
            return np.array([0.0, 1.0, 0.0, 0.0])
        # Everything else: zero vector. Cosine with any topic vector is 0,
        # safely below threshold — the fallback returns None.
        return np.array([0.0, 0.0, 0.0, 0.0])


def _inject_fake_embedding_state(monkeypatch):
    """Set up the theory module with the fake model + pre-built vectors.

    Bypasses the lazy-init path so tests don't try to import
    ``sentence_transformers`` at all.
    """
    import numpy as np
    fake = _FakeModel()
    topics = theory._load_topics()

    # Build per-topic vectors using the fake encoder so the topic with
    # "mutual fund" / "what is mf" aliases lands at [1,0,0,0] and
    # the SIP topic at [0,1,0,0]. Everything else at [0,0,1,0].
    vectors = []
    for entry in topics:
        title = entry.get("title", "")
        aliases = entry.get("aliases", []) or []
        surface = " | ".join([title] + [a for a in aliases if a])
        vec = fake.encode(surface)
        vectors.append((entry, vec))

    monkeypatch.setattr(theory, "_EMBEDDING_MODEL", fake)
    monkeypatch.setattr(theory, "_TOPIC_VECTORS", vectors)
    monkeypatch.setattr(theory, "_EMBEDDING_INITIALISED", True)
    monkeypatch.setattr(theory, "_EMBEDDING_AVAILABLE", True)


def test_embedding_fallback_catches_paraphrase(monkeypatch) -> None:
    """A paraphrase that the substring matcher misses gets caught by embeddings."""
    _inject_fake_embedding_state(monkeypatch)

    # "explain MFs concept" doesn't substring-match any alias verbatim but
    # the fake model maps it to the MF cluster (because "mf" is in the
    # query). Embedding fallback should pick the what_is_mf entry.
    result = theory.get_education_content("explain MFs concept")
    assert result["matched"] is True
    assert result["topic_id"] == "what_is_mf"


def test_embedding_fallback_below_threshold_no_match(monkeypatch) -> None:
    """Embedding similarity below threshold falls through to no-match envelope."""
    _inject_fake_embedding_state(monkeypatch)

    # The fake model maps this query to [0,0,1,0] which is orthogonal to
    # all topic vectors -> cosine 0 < threshold -> no match.
    result = theory.get_education_content("zzz unrelated query")
    assert result["matched"] is False


def test_graceful_degradation_without_sentence_transformers(monkeypatch) -> None:
    """If ``sentence-transformers`` import fails, substring still works."""
    # Force ImportError on the lazy import inside _init_embedding_fallback.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sentence_transformers":
            raise ImportError("simulated missing dep")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Substring path still works.
    result = theory.get_education_content("what is sip")
    assert result["matched"] is True

    # And a paraphrase that ONLY embedding could catch falls through to
    # no-match because embeddings are unavailable.
    result = theory.get_education_content("zzz totally unrelated paraphrase")
    assert result["matched"] is False
    # Embedding fallback is disabled but substring still functions —
    # confirmed by the matched=True case above.


def test_substring_short_circuits_embedding(monkeypatch) -> None:
    """When substring matches, the embedding path is NOT consulted.

    Verifies the order-of-operations: substring is the fast path, embedding
    is the slow fallback. We assert by injecting a fake model that would
    return the WRONG topic for the SIP query — if substring works first,
    the fake never gets used and we still get the right topic.
    """
    _inject_fake_embedding_state(monkeypatch)

    # "what is sip" substring-matches the what_is_sip alias directly.
    # The fake model would have returned [0,1,0,0] too (the SIP cluster)
    # so let's swap in a wrong-answer model to prove substring won the race.
    class WrongAnswerModel:
        def encode(self, text, normalize_embeddings=True):
            import numpy as np
            # Always return the MF-cluster vector — i.e. the wrong topic
            # for a SIP query. If embedding wins, this test will fail.
            return np.array([1.0, 0.0, 0.0, 0.0])

    monkeypatch.setattr(theory, "_EMBEDDING_MODEL", WrongAnswerModel())

    result = theory.get_education_content("what is sip")
    assert result["matched"] is True
    assert result["topic_id"] == "what_is_sip", (
        "Substring should have matched first; if this returns what_is_mf, "
        "the embedding fallback is running before substring."
    )
