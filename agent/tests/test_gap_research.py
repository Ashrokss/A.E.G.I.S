"""The "Perform research" pipeline on a knowledge gap: a gap can be
rejected outright (dismissed, no answer generated) or researched (a
pending_approval document is created, never auto-ingested, until an admin
approves or rejects it). Mirrors test_research_candidates.py's gap-linkage
shape (reject reopens, approve resolves), applied to gap -> document instead
of gap -> candidate. Run: python tests/test_gap_research.py"""
import asyncio
import os
import tempfile

from tools import knowledge_base as kb
from tools import kb_report

db = os.path.join(tempfile.mkdtemp(), "runs.db")
kb.ensure_table(db)

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

# ─── get_gap: tenant-scoped, same convention as get_thread/delete_document ───
gap_id = kb.record_gap(db, TENANT_A, "What causes X?", "low_coverage", 0.1)
assert kb.get_gap(db, TENANT_A, gap_id) is not None
assert kb.get_gap(db, TENANT_B, gap_id) is None, "wrong tenant must not see the gap"
assert kb.get_gap(db, TENANT_A, "no-such-gap") is None

# ─── reject_gap: dismisses an open gap, no document, no reason required ──────
dismissed_gap = kb.record_gap(db, TENANT_A, "A question nobody will research", "low_coverage", 0.05)
assert kb.reject_gap(db, TENANT_A, dismissed_gap) is True
assert kb.get_gap(db, TENANT_A, dismissed_gap)["status"] == "resolved"
# Rejecting again (no longer 'open') is a no-op failure, not a silent re-resolve.
assert kb.reject_gap(db, TENANT_A, dismissed_gap) is False
# Cross-tenant reject must not touch tenant A's gap.
assert kb.reject_gap(db, TENANT_B, gap_id) is False
assert kb.get_gap(db, TENANT_A, gap_id)["status"] == "open"

# ─── create_document(status='pending_approval', gap_id=...): eager row, ─────
# never auto-ingested ──────────────────────────────────────────────────────
pending_doc_id = kb.create_document(
    db, TENANT_A, f"research-{gap_id[:8]}.md",
    "# Research: What causes X?\n\n_Nothing in the web results was relevant enough.._",
    uploaded_by="web research, requested by alice@example.com",
    status="pending_approval", gap_id=gap_id,
)
pending_doc = kb.get_document(db, pending_doc_id)
assert pending_doc["status"] == "pending_approval", pending_doc
assert pending_doc["gap_id"] == gap_id, pending_doc
listed = kb.list_documents(db, TENANT_A)
assert any(d["id"] == pending_doc_id and d["status"] == "pending_approval" for d in listed), (
    "list_documents (what the RCA docs tab reads) must surface a pending_approval row "
    "with no separate endpoint or query needed"
)
listed_row = next(d for d in listed if d["id"] == pending_doc_id)
assert listed_row["chunk_count"] == 0, "a pending document must not have been ingested yet"

# An invalid status is rejected outright, same as set_document_status already does.
try:
    kb.create_document(db, TENANT_A, "x.md", "text", uploaded_by=None, status="not-a-real-status")
    assert False, "an unknown document status must raise"
except ValueError:
    pass

# ─── reject_pending_document: deletes the doc, reopens the gap ──────────────
reopen_gap = kb.record_gap(db, TENANT_A, "Reopen me", "low_coverage", 0.1)
kb.set_gap_status(db, reopen_gap, "researching")
reject_doc_id = kb.create_document(
    db, TENANT_A, "research-reopen.md", "# Research: Reopen me\n\nDraft content.",
    uploaded_by="web research", status="pending_approval", gap_id=reopen_gap,
)
assert kb.reject_pending_document(db, TENANT_A, reject_doc_id) is True
assert kb.get_document(db, reject_doc_id) is None, "reject must delete the pending document outright"
assert kb.get_gap(db, TENANT_A, reopen_gap)["status"] == "open", (
    "rejecting a pending document must reopen its gap, not leave it stuck in 'researching'"
)
# Rejecting a document that no longer exists (or isn't pending_approval) is a no-op failure.
assert kb.reject_pending_document(db, TENANT_A, reject_doc_id) is False
assert kb.reject_pending_document(db, TENANT_A, pending_doc_id and "not-a-real-id") is False

