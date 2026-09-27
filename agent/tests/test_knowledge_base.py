"""Two-pass chunking (with the whitespace-boundary fix), hybrid RRF retrieval,
citation resolution, gap detection, and tenant isolation for the RCA knowledge
base. Run: python tests/test_knowledge_base.py"""
import asyncio
import json
import os
import tempfile

from tools import knowledge_base as kb

db = os.path.join(tempfile.mkdtemp(), "runs.db")

# ensure_table must be safe to call on every boot, not just the first.
kb.ensure_table(db)
kb.ensure_table(db)

# ─── Chunking ───────────────────────────────────────────────────────────────
DOC = (
    "# ALB 5xx spike\n\n"
    "## Root Cause\n"
    + ("Targets were draining during an ASG scale-in event. " * 40) + "\n\n"
    "## Impact\n"
    "No user-facing outage; error budget consumed briefly.\n"
)

chunks = kb.chunk_text(DOC)
assert chunks, "expected at least one chunk"
assert chunks[0]["section_title"] is None, "text before the first ## header has no section"
section_titles = {c["section_title"] for c in chunks}
assert "Root Cause" in section_titles, section_titles
assert "Impact" in section_titles, section_titles
# The oversized Root Cause section must have been size-split, not left as one chunk.
root_cause_chunks = [c for c in chunks if c["section_title"] == "Root Cause"]
assert len(root_cause_chunks) > 1, "an oversized section must be size-split"
for c in root_cause_chunks:
    assert len(c["text"]) <= kb.CHUNK_SIZE, len(c["text"])
# No chunk may start or end mid-word (the whitespace-boundary fix) — every
# emitted piece is already `.strip()`-ed, so a stray leading/trailing partial
# word would show up as a piece starting/ending with a fragment of the
# 40x-repeated phrase's individual words rather than a whole one.
for c in root_cause_chunks:
    words = c["text"].split()
    assert all(w.strip(".,;:") for w in words), c["text"]

