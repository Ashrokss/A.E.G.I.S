"""
Knowledge base: user-uploaded documents (runbooks, standards, postmortems),
chunked and embedded so the KB chat (kb_chat_agent.py) can retrieve them.

Hybrid retrieval — cosine similarity over Bedrock embeddings plus BM25 keyword
ranking, fused by reciprocal rank — as plain Python and raw SQL against the
same runs.db SQLite file the agent uses for everything else: no vector
database and no ORM. Only _search_vectors is storage-specific, so moving to
Postgres + pgvector later is a contained change.
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime

from rank_bm25 import BM25Okapi

from utils.embeddings_client import EmbeddingsModelClient, _STOPWORDS
from utils.model_client import ModelClient

logger = logging.getLogger("uvicorn.error")

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120
MAX_TOOL_OUTPUT_CHARS = 20_000  # same output budget aws_observability.py clamps to

# Below this top semantic-similarity score, a question is treated as a
# knowledge gap (see record_gap) — matches rca/router.py's COVERAGE_FLOOR.
COVERAGE_FLOOR = 0.35
# A crude hashing-trick bag-of-words vector (no real embeddings configured)
# sits on a much lower absolute cosine scale than a real embedding model does,
# even for a genuine match -- reusing COVERAGE_FLOOR against it means almost
# everything reads as "irrelevant". Empirically recalibrated for that scale
# (see the empirical check run while fixing this): irrelevant pairs cluster
# under ~0.13, relevant pairs land at ~0.20+ once stopwords are filtered.
# Mock mode is a crude approximation by design (utils/embeddings_client.py is
# explicit about this) -- configure
# BEDROCK_EMBED_MODEL for real semantic matching rather than tuning this
# further; it will never perfectly separate relevant from irrelevant.
MOCK_COVERAGE_FLOOR = 0.15

STATUSES = ("processing", "ready", "failed", "pending_approval")
GAP_STATUSES = ("open", "researching", "resolved")

_SECTION_RE = re.compile(r"^##\s+(.*)$", re.MULTILINE)
_TITLE_RE = re.compile(r"^#\s+(.*)$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")  # keeps hyphenated ids (INC-2025-0101) as one token
_CITATION_RE = re.compile(r"\[([^\]·]+)·([^\]]+)\]")
_AGGREGATE_RE = re.compile(
    r"\b(how many|count|list all|list every|every|all of the|total|totals|"
    r"longest|shortest|most|fewest|average|across all|each of)\b",
    re.IGNORECASE,
)
# YAML frontmatter, as the seed RCA corpus's own RCA docs use it
# (--- ... ---). Not a full YAML parser -- no nesting, no multi-line scalars,
# just enough to read this corpus's flat `key: value` / `key: ["a", "b"]`
# fields (doc_id, services, severity, cost_usd, ...) without a new dependency.
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_FRONTMATTER_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*)$")
_FRONTMATTER_LIST_ITEM_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'')


def _utc_iso() -> str:
    return datetime.utcnow().isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table(db_path: str) -> None:
    """Create the knowledge-base tables if absent. Called from main.init_db(),
    same as sre_store.ensure_table()."""
    conn = _connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_documents (
            id          TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL,
            filename    TEXT NOT NULL,
            title       TEXT,
            uploaded_by TEXT,
            uploaded_at TIMESTAMP NOT NULL,
            status      TEXT NOT NULL DEFAULT 'processing',
            error       TEXT,
            raw_text    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_documents_tenant ON kb_documents(tenant_id)"
    )
    # Additive per-column migrations, same idempotent shape as
    # sre_store.ensure_table()'s. cite_key: the short tag ("[cite_key ·
    # section]") a citation in a generated answer names — computed once at
    # upload time, not derived from `title` at read time, so it never drifts
    # out from under chunks embedded against the original value.
    for ddl in (
        "ALTER TABLE kb_documents ADD COLUMN cite_key TEXT",
        "ALTER TABLE kb_documents ADD COLUMN metadata TEXT",
        # Set only for a document created by "Perform Research" on a
        # knowledge gap (status starts 'pending_approval', never auto-
        # ingested) -- lets approve/reject resolve or reopen the gap that
        # spawned it. NULL for every other document (upload, seed, or a
        # promoted chat research candidate).
        "ALTER TABLE kb_documents ADD COLUMN gap_id TEXT",
        # Set only for the detailed RCA document an investigation generates
        # from its own published submit_rca (tools/rca_document.py). Also
        # created 'pending_approval' and never auto-ingested: an operator
        # reviews it in the chat (or the RCA docs tab) and approving is what
        # runs the same chunk/embed pipeline an upload goes through, which is
        # how an incident's own analysis becomes queryable knowledge. NULL for
        # every other document.
        "ALTER TABLE kb_documents ADD COLUMN incident_id TEXT",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    # Service dependency graph -- same shape as the seed RCA
    # corpus's okf/services/*.md (id/name/aliases/depends_on), except
    # depends_on is grown automatically (see extract_dependency_edges /
    # record_dependency) instead of a human authoring it in PR review, per
    # the "no manual curation" rule already applied to kb_patterns.
    # depends_on_draft holds edges discovered mid-investigation, invisible to
    # downstream_of() until validate_service_dependencies_for_incident
    # promotes them on incident resolution -- the same draft/validated split
    # kb_patterns uses, for the same reason.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_services (
            tenant_id        TEXT NOT NULL,
            id               TEXT NOT NULL,
            name             TEXT,
            aliases          TEXT NOT NULL DEFAULT '[]',
            depends_on       TEXT NOT NULL DEFAULT '[]',
            depends_on_draft TEXT NOT NULL DEFAULT '[]',
            owned_by         TEXT NOT NULL DEFAULT 'auto-extracted',
            updated_at       TIMESTAMP NOT NULL,
            PRIMARY KEY (tenant_id, id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_chunks (
            id              TEXT PRIMARY KEY,
            document_id     TEXT NOT NULL,
            tenant_id       TEXT NOT NULL,
            chunk_index     INTEGER NOT NULL,
            section_title   TEXT,
            text            TEXT NOT NULL,
            embedding       TEXT NOT NULL,
            embedding_model TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_chunks_tenant ON kb_chunks(tenant_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_chunks_document ON kb_chunks(document_id)"
    )
    # Knowledge gaps: a question the knowledge base couldn't confidently
    # answer. Upserted by (tenant_id, normalized question) so the same
    # question from different users collapses into one row instead of
    # flooding the gap list with duplicates.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_gaps (
            id                  TEXT PRIMARY KEY,
            tenant_id           TEXT NOT NULL,
            question            TEXT NOT NULL,
            question_normalized TEXT NOT NULL,
            reason              TEXT NOT NULL,
            best_score          REAL,
            hit_count           INTEGER NOT NULL DEFAULT 1,
            status              TEXT NOT NULL DEFAULT 'open',
            first_seen          TIMESTAMP NOT NULL,
            last_seen           TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_gaps_tenant ON kb_gaps(tenant_id, status)"
    )
    # Automatically-learned incident patterns (replaces a hand-curated
    # failure-mode library): a pattern is drafted from an incident's own
    # published RCA, then flipped to `validated` only when that incident is
    # later marked RESOLVED — see incident_symptom_summary/create_pattern/
    # validate_patterns_for_incident. Matching (find_matching_pattern) only
    # ever considers `validated` rows, so an unresolved incident's guess can
    # never short-circuit a future one.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_patterns (
            id                  TEXT PRIMARY KEY,
            tenant_id           TEXT NOT NULL,
            incident_id         TEXT NOT NULL,
            symptom_summary     TEXT NOT NULL,
            root_cause          TEXT,
            remediation         TEXT,
            signature_embedding TEXT NOT NULL,
            embedding_model     TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'draft',
            created_at          TIMESTAMP NOT NULL,
            validated_at        TIMESTAMP,
            validation_source   TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_patterns_tenant ON kb_patterns(tenant_id, status)"
    )
    # Web-research candidates awaiting tenant admin approval before
    # anything sourced from the web becomes retrievable knowledge — see
    # create_research_candidate / approve_research_candidate /
    # reject_research_candidate.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_research_candidates (
            id                  TEXT PRIMARY KEY,
            tenant_id           TEXT NOT NULL,
            gap_id              TEXT,
            question            TEXT NOT NULL,
            consolidated_answer TEXT NOT NULL,
            doc_citations       TEXT NOT NULL,
            web_citations       TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending_approval',
            created_by          TEXT,
            created_at          TIMESTAMP NOT NULL,
            reviewed_by         TEXT,
            reviewed_at         TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_candidates_tenant "
        "ON kb_research_candidates(tenant_id, status)"
    )
    # Append-only audit trail for anything that changes what the knowledge
    # base knows outside a plain document upload — a candidate approval or
    # rejection today, room for more actions later without a schema change.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_audit (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id  TEXT NOT NULL,
            actor      TEXT NOT NULL,
            action     TEXT NOT NULL,
            subject_id TEXT,
            detail     TEXT,
            ts         TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_audit_tenant ON kb_audit(tenant_id, ts DESC)"
    )
    conn.commit()
    conn.close()


# ─── Documents ──────────────────────────────────────────────────────────────

def _parse_frontmatter_value(raw: str):
    raw = raw.strip()
    if raw == "" or raw.lower() == "null" or raw == "~":
        return None
    if raw.startswith("[") and raw.endswith("]"):
        items = _FRONTMATTER_LIST_ITEM_RE.findall(raw[1:-1])
        if items:
            return [a or b for a, b in items]
        return [v.strip() for v in raw[1:-1].split(",") if v.strip()]
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _parse_frontmatter_fields(block: str) -> dict:
    """Every flat `key: value` pair a frontmatter block declares -- lists
    (`["a", "b"]`), quoted/bare strings, numbers, and null. Deliberately not a
    general YAML parser: no nesting, no multi-line scalars, just this corpus's
    flat doc_id/services/severity/cost_usd-style fields. A `#`-prefixed line
    is a comment, not a field."""
    fields: dict = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _FRONTMATTER_FIELD_RE.match(stripped)
        if not m:
            continue
        fields[m.group(1)] = _parse_frontmatter_value(m.group(2))
    return fields


def _split_frontmatter(raw_text: str) -> tuple[str | None, dict, str]:
    """(title_from_frontmatter, metadata, body_without_frontmatter) -- title
    is None, metadata is {}, and body is raw_text unchanged when there is no
    frontmatter block. metadata is every flat field the block declares
    (services, severity, cost_usd, ... -- whatever is present); tenant
    uploads with no frontmatter at all simply get {}."""
    m = _FRONTMATTER_RE.match(raw_text)
    if not m:
        return None, {}, raw_text
    metadata = _parse_frontmatter_fields(m.group(1))
    title = metadata.get("title")
    title = title.strip() if isinstance(title, str) else None
    return title, metadata, raw_text[m.end():]


def create_document(
    db_path: str, tenant_id: str, filename: str, raw_text: str, uploaded_by: str | None,
    status: str = "processing", gap_id: str | None = None, incident_id: str | None = None,
) -> str:
    """status/gap_id/incident_id default to the normal upload/seed path
    (created 'processing', ingested moments later). Two callers pass
    status='pending_approval' instead, each with the id of whatever it came
    from: a knowledge-gap "Perform Research" result (gap_id) and an
    investigation's own detailed RCA document (incident_id). Both are created
    eagerly so there is something to review, but deliberately NOT queued for
    ingest_document until someone approves -- approve_pending_document runs
    the same chunk/embed pipeline a normal upload does, just gated behind
    that decision."""
    if status not in STATUSES:
        raise ValueError(f"unknown document status {status!r}")
    frontmatter_title, metadata, body = _split_frontmatter(raw_text)
    title = frontmatter_title or _extract_title(body, filename)
    cite_key = _make_cite_key(title, filename)
    doc_id = str(uuid.uuid4())
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO kb_documents (id, tenant_id, filename, title, cite_key, uploaded_by, "
        "uploaded_at, status, raw_text, metadata, gap_id, incident_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (doc_id, tenant_id, filename, title, cite_key, uploaded_by, _utc_iso(), status, body,
         json.dumps(metadata) if metadata else None, gap_id, incident_id),
    )
    conn.commit()
    conn.close()
    return doc_id


def get_incident_document(db_path: str, tenant_id: str, incident_id: str) -> dict | None:
    """The most recently generated RCA document for this incident, whatever
    its state (pending_approval awaiting review, ready once approved, failed).
    Rejecting deletes the row outright, so "none" genuinely means there is
    nothing to review -- which is what lets the chat card offer Regenerate."""
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM kb_documents WHERE tenant_id = ? AND incident_id = ? "
        "ORDER BY uploaded_at DESC LIMIT 1",
        (tenant_id, incident_id),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _document_metadata(doc: dict) -> dict:
    """Parsed metadata for a kb_documents row -- {} when the document has
    none (a plain tenant upload before dependency extraction has run, or one
    that predates this column)."""
    raw = doc.get("metadata")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _set_document_metadata(db_path: str, doc_id: str, metadata: dict) -> None:
    conn = _connect(db_path)
    conn.execute("UPDATE kb_documents SET metadata = ? WHERE id = ?", (json.dumps(metadata), doc_id))
    conn.commit()
    conn.close()


def get_document(db_path: str, doc_id: str) -> dict | None:
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_documents(db_path: str, tenant_id: str) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT d.*, (SELECT COUNT(*) FROM kb_chunks c WHERE c.document_id = d.id) AS chunk_count "
        "FROM kb_documents d WHERE d.tenant_id = ? ORDER BY d.uploaded_at DESC",
        (tenant_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


async def seed_documents_from_directory(db_path: str, tenant_id: str, seed_dir: str, seeded_by: str) -> int:
    """Idempotently ingest every .md/.txt file in seed_dir as a document owned
    by tenant_id, skipping filenames already present for that scope -- so
    calling this on every startup (the intended use, see main.py) never
    re-seeds or duplicates the corpus after the first successful run. Async
    (not just asyncio.to_thread'd sync code) because dependency-graph
    extraction needs a live model call per newly-seeded document — the
    blocking chunk/embed step itself is still off-thread.
    Returns how many new documents were created."""
    if not os.path.isdir(seed_dir):
        return 0
    existing_filenames = {d["filename"] for d in list_documents(db_path, tenant_id)}
    created = 0
    for filename in sorted(os.listdir(seed_dir)):
        if not filename.lower().endswith((".md", ".txt")) or filename in existing_filenames:
            continue
        path = os.path.join(seed_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as ex:
            logger.warning("[kb] could not read seed file %s: %s", filename, ex)
            continue
        if not text.strip():
            continue
        doc_id = create_document(db_path, tenant_id, filename, text, uploaded_by=seeded_by)
        try:
            await asyncio.to_thread(_ingest_document_sync, db_path, doc_id)
            created += 1
        except Exception as ex:
            logger.warning("[kb] seeding failed for %s: %s", filename, ex)
            set_document_status(db_path, doc_id, "failed", error=str(ex)[:500])
            continue
        try:
            await learn_service_dependencies(db_path, doc_id)
        except Exception as ex:
            logger.warning("[kb] dependency extraction failed for %s: %s", filename, ex)
    return created


def delete_document(db_path: str, tenant_id: str, doc_id: str) -> bool:
    """False if no matching document for this tenant — the caller 404s rather
    than leaking whether a document exists for a different tenant."""
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT id FROM kb_documents WHERE id = ? AND tenant_id = ?", (doc_id, tenant_id)
    ).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute("DELETE FROM kb_chunks WHERE document_id = ?", (doc_id,))
    conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()
    return True


def set_document_status(db_path: str, doc_id: str, status: str, error: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown document status {status!r}")
    conn = _connect(db_path)
    conn.execute(
        "UPDATE kb_documents SET status = ?, error = ? WHERE id = ?", (status, error, doc_id)
    )
    conn.commit()
    conn.close()


def _extract_title(raw_text: str, filename: str) -> str:
    m = _TITLE_RE.search(raw_text)
    return m.group(1).strip() if m else filename


def _make_cite_key(title: str, filename: str) -> str:
    """The short tag a citation names: the title when it's citation-length,
    otherwise the filename stem — mirrors rca/ingest.py::doc_label(), which
    exists because a long document title makes an unreadable `[... · ...]`
    tag."""
    candidate = (title or "").strip()
    if candidate and len(candidate) <= 60:
        return candidate
    stem = filename.rsplit(".", 1)[0] if filename else "doc"
    return stem[:60] or "doc"


# ─── Chunking ───────────────────────────────────────────────────────────────

def chunk_text(raw_text: str) -> list[dict]:
    """Two-pass split: `##` section headers first (a chunk never crosses a
    section, so a citation can name it), then size-split any section still
    over CHUNK_SIZE with CHUNK_OVERLAP."""
    chunks = []
    for section_title, body in _split_sections(raw_text):
        for piece in _split_size(body):
            if piece:
                chunks.append({"section_title": section_title, "text": piece})
    return chunks


def _split_sections(raw_text: str) -> list[tuple[str | None, str]]:
    matches = list(_SECTION_RE.finditer(raw_text))
    if not matches:
        return [(None, raw_text)]
    sections = []
    if matches[0].start() > 0:
        sections.append((None, raw_text[: matches[0].start()]))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw_text)
        sections.append((m.group(1).strip(), raw_text[start:end]))
    return sections


def _split_size(text: str) -> list[str]:
    """Sliding window with a whitespace-aware boundary. A flat character cut
    at CHUNK_SIZE can land mid-word, and (the bug this guards against) a
    naive `next_start = end - overlap` can advance by only a handful of
    characters once `end` has already been backed up to a word boundary —
    turning a modest corpus into thousands of near-duplicate chunks. Backing
    the window up to the nearest space, advancing by at least
    `max(window - overlap, 1)`, and skipping the continuation start forward
    to the next word boundary keeps both the window size and the advance
    honest."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= CHUNK_SIZE:
        return [text]

    pieces: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        if end < n:
            break_at = text.rfind(" ", start, end)
            if break_at > start:
                end = break_at
        piece = text[start:end].strip()
        if piece:
            pieces.append(piece)
        if end >= n:
            break

        window = end - start
        next_start = start + max(window - CHUNK_OVERLAP, 1)
        while next_start < n and text[next_start - 1] not in " \n\t":
            next_start += 1
        start = next_start
    return pieces


# ─── Ingestion ──────────────────────────────────────────────────────────────

def _search_text(cite_key: str, section_title: str | None, text: str) -> str:
    """What actually gets embedded — not the raw chunk text. Identity-prefixed
    so a document that barely repeats its own title/cite_key inside a chunk's
    body still ranks well for a query naming it directly."""
    return f"[{cite_key} · {section_title or 'Document'}]\n{text}"


async def ingest_document(db_path: str, doc_id: str) -> None:
    """Background-task entrypoint: chunk, embed and store, or record a failure.
    Wrapped in asyncio.to_thread since sqlite3 and the embeddings SDK call are
    both synchronous — same pattern aws_observability.py uses for boto3."""
    try:
        await asyncio.to_thread(_ingest_document_sync, db_path, doc_id)
    except Exception as ex:
        logger.exception("[kb] ingest failed for %s", doc_id)
        try:
            set_document_status(db_path, doc_id, "failed", error=str(ex)[:500])
        except Exception:
            pass
        return
    try:
        await learn_service_dependencies(db_path, doc_id)
    except Exception as ex:
        logger.warning("[kb] dependency extraction failed for %s: %s", doc_id, ex)


def _ingest_document_sync(db_path: str, doc_id: str) -> None:
    doc = get_document(db_path, doc_id)
    if not doc:
        return
    pieces = chunk_text(doc["raw_text"])
    if not pieces:
        set_document_status(db_path, doc_id, "failed", error="Document produced no usable text.")
        return

    cite_key = doc.get("cite_key") or doc["title"] or doc["filename"]
    search_texts = [_search_text(cite_key, p["section_title"], p["text"]) for p in pieces]

    embedder = EmbeddingsModelClient()
    vectors, model_label = embedder.embed_many(search_texts)

    conn = _connect(db_path)
    conn.execute("DELETE FROM kb_chunks WHERE document_id = ?", (doc_id,))
    for i, (piece, vector) in enumerate(zip(pieces, vectors)):
        conn.execute(
            "INSERT INTO kb_chunks (id, document_id, tenant_id, chunk_index, "
            "section_title, text, embedding, embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), doc_id, doc["tenant_id"], i, piece["section_title"],
             piece["text"], json.dumps(vector), model_label),
        )
    conn.commit()
    conn.close()
    set_document_status(db_path, doc_id, "ready")