# A ready document cannot be "reject-pending"'d -- wrong lifecycle entirely.
ready_doc_id = kb.create_document(db, TENANT_A, "ready.md", "# Ready\n\nContent.", uploaded_by="test")
kb.set_document_status(db, ready_doc_id, "ready")
assert kb.reject_pending_document(db, TENANT_A, ready_doc_id) is False

# ─── approve_pending_document: ingests, resolves the gap ────────────────────
approve_gap = kb.record_gap(db, TENANT_A, "Approve me", "low_coverage", 0.1)
kb.set_gap_status(db, approve_gap, "researching")
approve_doc_id = kb.create_document(
    db, TENANT_A,
    "research-approve.md",
    "# Research: Approve me\n\n## Root Cause\nA genuinely retrievable sentence about "
    "widget latency during failover, long enough to survive chunking.\n",
    uploaded_by="web research", status="pending_approval", gap_id=approve_gap,
)
ok = asyncio.run(kb.approve_pending_document(db, TENANT_A, approve_doc_id, "bob@example.com"))
assert ok is True
approved_doc = kb.get_document(db, approve_doc_id)
assert approved_doc["status"] == "ready", approved_doc
assert kb.get_gap(db, TENANT_A, approve_gap)["status"] == "resolved", (
    "approving a pending document must resolve the gap it came from"
)
audit = kb.list_audit(db, TENANT_A)
assert any(a["action"] == "pending_document_approved" and a["subject_id"] == approve_doc_id
           and a["detail"] == f"from gap {approve_gap}" for a in audit), audit
# The approved content is now genuinely retrievable, like any uploaded document.
results = kb.search(db, TENANT_A, "widget latency during failover", top_k=3)["results"]
assert any(r["document_title"] for r in results), results

# Approving an already-approved (now 'ready') document again is a no-op failure.
assert asyncio.run(kb.approve_pending_document(db, TENANT_A, approve_doc_id, "bob@example.com")) is False

# Cross-tenant approve must not touch tenant A's document.
cross_gap = kb.record_gap(db, TENANT_A, "Cross tenant", "low_coverage", 0.1)
cross_doc_id = kb.create_document(
    db, TENANT_A, "research-cross.md", "# Research: Cross tenant\n\nDraft.",
    uploaded_by="web research", status="pending_approval", gap_id=cross_gap,
)
assert asyncio.run(kb.approve_pending_document(db, TENANT_B, cross_doc_id, "eve@example.com")) is False
assert kb.get_document(db, cross_doc_id)["status"] == "pending_approval", (
    "must not ingest another tenant's pending document"
)

# A document with no usable text ends 'failed', not 'ready' -- and its gap must
# stay 'researching' (not resolved), so the admin sees the failure via the
# document's own status and can reject it (reopening the gap) rather than the
# gap silently reading as answered when nothing actually entered the knowledge
# base.
empty_gap = kb.record_gap(db, TENANT_A, "Empty content", "low_coverage", 0.1)
kb.set_gap_status(db, empty_gap, "researching")
empty_doc_id = kb.create_document(
    db, TENANT_A, "research-empty.md", "", uploaded_by="web research",
    status="pending_approval", gap_id=empty_gap,
)
empty_ok = asyncio.run(kb.approve_pending_document(db, TENANT_A, empty_doc_id, "carol@example.com"))
assert empty_ok is False, "approval must report failure when the document produces no usable text"
assert kb.get_document(db, empty_doc_id)["status"] == "failed"
assert kb.get_gap(db, TENANT_A, empty_gap)["status"] == "researching", (
    "a failed ingest must not resolve the gap -- nothing real entered the knowledge base"
)

