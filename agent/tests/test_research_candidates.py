"""Web-research approval queue: candidates are pending until a tenant admin
approves (promoting them into retrievable knowledge, resolving their gap) or
rejects (reopening the gap). Run: python tests/test_research_candidates.py"""
import asyncio
import os
import tempfile

from tools import knowledge_base as kb

db = os.path.join(tempfile.mkdtemp(), "runs.db")
kb.ensure_table(db)

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

# ─── extract_citations ────────────────────────────────────────────────────────
answer = (
    "Per the runbook [Postmortem 2026 · Root Cause] this matches a known AWS "
    "issue documented at https://docs.aws.amazon.com/some/page and also "
    "discussed at https://example.com/blog/post (see both for context)."
)
doc_tags, web_urls = kb.extract_citations(answer)
assert doc_tags == ["Postmortem 2026 · Root Cause"], doc_tags
assert web_urls == ["https://docs.aws.amazon.com/some/page", "https://example.com/blog/post"], web_urls

# A pure web-only answer has no doc citations.
web_only_tags, web_only_urls = kb.extract_citations("Found at https://status.aws.amazon.com/x only.")
assert web_only_tags == []
assert web_only_urls == ["https://status.aws.amazon.com/x"]

# ─── Gap linkage: rejecting reopens, approving resolves ──────────────────────
QUESTION = "Why does the connection pool exhaust during failover?"
gap_id = kb.record_gap(db, TENANT_A, QUESTION, "low_coverage", 0.1)
assert kb.list_gaps(db, TENANT_A, status="open") != []

candidate_id = kb.create_research_candidate(
    db, TENANT_A, gap_id, QUESTION,
    "During failover the app tier holds stale connections "
    "[https://docs.aws.amazon.com/rds/failover]",
    doc_citations=[], web_citations=["https://docs.aws.amazon.com/rds/failover"],
    created_by="sre_agent",
)
pending = kb.list_research_candidates(db, TENANT_A)
assert len(pending) == 1 and pending[0]["id"] == candidate_id, pending
assert pending[0]["status"] == "pending_approval"
assert pending[0]["web_citations"] == ["https://docs.aws.amazon.com/rds/failover"]

# ─── Reject: gap reopens, candidate marked rejected, audited ─────────────────
rejected_ok = kb.reject_research_candidate(db, TENANT_A, candidate_id, "alice@example.com", "not specific enough")
assert rejected_ok is True
assert kb.get_research_candidate(db, candidate_id)["status"] == "rejected"
assert any(g["id"] == gap_id and g["status"] == "open" for g in kb.list_gaps(db, TENANT_A, status="open")), \
    "rejecting a candidate must reopen its gap, not leave it resolved"
audit = kb.list_audit(db, TENANT_A)
assert any(a["action"] == "candidate_rejected" and a["subject_id"] == candidate_id for a in audit), audit

# A second rejection of the same (already-reviewed) candidate is a no-op failure.
assert kb.reject_research_candidate(db, TENANT_A, candidate_id, "alice@example.com", "again") is False

# ─── Approve: promotes into a real, retrievable document; resolves the gap ───
candidate_id_2 = kb.create_research_candidate(
    db, TENANT_A, gap_id, QUESTION,
    "# Connection pool exhaustion during RDS failover\n\n"
    "## Root Cause\nDuring an RDS Multi-AZ failover the application tier holds "
    "stale connections open past the DNS TTL, exhausting the pool.\n",
    doc_citations=[], web_citations=["https://docs.aws.amazon.com/rds/failover"],
    created_by="sre_agent",
)
doc_id = asyncio.run(kb.approve_research_candidate(db, TENANT_A, candidate_id_2, "bob@example.com"))
assert doc_id is not None
assert kb.get_research_candidate(db, candidate_id_2)["status"] == "approved"
doc = kb.get_document(db, doc_id)
assert doc is not None and doc["tenant_id"] == TENANT_A
assert doc["status"] == "ready", doc  # approve_research_candidate ingests synchronously
assert "approved by bob@example.com" in doc["uploaded_by"], doc
assert any(g["id"] == gap_id for g in kb.list_gaps(db, TENANT_A, status="resolved")), \
    "approving a candidate must resolve its gap"

# The promoted content is now genuinely retrievable, like any uploaded document.
results = kb.search(db, TENANT_A, "connection pool exhaustion during failover", top_k=3)["results"]
assert any(r["document_title"].startswith("Connection pool exhaustion") for r in results), results

# ─── Tenant isolation ─────────────────────────────────────────────────────────
assert kb.list_research_candidates(db, TENANT_B) == []
cross_tenant_candidate = kb.create_research_candidate(
    db, TENANT_A, None, "another question", "some answer", [], [], "sre_agent",
)
assert asyncio.run(kb.approve_research_candidate(db, TENANT_B, cross_tenant_candidate, "eve@example.com")) is None, \
    "must not approve another tenant's candidate"
assert kb.get_research_candidate(db, cross_tenant_candidate)["status"] == "pending_approval"

print("ok: citation extraction, gap reopen-on-reject / resolve-on-approve, "
      "promoted content is retrievable, cross-tenant approval rejected")