# ─── Service dependency graph ──────────────────────────────────────────────
# Auto-grown equivalent of the seed RCA corpus's okf/services/*.md
# depends_on graph. That graph is explicitly hand-curated (every file there
# carries `owned_by: "TBD - set in review"`); this one is not. It grows two
# ways, matching the trust level each source already has elsewhere in this
# module:
#   1. Every ingested document (seeded, uploaded, or promoted from approved
#      research) is read once for dependency relationships and the result
#      goes straight into the trusted graph — the document itself is already
#      trusted content (a human chose to add it, or an admin already
#      approved it), so there is nothing left to validate.
#   2. Relationships surfacing mid-investigation (sre_agent.py reading its
#      own live AWS tool output or approved web research) land in
#      depends_on_draft instead, and only reach the trusted graph if that
#      incident is later resolved (validate_service_dependencies_for_incident)
#      — the same resolution-is-the-validation-signal rule kb_patterns uses.

_DEPENDENCY_EXTRACTION_PROMPT = (
    "You extract AWS service dependency relationships from one incident report. "
    "A dependency means: the first service REQUIRES the second to function, so if the "
    "second fails or degrades, the first is directly affected (e.g. an ALB depends on the "
    "EC2/ECS targets behind it and the ACM certificate on its listener; a Lambda function "
    "that queries a database depends on that database). "
    "Identify services by short lowercase identifiers such as alb, ec2, ecs, rds, lambda, "
    "s3, dynamodb, api-gateway, cloudfront, elasticache, sqs, sns, vpc, iam, kms, acm, "
    "route-53, eventbridge, step-functions, natgw, asg — or another short lowercase id if "
    "the text names a service not in that list. "
    "Respond with ONLY a JSON array (no prose, no markdown code fences). Each item: "
    '{"service": "<id>", "depends_on": "<id>", "evidence": "<one short quote from the text>"}. '
    "Only include relationships the text actually supports — never guess or use outside "
    "knowledge about what a service usually depends on. If the text supports none, respond "
    "with exactly []."
)


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