# Regression guard for the "short overlap barely advances" bug: a long,
# header-free body must not explode into a huge number of near-duplicate
# chunks — each chunk should advance by a meaningful fraction of CHUNK_SIZE.
LONG_BODY = "word " * 2000  # ~10,000 chars, no ## headers at all
long_chunks = kb.chunk_text(LONG_BODY)
expected_upper_bound = (len(LONG_BODY) // (kb.CHUNK_SIZE - kb.CHUNK_OVERLAP)) + 2
assert len(long_chunks) <= expected_upper_bound, (
    f"chunking exploded: {len(long_chunks)} chunks for a {len(LONG_BODY)}-char body, "
    f"expected at most ~{expected_upper_bound}"
)

# ─── cite_key ───────────────────────────────────────────────────────────────
assert kb._make_cite_key("A short title", "whatever.md") == "A short title"
assert kb._make_cite_key("x" * 100, "postmortem-2026.md") == "postmortem-2026"
assert kb._make_cite_key("", "no-title.md") == "no-title"

# ─── Ingestion + hybrid retrieval ────────────────────────────────────────────
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

doc_scaling = kb.create_document(
    db, TENANT_A, "scaling-incident.md",
    "# ASG scale-in drains targets\n\n## Root Cause\nASG scale-in terminated targets "
    "before connection draining completed, causing 5xx errors on the ALB.\n",
    uploaded_by="alice",
)
doc_db = kb.create_document(
    db, TENANT_A, "db-failover.md",
    "# RDS failover latency\n\n## Root Cause\nA Multi-AZ failover caused a brief "
    "connection pool exhaustion in the application tier.\n",
    uploaded_by="alice",
)

asyncio.run(kb.ingest_document(db, doc_scaling))
asyncio.run(kb.ingest_document(db, doc_db))

assert kb.get_document(db, doc_scaling)["status"] == "ready", kb.get_document(db, doc_scaling)
assert kb.get_document(db, doc_db)["status"] == "ready", kb.get_document(db, doc_db)

payload = kb.search(db, TENANT_A, "ASG scale-in terminated targets during draining", top_k=2)
assert "results" in payload and "coverage" in payload, payload
results = payload["results"]
assert results, "expected at least one match"
# The right document must dominate the top results (the corpus is only two
# documents, so which specific chunk of it — the title line or the Root
# Cause body — ranks first can legitimately vary with the embedder/BM25
# weighting; asserting on the document, not the exact chunk, is what matters).
assert all(r["document_title"] == "ASG scale-in drains targets" for r in results), results
assert results[0]["cite_key"] == "ASG scale-in drains targets", results[0]
assert results[0]["citation_tag"].startswith("[ASG scale-in drains targets ·"), results[0]
root_cause_hit = next((r for r in results if r["section_title"] == "Root Cause"), None)
assert root_cause_hit is not None, "the Root Cause chunk should be retrievable at all for this query"
assert root_cause_hit["citation_tag"] == "[ASG scale-in drains targets · Root Cause]", root_cause_hit
# A direct, near-exact match against the actual incident should clear the
# relevance gate.
assert payload["relevant"], (payload["coverage"], payload["coverage_floor"])

# BM25 must be contributing, not just semantic similarity: a query that only
# shares an exact identifier-ish token with one document (and nothing
# semantically similar) should still surface it via the keyword ranking half
# of the RRF fusion.
kw_payload = kb.search(db, TENANT_A, "Multi-AZ", top_k=2)
kw_titles = [r["document_title"] for r in kw_payload["results"]]
assert "RDS failover latency" in kw_titles, kw_titles

# ─── Citation resolution ──────────────────────────────────────────────────────
answer_text = (
    "The cause was targets draining during a scale-in "
    "[ASG scale-in drains targets · Root Cause]. An unrelated tag "
    "[Nonexistent Doc · Root Cause] must not resolve."
)
citations = kb.resolve_citations(answer_text, results)  # validated against whatever was actually retrieved
assert len(citations) == 1, citations
assert citations[0]["cite_key"] == "ASG scale-in drains targets", citations[0]

# ─── Aggregate routing ────────────────────────────────────────────────────────
assert kb.classify_question("how many incidents mention RDS?") == "aggregate"
assert kb.classify_question("what caused the ALB 5xx spike?") == "retrieval"
index_text = kb.render_document_index(db, TENANT_A)
assert "ASG scale-in drains targets" in index_text
assert "RDS failover latency" in index_text

# A model driving search_knowledge_base routinely shortens an aggregate
# question down to a bare keyword (observed live: "How many incidents are in
# the knowledge base?" -> query="incidents") -- classify_question on that
# alone misses it. dispatch's user_question parameter is exactly this
# regression guard: pass the ORIGINAL question through and the aggregate
# route must still fire even though the tool's own query argument would not
# trigger it alone.
aggregate_payload = json.loads(asyncio.run(kb.dispatch(
    db, TENANT_A, "search_knowledge_base", {"query": "incidents"},
    user_question="How many incidents are in the knowledge base?",
)))
assert aggregate_payload.get("document_index"), (
    "user_question must independently trigger aggregate routing even when the "
    "tool's own query argument (a model paraphrase) does not", aggregate_payload
)
# document_count exists precisely because an LLM counting table rows in a
# busy tool result is unreliable even at small counts (observed live: a real
# model miscounted a 24-row table as 22, and a 25-row one as 55) -- the
# count must be handed over precomputed, not left for the model to derive.
assert aggregate_payload.get("document_count") == 2, aggregate_payload
assert aggregate_payload["relevant"] is True, (
    "an aggregate answer must never also read as a knowledge gap", aggregate_payload
)
# The tool's own query argument still works on its own when it IS aggregate-shaped.
own_query_payload = json.loads(asyncio.run(kb.dispatch(
    db, TENANT_A, "search_knowledge_base", {"query": "how many incidents are there?"},
)))
assert own_query_payload.get("document_index") and own_query_payload.get("document_count") == 2, own_query_payload
# Neither being aggregate-shaped must not spuriously add a document_index/count.
neither_payload = json.loads(asyncio.run(kb.dispatch(
    db, TENANT_A, "search_knowledge_base", {"query": "ASG scale-in terminated targets during draining"},
    user_question="What caused the ALB 5xx spike during scale-in?",
)))
assert "document_index" not in neither_payload and "document_count" not in neither_payload, neither_payload

# ─── Gap detection ────────────────────────────────────────────────────────────
# Also a regression guard for a real false positive this corpus reproduces:
# the mock embedding's hashing trick alone scores this query at 0.2426 --
# above coverage_floor (0.15) purely by hash-collision chance, with zero
# actual shared vocabulary with either document. "relevant" must catch what
# raw coverage misses.
no_hit_payload = kb.search(db, TENANT_A, "completely unrelated kubernetes ingress question", top_k=5)
assert not no_hit_payload["relevant"], (no_hit_payload["coverage"], no_hit_payload["coverage_floor"])
gap_id = kb.record_gap(db, TENANT_A, "Completely unrelated kubernetes ingress question?",
                        "low_coverage", no_hit_payload["coverage"])
gaps = kb.list_gaps(db, TENANT_A)
assert any(g["id"] == gap_id for g in gaps), gaps
assert gaps[0]["hit_count"] == 1

# Asking the same question again (different casing/whitespace) must collapse
# into the same gap row via the normalized-question upsert, not create a
# second one.
gap_id_2 = kb.record_gap(db, TENANT_A, "completely   unrelated KUBERNETES ingress question",
                          "low_coverage", no_hit_payload["coverage"])
assert gap_id_2 == gap_id, (gap_id, gap_id_2)
gaps_after = kb.list_gaps(db, TENANT_A)
assert len(gaps_after) == 1, gaps_after
assert gaps_after[0]["hit_count"] == 2, gaps_after[0]

kb.set_gap_status(db, gap_id, "resolved")
assert kb.list_gaps(db, TENANT_A, status="open") == []
assert len(kb.list_gaps(db, TENANT_A, status="resolved")) == 1

# ─── Tenant isolation ─────────────────────────────────────────────────────────
b_payload = kb.search(db, TENANT_B, "ASG scale-in terminated targets during draining", top_k=5)
assert b_payload["results"] == [], b_payload

doc_b_only = kb.create_document(
    db, TENANT_B, "tenant-b-secret.md",
    "# Tenant B private incident\n\n## Root Cause\nASG scale-in terminated targets during "
    "draining, a tenant-B-specific detail nobody else should see.\n",
    uploaded_by="bob",
)
asyncio.run(kb.ingest_document(db, doc_b_only))
assert kb.get_document(db, doc_b_only)["status"] == "ready"

a_results_after = kb.search(db, TENANT_A, "ASG scale-in terminated targets during draining", top_k=5)
assert all(r["document_title"] != "Tenant B private incident" for r in a_results_after["results"]), a_results_after

# Gaps must be tenant-scoped too — tenant B's own low-coverage question must
# not appear in tenant A's gap list.
kb.record_gap(db, TENANT_B, "tenant b's own unanswerable question", "low_coverage", 0.1)
assert all(g["question"] != "tenant b's own unanswerable question" for g in kb.list_gaps(db, TENANT_A, status=None))

# ─── Listing and deletion are tenant-scoped ───────────────────────────────────
assert len(kb.list_documents(db, TENANT_A)) == 2
assert len(kb.list_documents(db, TENANT_B)) == 1
assert kb.delete_document(db, TENANT_B, doc_scaling) is False, "must not delete another tenant's document"
assert kb.delete_document(db, TENANT_A, doc_scaling) is True
assert len(kb.list_documents(db, TENANT_A)) == 1

# ─── dispatch() declines to search with no tenant scope ──────────────────────
no_tenant = asyncio.run(kb.dispatch(db, None, "search_knowledge_base", {"query": "anything"}))
assert "No knowledge base is scoped" in no_tenant, no_tenant

# ─── dispatch() end to end: aggregate route includes the document index ─────
agg_result = asyncio.run(kb.dispatch(db, TENANT_A, "search_knowledge_base",
                                      {"query": "list every incident", "top_k": 5}))
assert "document_index" in agg_result, agg_result
assert "RDS failover latency" in agg_result, agg_result

print("ok: fixed chunking, hybrid RRF retrieval, citations, aggregate routing, "
      "gap detection, tenant isolation, scoped list/delete")