# ─── kb_report._resolve_web_citations: same "can't cite what wasn't shown" ──
# guard as kb.resolve_citations, applied to (web: <url>) tags ───────────────
available = {"https://docs.aws.amazon.com/real-page"}
text_with_real_and_fake = (
    "This is documented (web: https://docs.aws.amazon.com/real-page) and also "
    "claims something from (web: https://not-actually-retrieved.example.com)."
)
resolved = kb_report._resolve_web_citations(text_with_real_and_fake, available)
assert resolved == ["https://docs.aws.amazon.com/real-page"], (
    "a citation to a URL that wasn't actually retrieved must not resolve", resolved
)

# ─── generate_report_from_web: deterministic in this credential-less test ───
# environment (ModelClient().available is reliably False), same standard
# test_kb_report.py already holds the KB-search version to -- real network
# search still runs (against the actually-configured Tavily key, if any),
# but synthesis stops at the same "no model configured" gate before ever
# calling the model, so the report comes back in its default empty shape.
report = asyncio.run(kb_report.generate_report_from_web("a niche, unlikely-to-matter test topic"))
assert report["topic"] == "a niche, unlikely-to-matter test topic"
assert all(report[f] in ("", []) for f in ("summary", "impact", "root_cause", "detection"))

# ─── run_gap_research: always produces a reviewable document, even when web ─
# search/synthesis found nothing (no model configured in this environment) ──
research_gap_id = kb.record_gap(db, TENANT_A, "Run gap research end to end", "low_coverage", 0.1)
kb.set_gap_status(db, research_gap_id, "researching")
asyncio.run(kb_report.run_gap_research(db, TENANT_A, research_gap_id, "Run gap research end to end", "dave@example.com"))
research_docs = [d for d in kb.list_documents(db, TENANT_A) if d.get("gap_id") == research_gap_id]
assert len(research_docs) == 1, research_docs
assert research_docs[0]["status"] == "pending_approval", (
    "run_gap_research must create a reviewable pending document even when nothing "
    "was found, so the admin has something to explicitly reject rather than the gap "
    "silently sitting stuck in 'researching' with no visible next step"
)

# ─── run_gap_research: an actual exception reverts the gap to 'open' ────────
# rather than leaving it stuck in 'researching' with nothing to show ─────────
broken_gap_id = kb.record_gap(db, TENANT_A, "This one will explode", "low_coverage", 0.1)
kb.set_gap_status(db, broken_gap_id, "researching")


async def _boom(*_a, **_k):
    raise RuntimeError("synthesis blew up")


original_generate = kb_report.generate_report_from_web
kb_report.generate_report_from_web = _boom
try:
    asyncio.run(kb_report.run_gap_research(db, TENANT_A, broken_gap_id, "This one will explode", "dave@example.com"))
finally:
    kb_report.generate_report_from_web = original_generate

assert kb.get_gap(db, TENANT_A, broken_gap_id)["status"] == "open", (
    "an actual exception during research must revert the gap to 'open' so the user "
    "can just try again, not leave it permanently stuck in 'researching'"
)
assert not [d for d in kb.list_documents(db, TENANT_A) if d.get("gap_id") == broken_gap_id], (
    "no document should be left behind when research itself raised"
)

print("ok: get_gap tenant-scoped, reject_gap dismisses without a document, "
      "pending_approval documents are created eagerly but never auto-ingested, "
      "reject_pending_document deletes + reopens the gap, approve_pending_document "
      "ingests + resolves the gap (and does NOT resolve it on an ingest failure), "
      "cross-tenant approve/reject rejected, _resolve_web_citations rejects an "
      "unretrieved URL, generate_report_from_web behaves deterministically with no "
      "model configured, and run_gap_research always leaves something reviewable "
      "behind, reverting the gap to open on an actual exception")