async def extract_dependency_edges(mc: ModelClient, title: str, body: str) -> list[dict]:
    """One incident's text -> [{service, depends_on, evidence}, ...]. Returns
    [] on any failure (no model configured, malformed JSON, transient error)
    — dependency-graph growth is a bonus on top of a document that is already
    usable without it, never a reason to fail ingestion."""
    if not mc.available:
        return []
    messages = [
        {"role": "system", "content": _DEPENDENCY_EXTRACTION_PROMPT},
        {"role": "user", "content": f"### {title} ###\n\n{body[:6000]}"},
    ]
    try:
        response = await mc.complete(messages, model=mc.model_chat)
        raw = _strip_code_fence((response.choices[0].message.content or ""))
        edges = json.loads(raw)
    except Exception as ex:
        logger.warning("[kb] dependency extraction call failed: %s", ex)
        return []
    if not isinstance(edges, list):
        return []
    cleaned = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        service = str(e.get("service", "")).strip().lower()
        depends_on = str(e.get("depends_on", "")).strip().lower()
        if service and depends_on and service != depends_on:
            cleaned.append({
                "service": service, "depends_on": depends_on,
                "evidence": str(e.get("evidence", "") or "")[:300],
            })
    return cleaned


async def learn_service_dependencies(db_path: str, doc_id: str) -> None:
    """Read one already-ingested document for dependency relationships and
    fold them straight into the trusted graph, then backfill the document's
    own services metadata if it did not already have one (frontmatter never
    carries anything but a flat services list, and a plain upload or a
    promoted research answer usually carries no frontmatter at all) — so
    every document ends up in the same shape regardless of where it came
    from."""
    doc = get_document(db_path, doc_id)
    if not doc:
        return
    mc = ModelClient()
    edges = await extract_dependency_edges(mc, doc.get("title") or doc["filename"], doc["raw_text"])
    if not edges:
        return
    metadata = _document_metadata(doc)
    if not metadata.get("services"):
        services = sorted({e["service"] for e in edges} | {e["depends_on"] for e in edges})
        metadata["services"] = services
        await asyncio.to_thread(_set_document_metadata, db_path, doc_id, metadata)
    for e in edges:
        await asyncio.to_thread(
            record_dependency, db_path, doc["tenant_id"], e["service"], e["depends_on"],
            validated=True, source_document_id=doc_id, evidence=e["evidence"],
        )


