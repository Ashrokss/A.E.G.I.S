"""The _cosine length guard, and the embeddings client's mock contract.

No AWS, no credentials, no network. Run:
    PYTHONPATH=. python tests/test_embeddings_dimension_guard.py

Why this exists: _cosine used zip(), which truncates to the shorter vector.
A 256-dim mock vector scored against a 1024-dim real one returned a
plausible-looking number rather than an error, and that number feeds the
knowledge-gap coverage decision. Nothing read `embedding_model` back, so the
label mechanism never caught it either.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.knowledge_base import _cosine  # noqa: E402
from utils.embeddings_client import _MOCK_DIMENSIONS, EmbeddingsModelClient, _mock_embed  # noqa: E402


def test_mismatched_lengths_refuse_to_compare():
    """The bug: zip() would have scored these ~1.0."""
    short = [1.0] * 256
    long = [1.0] * 1024
    assert _cosine(short, long) == 0.0
    assert _cosine(long, short) == 0.0


def test_equal_lengths_are_unchanged():
    assert _cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert abs(_cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9
    assert abs(_cosine([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) - 1.0) < 1e-9


def test_zero_vector_still_returns_zero():
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert _cosine([], []) == 0.0


def test_mock_vectors_are_uniform_and_normalized():
    """Mock mode stays internally comparable: same length, unit norm."""
    a = _mock_embed("the ALB returned 5xx during scale-in")
    b = _mock_embed("an unrelated RDS failover postmortem")
    assert len(a) == len(b) == _MOCK_DIMENSIONS
    assert abs(sum(x * x for x in a) ** 0.5 - 1.0) < 1e-6
    # Disjoint topics score exactly 0 here — that is the stopword list doing
    # its job, not the length guard refusing. Overlapping text must still score.
    assert _cosine(a, b) == 0.0
    assert _cosine(a, _mock_embed("the ALB returned 5xx again")) > 0.0


def test_mock_embed_is_deterministic():
    assert _mock_embed("same text") == _mock_embed("same text")


def test_empty_input_short_circuits_without_a_client():
    saved = {k: os.environ.pop(k, None) for k in (
        "BEDROCK_EMBED_MODEL", "BEDROCK_REGION", "AWS_REGION")}
    try:
        client = EmbeddingsModelClient()
        assert not client.available
        vectors, label = client.embed_many([])
        assert vectors == []
        assert label == "mock"
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_unconfigured_client_falls_back_to_mock_labels():
    saved = {k: os.environ.pop(k, None) for k in (
        "BEDROCK_EMBED_MODEL", "BEDROCK_REGION", "AWS_REGION")}
    try:
        vectors, label = EmbeddingsModelClient().embed_many(["a", "b"])
        assert label == "mock"          # the label a re-embed pass filters on
        assert len(vectors) == 2
        assert all(len(v) == _MOCK_DIMENSIONS for v in vectors)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_model_without_region_is_mock_mode():
    saved = {k: os.environ.pop(k, None) for k in (
        "BEDROCK_EMBED_MODEL", "BEDROCK_REGION", "AWS_REGION")}
    os.environ["BEDROCK_EMBED_MODEL"] = "amazon.titan-embed-text-v2:0"
    try:
        assert not EmbeddingsModelClient().available
    finally:
        os.environ.pop("BEDROCK_EMBED_MODEL", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} checks passed")