def _ensure_service_row(conn: sqlite3.Connection, tenant_id: str, service_id: str) -> None:
    existing = conn.execute(
        "SELECT 1 FROM kb_services WHERE tenant_id = ? AND id = ?", (tenant_id, service_id)
    ).fetchone()
    if existing:
        return
    conn.execute(
        "INSERT INTO kb_services (tenant_id, id, name, aliases, depends_on, depends_on_draft, "
        "owned_by, updated_at) VALUES (?, ?, ?, '[]', '[]', '[]', 'auto-extracted', ?)",
        (tenant_id, service_id, service_id, _utc_iso()),
    )


def record_dependency(
    db_path: str, tenant_id: str, service_id: str, depends_on_id: str, *, validated: bool,
    source_incident_id: str | None = None, source_document_id: str | None = None,
    evidence: str | None = None,
) -> None:
    """Record one `service_id depends_on depends_on_id` fact.

    validated=True (an already-ingested document — bulk-seeded, uploaded, or
    promoted from approved research) writes straight into the live
    depends_on list, the same trust level the rest of that document already
    has. validated=False (discovered mid-investigation, before the incident
    itself has resolved) goes into depends_on_draft instead, invisible to
    downstream_of() until validate_service_dependencies_for_incident promotes
    it."""
    conn = _connect(db_path)
    _ensure_service_row(conn, tenant_id, service_id)
    _ensure_service_row(conn, tenant_id, depends_on_id)
    row = conn.execute(
        "SELECT depends_on, depends_on_draft FROM kb_services WHERE tenant_id = ? AND id = ?",
        (tenant_id, service_id),
    ).fetchone()
    validated_ids = json.loads(row["depends_on"])
    now = _utc_iso()
    if validated:
        if depends_on_id not in validated_ids:
            validated_ids.append(depends_on_id)
            conn.execute(
                "UPDATE kb_services SET depends_on = ?, updated_at = ? WHERE tenant_id = ? AND id = ?",
                (json.dumps(validated_ids), now, tenant_id, service_id),
            )
    else:
        draft_entries = json.loads(row["depends_on_draft"])
        already_draft = any(
            d["id"] == depends_on_id and d.get("source_incident_id") == source_incident_id
            for d in draft_entries
        )
        if depends_on_id not in validated_ids and not already_draft:
            draft_entries.append({
                "id": depends_on_id, "source_incident_id": source_incident_id,
                "source_document_id": source_document_id, "evidence": evidence, "first_seen": now,
            })
            conn.execute(
                "UPDATE kb_services SET depends_on_draft = ?, updated_at = ? WHERE tenant_id = ? AND id = ?",
                (json.dumps(draft_entries), now, tenant_id, service_id),
            )
    conn.commit()
    conn.close()


def validate_service_dependencies_for_incident(db_path: str, tenant_id: str, incident_id: str) -> int:
    """Promote every draft dependency this incident's own investigation
    surfaced into the trusted graph. Called from main.py's resolve_incident,
    the same validation signal validate_patterns_for_incident uses. Returns
    how many edges were promoted."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT id, depends_on, depends_on_draft FROM kb_services WHERE tenant_id = ?", (tenant_id,)
    ).fetchall()
    promoted = 0
    for row in rows:
        draft_entries = json.loads(row["depends_on_draft"])
        if not draft_entries:
            continue
        validated_ids = json.loads(row["depends_on"])
        keep_draft = []
        changed = False
        for entry in draft_entries:
            if entry.get("source_incident_id") == incident_id:
                if entry["id"] not in validated_ids:
                    validated_ids.append(entry["id"])
                changed = True
                promoted += 1
            else:
                keep_draft.append(entry)
        if changed:
            conn.execute(
                "UPDATE kb_services SET depends_on = ?, depends_on_draft = ?, updated_at = ? "
                "WHERE tenant_id = ? AND id = ?",
                (json.dumps(validated_ids), json.dumps(keep_draft), _utc_iso(), tenant_id, row["id"]),
            )
    conn.commit()
    conn.close()
    return promoted


def downstream_of(db_path: str, tenant_id: str, service_id: str) -> list[str]:
    """Every service that depends on service_id, directly or transitively,
    nearest-first — reverse breadth-first search over the trusted (validated)
    graph only, same algorithm as the POC's rag/routing/dependency_graph.py,
    tenant-scoped and DB-backed instead of file-based and process-global.
    Empty if service_id is unknown or nothing depends on it yet, which is a
    real answer, not an error: the graph is expected to lag which services
    actually exist."""
    conn = _connect(db_path)
    rows = conn.execute("SELECT id, depends_on FROM kb_services WHERE tenant_id = ?", (tenant_id,)).fetchall()
    conn.close()
    reverse: dict[str, list[str]] = {}
    for row in rows:
        for dep in json.loads(row["depends_on"]):
            reverse.setdefault(dep, []).append(row["id"])
    visited = {service_id}
    ordered: list[str] = []
    frontier = list(reverse.get(service_id, []))
    while frontier:
        next_frontier: list[str] = []
        for dependent in frontier:
            if dependent in visited:
                continue
            visited.add(dependent)
            ordered.append(dependent)
            next_frontier.extend(reverse.get(dependent, []))
        frontier = next_frontier
    return ordered


# ─── Gaps ───────────────────────────────────────────────────────────────────

_PUNCT_RE = re.compile(r"[^\w\s]")


def gap_id_for_question(tenant_id: str, question: str) -> str:
    """Deterministic id for a (tenant, question) pair — same scheme record_gap
    uses, exposed so a caller elsewhere (the research-candidate capture in
    sre_agent.py) can reopen the right gap on a rejection without re-deriving
    the hash independently."""
    return hashlib.sha1(f"{tenant_id}|{_normalize_question(question)}".encode("utf-8")).hexdigest()[:16]


def _normalize_question(question: str) -> str:
    """Lowercased, punctuation-blanked (not stripped — a space, so hyphenated
    words don't fuse into one token), whitespace-collapsed. A trailing "?" or
    stray double space must not mint a second gap row for the same question."""
    return " ".join(_PUNCT_RE.sub(" ", question.lower()).split())


def record_gap(db_path: str, tenant_id: str, question: str, reason: str, score: float | None) -> str:
    """Upsert-by-(tenant, normalized question): a repeat of the same question
    increments hit_count and refreshes last_seen rather than piling up
    duplicate rows."""
    gap_id = gap_id_for_question(tenant_id, question)
    now = _utc_iso()
    conn = _connect(db_path)
    existing = conn.execute("SELECT id FROM kb_gaps WHERE id = ?", (gap_id,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE kb_gaps SET hit_count = hit_count + 1, last_seen = ?, "
            "best_score = ?, reason = ? WHERE id = ?",
            (now, score, reason, gap_id),
        )
    else:
        conn.execute(
            "INSERT INTO kb_gaps (id, tenant_id, question, question_normalized, reason, "
            "best_score, hit_count, status, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, 'open', ?, ?)",
            (gap_id, tenant_id, question, _normalize_question(question), reason, score, now, now),
        )
    conn.commit()
    conn.close()
    return gap_id


def list_gaps(db_path: str, tenant_id: str, status: str | None = "open") -> list[dict]:
    conn = _connect(db_path)
    if status:
        rows = conn.execute(
            "SELECT * FROM kb_gaps WHERE tenant_id = ? AND status = ? ORDER BY last_seen DESC",
            (tenant_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM kb_gaps WHERE tenant_id = ? ORDER BY last_seen DESC", (tenant_id,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_gap_status(db_path: str, gap_id: str, status: str) -> None:
    if status not in GAP_STATUSES:
        raise ValueError(f"unknown gap status {status!r}")
    conn = _connect(db_path)
    conn.execute("UPDATE kb_gaps SET status = ? WHERE id = ?", (status, gap_id))
    conn.commit()
    conn.close()


def get_gap(db_path: str, tenant_id: str, gap_id: str) -> dict | None:
    """Tenant-scoped in the SQL, same convention as get_thread/delete_document
    -- a wrong tenant_id returns None rather than leaking whether the gap
    exists for someone else's tenant."""
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM kb_gaps WHERE id = ? AND tenant_id = ?", (gap_id, tenant_id)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def reject_gap(db_path: str, tenant_id: str, gap_id: str) -> bool:
    """Dismiss an open gap without researching it. Lower-stakes than
    reject_research_candidate (there is no generated answer here to have
    been wrong, just a question nobody has looked into), so this needs no
    reason. Only an 'open' gap can be rejected this way -- one already
    'researching' has a pending document to decide on instead (see
    reject_pending_document, which reopens the gap on its own reject path),
    and one already 'resolved' has nothing left to reject."""
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT id FROM kb_gaps WHERE id = ? AND tenant_id = ? AND status = 'open'",
        (gap_id, tenant_id),
    ).fetchone()
    if not row:
        conn.close()
        return False
    conn.execute("UPDATE kb_gaps SET status = 'resolved' WHERE id = ?", (gap_id,))
    conn.commit()
    conn.close()
    return True


# ─── Patterns (automatic learning) ─────────────────────────────────────────

PATTERN_STATUSES = ("draft", "validated")
PATTERN_MATCH_THRESHOLD = 0.90  # high bar: a match SKIPS investigation entirely


def incident_symptom_summary(row: dict) -> str:
    """Deterministic, alarm-level signature for an incident — built the same
    way whether this incident is becoming a new pattern (after its own RCA is
    published) or being checked against existing patterns (before any
    investigation has run), since only alarm metadata is available at match
    time. Deliberately excludes root-cause text: matching on the CAUSE would
    make this circular (we don't know the cause yet for the incident being
    matched); matching on what an operator actually observes is what lets a
    recurring failure be recognized before it's re-diagnosed."""
    parts = [
        f"alarm={row.get('alarm_name') or ''}",
        f"namespace={row.get('namespace') or ''}",
        f"metric={row.get('metric_name') or ''}",
        f"region={row.get('region') or ''}",
        f"state_reason={row.get('state_reason') or ''}",
    ]
    return " | ".join(parts)


def create_pattern(
    db_path: str, tenant_id: str, incident_id: str, symptom_summary: str,
    root_cause: str, remediation: str,
) -> str:
    """Draft a pattern from a just-published RCA. Starts as `draft` — see
    validate_patterns_for_incident for how it earns `validated`."""
    embedder = EmbeddingsModelClient()
    vectors, model_label = embedder.embed_many([symptom_summary])
    pattern_id = str(uuid.uuid4())
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO kb_patterns (id, tenant_id, incident_id, symptom_summary, root_cause, "
        "remediation, signature_embedding, embedding_model, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)",
        (pattern_id, tenant_id, incident_id, symptom_summary, root_cause, remediation,
         json.dumps(vectors[0]), model_label, _utc_iso()),
    )
    conn.commit()
    conn.close()
    return pattern_id


def validate_patterns_for_incident(
    db_path: str, tenant_id: str, incident_id: str, validation_source: str
) -> int:
    """The validation signal: called when an incident is marked RESOLVED.
    Returns how many draft patterns were flipped (0 or 1 in practice — one
    incident normally drafts one pattern — but not assumed)."""
    conn = _connect(db_path)
    now = _utc_iso()
    cur = conn.execute(
        "UPDATE kb_patterns SET status = 'validated', validated_at = ?, validation_source = ? "
        "WHERE tenant_id = ? AND incident_id = ? AND status = 'draft'",
        (now, validation_source, tenant_id, incident_id),
    )
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


def find_matching_pattern(
    db_path: str, tenant_id: str, signature_vector: list[float],
    threshold: float = PATTERN_MATCH_THRESHOLD,
) -> dict | None:
    """Cosine-match against this tenant's VALIDATED patterns only — a draft
    pattern (from an incident nobody has confirmed is actually resolved yet)
    must never short-circuit a different incident's investigation."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM kb_patterns WHERE tenant_id = ? AND status = 'validated'", (tenant_id,)
    ).fetchall()
    conn.close()

    best = None
    best_score = threshold
    for row in rows:
        score = _cosine(signature_vector, json.loads(row["signature_embedding"]))
        if score >= best_score:
            best_score = score
            best = dict(row)
    if best:
        best["match_score"] = round(best_score, 4)
    return best


def match_known_pattern(db_path: str, tenant_id: str, row: dict) -> dict | None:
    """Embed a fresh incident's symptom signature and check it against this
    tenant's validated patterns. Called once, at investigation start, before
    any tool budget is spent — this is the automatic, curation-free
    replacement for the POC's hand-authored known_pattern match."""
    summary = incident_symptom_summary(row)
    embedder = EmbeddingsModelClient()
    vectors, _ = embedder.embed_many([summary])
    return find_matching_pattern(db_path, tenant_id, vectors[0])


# ─── Audit ──────────────────────────────────────────────────────────────────

def add_audit(db_path: str, tenant_id: str, actor: str, action: str, subject_id: str | None, detail: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO kb_audit (tenant_id, actor, action, subject_id, detail, ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, actor, action, subject_id, detail, _utc_iso()),
    )
    conn.commit()
    conn.close()


def list_audit(db_path: str, tenant_id: str, limit: int = 200) -> list[dict]:
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT * FROM kb_audit WHERE tenant_id = ? ORDER BY ts DESC LIMIT ?",
        (tenant_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Research candidates (web-research approval queue) ─────────────────────

_WEB_URL_RE = re.compile(r"https?://[^\s)>\]]+")


def extract_citations(answer_text: str) -> tuple[list[str], list[str]]:
    """Best-effort split of a consolidated answer into doc-citation tags
    (`cite_key · section`) and raw web URLs, for display in the approval
    queue — not a strict validation pass like resolve_citations(); a human
    reviewer sees the full answer text regardless, so this only needs to be
    good enough to summarize at a glance."""
    doc_tags = [f"{m.group(1).strip()} · {m.group(2).strip()}" for m in _CITATION_RE.finditer(answer_text)]
    web_urls = list(dict.fromkeys(_WEB_URL_RE.findall(answer_text)))
    return doc_tags, web_urls


def create_research_candidate(
    db_path: str, tenant_id: str, gap_id: str | None, question: str, consolidated_answer: str,
    doc_citations: list[str], web_citations: list[str], created_by: str,
) -> str:
    candidate_id = str(uuid.uuid4())
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO kb_research_candidates (id, tenant_id, gap_id, question, consolidated_answer, "
        "doc_citations, web_citations, status, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?)",
        (candidate_id, tenant_id, gap_id, question, consolidated_answer,
         json.dumps(doc_citations), json.dumps(web_citations), created_by, _utc_iso()),
    )
    conn.commit()
    conn.close()
    return candidate_id


def get_research_candidate(db_path: str, candidate_id: str) -> dict | None:
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT * FROM kb_research_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_research_candidates(db_path: str, tenant_id: str, status: str = "pending_approval") -> list[dict]:
    conn = _connect(db_path)
    if status:
        rows = conn.execute(
            "SELECT * FROM kb_research_candidates WHERE tenant_id = ? AND status = ? "
            "ORDER BY created_at DESC",
            (tenant_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM kb_research_candidates WHERE tenant_id = ? ORDER BY created_at DESC",
            (tenant_id,),
        ).fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        d["doc_citations"] = json.loads(d["doc_citations"])
        d["web_citations"] = json.loads(d["web_citations"])
        results.append(d)
    return results


async def approve_research_candidate(db_path: str, tenant_id: str, candidate_id: str, reviewed_by: str) -> str | None:
    """Promotes the candidate into a first-class kb_documents row, ingested
    through the exact same chunk/embed pipeline an upload goes through, so it
    becomes retrievable like any other document — including dependency-graph
    extraction, so an admin approving a research answer grows the graph the
    same way an upload or a seeded doc does. Returns the new document id, or
    None if the candidate doesn't exist, isn't this tenant's, or was already
    reviewed — the caller 404s on None rather than guessing why."""
    candidate = get_research_candidate(db_path, candidate_id)
    if not candidate or candidate["tenant_id"] != tenant_id or candidate["status"] != "pending_approval":
        return None

    question = (candidate.get("question") or "").strip()
    answer = candidate["consolidated_answer"]
    # Only prepend a heading if the answer doesn't already have one -- some
    # candidates (sre_agent.py's RCA-shaped answers) already start with their
    # own, more specific "# ..." heading, and _extract_title takes whichever
    # H1 appears FIRST. Prepending unconditionally would shadow a better
    # existing title with the raw question every time.
    content = f"# {question}\n\n{answer}" if question and not answer.lstrip().startswith("#") else answer
    doc_id = create_document(
        db_path, tenant_id, f"web-research-{candidate_id[:8]}.md",
        content, uploaded_by=f"web research, approved by {reviewed_by}",
    )
    await asyncio.to_thread(_ingest_document_sync, db_path, doc_id)
    try:
        await learn_service_dependencies(db_path, doc_id)
    except Exception as ex:
        logger.warning("[kb] dependency extraction failed for %s: %s", doc_id, ex)

    now = _utc_iso()
    conn = _connect(db_path)
    conn.execute(
        "UPDATE kb_research_candidates SET status = 'approved', reviewed_by = ?, reviewed_at = ? "
        "WHERE id = ?",
        (reviewed_by, now, candidate_id),
    )
    conn.commit()
    conn.close()

    if candidate.get("gap_id"):
        set_gap_status(db_path, candidate["gap_id"], "resolved")
    add_audit(db_path, tenant_id, reviewed_by, "candidate_approved", doc_id, f"from candidate {candidate_id}")
    return doc_id


def reject_research_candidate(
    db_path: str, tenant_id: str, candidate_id: str, reviewed_by: str, reason: str
) -> bool:
    """Marks the candidate rejected and, if it traces back to a gap,
    REOPENS that gap rather than resolving it — a rejection means the
    question is still unanswered, not that this was an acceptable answer."""
    candidate = get_research_candidate(db_path, candidate_id)
    if not candidate or candidate["tenant_id"] != tenant_id or candidate["status"] != "pending_approval":
        return False

    now = _utc_iso()
    conn = _connect(db_path)
    conn.execute(
        "UPDATE kb_research_candidates SET status = 'rejected', reviewed_by = ?, reviewed_at = ? "
        "WHERE id = ?",
        (reviewed_by, now, candidate_id),
    )
    conn.commit()
    conn.close()

    if candidate.get("gap_id"):
        set_gap_status(db_path, candidate["gap_id"], "open")
    add_audit(db_path, tenant_id, reviewed_by, "candidate_rejected", candidate_id, reason)
    return True


async def approve_pending_document(db_path: str, tenant_id: str, doc_id: str, reviewed_by: str) -> bool:
    """Turns a "Perform Research" result into real knowledge: the document
    row already exists (created status='pending_approval' the moment
    research finished, so the RCA docs tab has something to show) but was
    deliberately never queued for ingest_document -- this runs that same
    chunk/embed pipeline now, gated behind an admin's explicit approval,
    then grows the dependency graph exactly like a fresh upload does.
    Resolves the originating gap only if ingestion actually succeeded --
    a document that produced no usable text ends 'failed', and the gap
    should stay 'researching' (approve again after fixing nothing is
    possible; reject instead reopens it) rather than being marked resolved
    for content that never actually entered the knowledge base."""
    doc = get_document(db_path, doc_id)
    if not doc or doc.get("tenant_id") != tenant_id or doc.get("status") != "pending_approval":
        return False

    await asyncio.to_thread(_ingest_document_sync, db_path, doc_id)
    ingested = get_document(db_path, doc_id)
    ok = bool(ingested and ingested.get("status") == "ready")
    if ok:
        try:
            await learn_service_dependencies(db_path, doc_id)
        except Exception as ex:
            logger.warning("[kb] dependency extraction failed for %s: %s", doc_id, ex)
        if doc.get("gap_id"):
            set_gap_status(db_path, doc["gap_id"], "resolved")

    # Two origins reach this function -- a knowledge gap's research result and
    # an investigation's own RCA document -- so the audit line names whichever
    # one this was rather than always claiming "from gap None".
    origin = (
        f"from gap {doc['gap_id']}" if doc.get("gap_id")
        else f"from incident {doc['incident_id']}" if doc.get("incident_id")
        else "from a pending document"
    )
    add_audit(
        db_path, tenant_id, reviewed_by,
        "pending_document_approved" if ok else "pending_document_approve_failed",
        doc_id, origin,
    )
    return ok


def reject_pending_document(db_path: str, tenant_id: str, doc_id: str) -> bool:
    """Discards a "Perform Research" result the admin doesn't want: deletes
    the document outright (never ingested, so no chunks exist to clean up
    beyond the defensive DELETE below) and, if it traces back to a gap,
    REOPENS that gap -- same "rejection means still unanswered, not that
    this was acceptable" reasoning reject_research_candidate uses."""
    doc = get_document(db_path, doc_id)
    if not doc or doc.get("tenant_id") != tenant_id or doc.get("status") != "pending_approval":
        return False

    conn = _connect(db_path)
    conn.execute("DELETE FROM kb_chunks WHERE document_id = ?", (doc_id,))
    conn.execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))
    conn.commit()
    conn.close()

    if doc.get("gap_id"):
        set_gap_status(db_path, doc["gap_id"], "open")
    return True


# ─── Retrieval ──────────────────────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _cosine(a: list[float], b: list[float]) -> float:
    # zip() would silently truncate to the shorter vector and return a
    # plausible-looking score for two different embedding models — a 256-dim
    # mock vector against a 1024-dim real one scores as if it meant something,
    # and that number feeds the knowledge-gap coverage decision. Refusing to
    # compare is the honest answer: stale chunks drop out of the semantic
    # ranking but BM25 still retrieves them through the RRF fusion.
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def classify_question(question: str) -> str:
    """Regex, not a model call — the trigger vocabulary is small and closed,
    so a round-trip to decide the route would add a failure mode the decision
    doesn't need. Mirrors rca/router.py::classify (aggregate vs retrieval;
    this module has no blast_radius/gap/known_pattern routes — those are
    handled elsewhere, see the plan)."""
    return "aggregate" if _AGGREGATE_RE.search(question) else "retrieval"


def count_ready_documents(db_path: str, tenant_id: str) -> int:
    """The exact number render_document_index's table has rows for. Handed to
    the model as its own field precisely because counting rows in a
    Markdown table embedded in a larger tool result is the kind of thing an
    LLM does unreliably at even moderate row counts -- computing it here
    removes the need for the model to count anything at all."""
    conn = _connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM kb_documents WHERE tenant_id = ? AND status = 'ready'", (tenant_id,)
    ).fetchone()[0]
    conn.close()
    return count


def render_document_index(db_path: str, tenant_id: str) -> str:
    """The tenant's document list as a pipe table — handed to the model
    alongside retrieved chunks on the aggregate route, since top-k retrieval
    structurally cannot answer "how many docs mention X" or "list every
    uploaded incident"."""
    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT d.id, d.title, d.uploaded_at, "
        "(SELECT COUNT(*) FROM kb_chunks c WHERE c.document_id = d.id) AS chunk_count "
        "FROM kb_documents d WHERE d.tenant_id = ? AND d.status = 'ready' "
        "ORDER BY d.uploaded_at",
        (tenant_id,),
    ).fetchall()
    conn.close()
    if not rows:
        return "No documents in the knowledge base yet."
    # Real GFM table syntax, not bare pipe-delimited lines: this string
    # reaches two different renderers -- an LLM (search_knowledge_base's
    # tool result, either shape parses fine) and ReactMarkdown+remarkGfm on
    # the frontend (kb_report.py's aggregate route), where CommonMark
    # collapses single newlines inside a paragraph into one run-together
    # line unless it is an actual table.
    lines = ["| id | title | uploaded_at | chunks |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id'][:8]} | {r['title']} | {r['uploaded_at']} | {r['chunk_count']} |")
    return "\n".join(lines)


def search(db_path: str, tenant_id: str, query: str, top_k: int = 5) -> dict:
    """Hybrid retrieval: Reciprocal Rank Fusion over a semantic ranking and a
    BM25 ranking, each over-fetched at 2*top_k and the fused result hard-capped
    at top_k (per rca/retrieve.py::hybrid — RRF fuses by *rank*, not raw score,
    which is what makes combining cosine similarity and a BM25 score
    meaningful despite the two being on unrelated scales).

    Returns {"results": [...], "coverage": float} — coverage is the *semantic*
    ranking's top score specifically (not the fused RRF score, which isn't a
    similarity number), used by callers to decide whether this counts as a
    knowledge gap.
    "coverage_floor" is the relevance threshold that applies to this coverage
    value -- COVERAGE_FLOOR for a real embedding model, the recalibrated
    MOCK_COVERAGE_FLOOR when running without AI credentials.
    "relevant" is the single boolean callers should actually gate on: coverage
    cleared coverage_floor, and -- in mock mode only, where a hashing-trick
    vector can score an unrelated pair by pure chance -- the match is also
    corroborated by at least one shared non-stopword term with the query.
    """
    embedder = EmbeddingsModelClient()
    query_vectors, _ = embedder.embed_many([query])
    query_vector = query_vectors[0]

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT c.id AS chunk_id, c.section_title, c.text, c.embedding, "
        "d.cite_key AS cite_key, d.title AS document_title, d.metadata AS document_metadata "
        "FROM kb_chunks c JOIN kb_documents d ON d.id = c.document_id "
        "WHERE c.tenant_id = ? AND d.status = 'ready'",
        (tenant_id,),
    ).fetchall()
    conn.close()

    coverage_floor = COVERAGE_FLOOR if embedder.available else MOCK_COVERAGE_FLOOR

    if not rows:
        return {"results": [], "coverage": 0.0, "coverage_floor": coverage_floor, "relevant": False}

    semantic_ranked = sorted(
        rows, key=lambda r: _cosine(query_vector, json.loads(r["embedding"])), reverse=True
    )
    coverage = _cosine(query_vector, json.loads(semantic_ranked[0]["embedding"]))

    wide_k = max(top_k * 2, top_k)
    semantic_top = semantic_ranked[:wide_k]

    bm25 = BM25Okapi([_tokenize(r["text"]) for r in rows])
    bm25_scores = bm25.get_scores(_tokenize(query))
    keyword_top = [
        rows[i] for i in sorted(range(len(rows)), key=lambda i: bm25_scores[i], reverse=True)[:wide_k]
    ]

    relevant = coverage >= coverage_floor
    if not embedder.available:
        # A hashing-trick vector has no real notion of meaning, so two
        # unrelated texts can still land in nearby buckets by chance --
        # empirically, "What is the capital of France?" and "Recommend a
        # good science fiction novel" both scored *higher* than a genuine
        # in-corpus question against the same small document set. A raw
        # BM25 score above 0 isn't a strong enough corroboration signal on
        # its own -- _tokenize keeps stopwords, so "a"/"the"/"for" alone are
        # enough to make BM25 nonzero for almost any query. Requiring an
        # actual *content* word (stopwords stripped) to appear verbatim is
        # real evidence of "not covered" that a fixed cosine floor alone
        # can't provide in mock mode. The right scope for that check took
        # three tries, verified against real queries each time:
        #   - Whole corpus: too permissive. Passed for three deliberately
        #     unrelated real test queries (Step Functions, Secrets Manager
        #     rotation, an internal team-ownership question) against a
        #     25-document corpus, because ordinary technical words like
        #     "deploy"/"application"/"access"/"credentials" appear SOMEWHERE
        #     across 25 real incident postmortems almost regardless of actual
        #     topic -- gets weaker as the corpus grows, backwards from what a
        #     relevance gate should do.
        #   - semantic_ranked[0] alone (the single top chunk by a noisy mock
        #     cosine score): too narrow. A genuinely matching document split
        #     across multiple chunks by chunk_text()'s section-header split
        #     can rank its best-matching chunk below its first chunk here, so
        #     real matches (verified: "What caused the RDS connection pool
        #     exhaustion incident?" against the doc of the same name) started
        #     failing too.
        #   - semantic_top (the wide_k window used for RRF fusion below,
        #     ~2x top_k chunks spanning several different documents): back to
        #     too permissive for the same reason as the whole corpus, just
        #     less so -- wide_k is large enough to reintroduce unrelated
        #     documents' vocabulary.
        # The scope that is actually correct: every chunk belonging to the
        # SAME document that produced `coverage` -- precisely the document
        # the coverage score is a claim about, immune to which one of that
        # document's own chunks a noisy cosine score happened to rank first.
        query_terms = {t for t in _tokenize(query) if t not in _STOPWORDS}
        top_doc_title = semantic_ranked[0]["document_title"]
        top_doc_terms = {
            t for r in rows if r["document_title"] == top_doc_title
            for t in _tokenize(r["text"]) if t not in _STOPWORDS
        }
        relevant = relevant and bool(query_terms & top_doc_terms)

    fused: dict[str, float] = {}
    row_by_id: dict[str, sqlite3.Row] = {}
    for ranking in (semantic_top, keyword_top):
        for rank, row in enumerate(ranking, start=1):
            key = row["chunk_id"]
            fused[key] = fused.get(key, 0.0) + 1.0 / (60 + rank)
            row_by_id[key] = row

    ordered_ids = sorted(fused, key=lambda k: fused[k], reverse=True)[:top_k]
    results = []
    for cid in ordered_ids:
        row = row_by_id[cid]
        try:
            document_metadata = json.loads(row["document_metadata"]) if row["document_metadata"] else {}
        except (ValueError, TypeError):
            document_metadata = {}
        results.append({
            "cite_key": row["cite_key"],
            "section_title": row["section_title"],
            "document_title": row["document_title"],
            "text": row["text"],
            "citation_tag": f"[{row['cite_key']} · {row['section_title'] or 'Document'}]",
            "document_metadata": document_metadata,
        })
    return {"results": results, "coverage": round(coverage, 4), "coverage_floor": coverage_floor, "relevant": relevant}


def resolve_citations(answer_text: str, results: list[dict]) -> list[dict]:
    """Only trusts a `[cite_key · section]` tag in generated text if it
    matches a chunk that was actually retrieved — a model can't cite
    something it wasn't shown. Used by callers composing an answer from
    search()'s results (and, later, by the web-research and RCA-report
    milestones over their own result sets)."""
    available = {
        (r["cite_key"].strip(), (r["section_title"] or "Document").strip()): r for r in results
    }
    seen = set()
    citations = []
    for m in _CITATION_RE.finditer(answer_text):
        key = (m.group(1).strip(), m.group(2).strip())
        if key in seen or key not in available:
            continue
        seen.add(key)
        r = available[key]
        citations.append({
            "cite_key": key[0],
            "section_title": key[1],
            "document_title": r["document_title"],
            "snippet": r["text"][:200],
        })
    return citations


# ─── Agent tool ─────────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "Search the team's uploaded RCA/postmortem documents for a prior incident or "
                "guidance relevant to the current investigation. Use this before concluding root "
                "cause if a similar failure may already be documented. Each result carries a "
                "citation_tag like '[cite_key · section]' — quote it verbatim when citing that "
                "passage in your answer. For a 'how many'/'list all'/'count' style question, the "
                "response ALSO includes `document_index` (a Markdown table, one row per document "
                "actually in the knowledge base) and `document_count` (the exact number of rows in "
                "that table, precomputed). For 'how many', state `document_count`'s value directly "
                "— never count the table rows yourself, that is unreliable. For 'list all', name "
                "the titles the table actually shows. Never state a number or list you did not read "
                "directly off these two fields."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for, e.g. the symptom or suspected cause.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max matching passages to return. Default 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_known_dependencies",
            "description": (
                "Check the auto-learned service dependency graph for what depends on a service, "
                "i.e. what could be affected if it fails or degrades — before requesting a live AWS "
                "dependency lookup or web research. Built entirely from past ingested incident "
                "documents and resolved investigations, not hand-curated, so an unfamiliar or "
                "not-yet-learned service returns an empty result rather than an error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "Short lowercase AWS service id, e.g. 'alb', 'rds', 'lambda'.",
                    },
                },
                "required": ["service"],
            },
        },
    },
]


def _dump(payload: dict) -> str:
    """Same truncate-the-list-until-it-fits budget as
    aws_observability._dump, sized to this tool's one list field."""
    text = json.dumps(payload, default=str, separators=(",", ":"))
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    results = payload.get("results") or []
    kept = list(results)
    while kept and len(text) > MAX_TOOL_OUTPUT_CHARS:
        kept = kept[:-1]
        text = json.dumps({**payload, "results": kept}, default=str, separators=(",", ":"))
    return text


async def dispatch(db_path: str, tenant_id: str | None, name: str, args: dict, user_question: str | None = None) -> str:
    """Run a knowledge-base tool call. Unknown names return a message rather
    than raising, matching aws_observability.dispatch. tenant_id is None when
    the incident was never touched by a tenant-lane request (e.g. a platform
    admin only) — searching everything in that case would risk mixing
    tenants, so this declines instead of guessing.

    user_question is the human's own message that prompted this tool call,
    if the caller has it -- used only for aggregate-question detection (see
    below), never for retrieval itself. A model choosing search_knowledge_base's
    own `query` argument routinely shortens "how many incidents are in the
    knowledge base?" down to a bare keyword like "incidents" -- classifying
    on that alone would silently miss the aggregate route (and did, in
    testing: the model tried to search the WEB to count its own local
    knowledge base once no document_index came back). Classifying on
    whichever of the two actually reads as an aggregate question is far more
    reliable than hoping the model's paraphrase preserves the tell."""
    if name == "check_known_dependencies":
        if tenant_id is None:
            return _dump({"service": args.get("service"), "downstream": [],
                          "message": "No knowledge base is scoped to this incident."})
        service = str(args.get("service") or "").strip().lower()
        if not service:
            return _dump({"service": service, "downstream": [], "message": "check_known_dependencies needs a service."})
        downstream = await asyncio.to_thread(downstream_of, db_path, tenant_id, service)
        return _dump({
            "service": service,
            "downstream": downstream,
            "message": (
                f"{len(downstream)} service(s) depend on {service}, per past ingested incidents."
                if downstream else
                f"No dependency relationships involving {service} are recorded yet."
            ),
        })
    if name != "search_knowledge_base":
        return _dump({"error": "unknown_tool", "message": f"{name} is not an available tool."})
    if tenant_id is None:
        return _dump({"results": [], "coverage": 0.0, "message": "No knowledge base is scoped to this incident."})

    query = (args.get("query") or "").strip()
    if not query:
        return _dump({"results": [], "coverage": 0.0, "message": "search_knowledge_base needs a query."})
    top_k = int(args.get("top_k") or 5)

    payload = await asyncio.to_thread(search, db_path, tenant_id, query, top_k)

    if "aggregate" in (classify_question(query), classify_question(user_question or "")):
        # The document index IS the complete answer to a "how many / list
        # all" question, independent of whether any one chunk happened to
        # score as semantically relevant -- forcing relevant=True here (and
        # skipping record_gap below) stops an aggregate question from ALSO
        # reading as a knowledge gap just because its retrieval-side score
        # was low, which would otherwise leave a contradictory payload (a
        # complete index alongside "this wasn't relevant") that risks the
        # model asking permission for web research it doesn't need.
        payload = {
            **payload,
            "document_index": await asyncio.to_thread(render_document_index, db_path, tenant_id),
            "document_count": await asyncio.to_thread(count_ready_documents, db_path, tenant_id),
            "relevant": True,
        }
    elif not payload["relevant"]:
        await asyncio.to_thread(record_gap, db_path, tenant_id, query, "low_coverage", payload["coverage"])

    return _dump(payload)