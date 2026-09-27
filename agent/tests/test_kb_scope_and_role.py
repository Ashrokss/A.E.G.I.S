"""Knowledge-base scope and role gating after the single-pool migration.

There is ONE deployment, ONE Cognito pool and ONE knowledge-base scope, so the
thing this file used to prove — that tenants were isolated from each other and
from a shared "platform" scope — no longer exists to prove. What matters now:

  * every caller resolves to the SAME scope, so an admin and a user see the same
    knowledge base (there is no per-caller partition left to leak across);
  * reading and uploading need only authentication;
  * the candidate queue needs role == 'admin', read from PostgreSQL — and a
    `user` is refused even though their token is perfectly valid.

The identity SOURCE (_fetch_identity, which calls the Node backend) is stubbed.
The GATES (_require_authenticated, _require_admin, _require_admin_scope) are the
real ones, so the role logic under test is production code.

Run: python tests/test_kb_scope_and_role.py
"""
import os
import tempfile

os.environ.setdefault("SRE_POLL_SECONDS", "0")

from fastapi.testclient import TestClient

import main
from tools import knowledge_base as kb

# Same redirect-before-use pattern as test_sre_endpoints.py: main.init_db()
# already ran against the module's original DB_PATH at import time, so the
# kb tables need re-creating against the throwaway path this test redirects to.
main.DB_PATH = os.path.join(tempfile.mkdtemp(), "test-runs.db")
kb.ensure_table(main.DB_PATH)

client = TestClient(main.app)

# ─── Auth is real before anything is stubbed ────────────────────────────────
# No Cognito issuer/JWKS is configured here, so a token cannot be verified and
# every gated endpoint must refuse. Asserting this FIRST is what proves the
# stubs below are bypassing a gate that genuinely exists.
assert client.get("/api/knowledge-base/documents").status_code in (401, 500), \
    "listing documents must be gated"
assert client.get("/api/knowledge-base/candidates").status_code in (401, 500), \
    "the candidate queue must be gated"


IDENTITIES = {
    "admin": {"id": "u-admin", "email": "admin@aegis.test", "role": "admin", "enabled": True},
    "user": {"id": "u-user", "email": "user@aegis.test", "role": "user", "enabled": True},
}

_acting = "admin"


async def _stub_identity(authorization=None):
    """Stands in for the Node backend's /api/auth/me. Returns whichever role the
    test is currently acting as — the gates then decide what that allows."""
    return IDENTITIES[_acting]


main._fetch_identity = _stub_identity

HEADERS = {"Authorization": "Bearer any-token-the-stub-ignores"}


def act_as(role: str):
    global _acting
    _acting = role


def upload(filename: str, text: str):
    return client.post(
        "/api/knowledge-base/documents", headers=HEADERS,
        files={"file": (filename, text, "text/markdown")},
    )


# ─── One scope: what an admin uploads, a user reads ─────────────────────────
act_as("admin")
r = upload("shared-doc.md", "# Shared doc\n\nSome content.\n")
assert r.status_code == 200, r.text
doc_id = r.json()["id"]

listed = client.get("/api/knowledge-base/documents", headers=HEADERS).json()
assert any(d["id"] == doc_id for d in listed["documents"]), listed

# The SAME document is visible to a plain user — one deployment, one knowledge
# base. This is the deliberate inverse of the old tenant-isolation assertion.
act_as("user")
listed_as_user = client.get("/api/knowledge-base/documents", headers=HEADERS).json()
assert any(d["id"] == doc_id for d in listed_as_user["documents"]), \
    "there is one knowledge base — a user must see what an admin uploaded"

# Full document content (the "RCA docs" tab's openable view) too.
r = client.get(f"/api/knowledge-base/documents/{doc_id}", headers=HEADERS)
assert r.status_code == 200, r.text
assert r.json()["content"] == "# Shared doc\n\nSome content.\n", r.json()
assert client.get("/api/knowledge-base/documents/no-such-id", headers=HEADERS).status_code == 404

# A user may upload as well — the knowledge base is ordinary product surface.
r = upload("user-doc.md", "# User doc\n\nMore content.\n")
assert r.status_code == 200, r.text
user_doc_id = r.json()["id"]

# ─── The scope is one constant, whoever asks ────────────────────────────────
assert main._resolve_kb_scope_id(None) == main._KB_SCOPE
assert main._resolve_kb_scope_id("Bearer whatever") == main._KB_SCOPE, \
    "the scope must not depend on the caller any more"

# ─── The candidate queue is ADMIN ONLY, from the PostgreSQL role ────────────
# The token is identical in both cases; only the role the identity reports
# differs. That is the whole point of the migration.
act_as("user")
r = client.get("/api/knowledge-base/candidates", headers=HEADERS)
assert r.status_code == 403, f"a user must not reach the candidate queue: {r.text}"

act_as("admin")
r = client.get("/api/knowledge-base/candidates", headers=HEADERS)
assert r.status_code == 200, r.text

# Approve/reject are gated the same way, and a user is refused before the
# candidate id is even looked at (so this is a 403, never a 404).
act_as("user")
r = client.post("/api/knowledge-base/candidates/no-such-id/approve", headers=HEADERS)
assert r.status_code == 403, f"a user must not approve knowledge: {r.text}"
r = client.post("/api/knowledge-base/candidates/no-such-id/reject", headers=HEADERS, json={"reason": "no"})
assert r.status_code == 403, f"a user must not reject knowledge: {r.text}"

# ─── Delete is authenticated, and works within the one scope ───────────────
act_as("admin")
r = client.delete(f"/api/knowledge-base/documents/{user_doc_id}", headers=HEADERS)
assert r.status_code == 200, r.text
assert client.delete("/api/knowledge-base/documents/no-such-id", headers=HEADERS).status_code == 404

# ─── A disabled or unregistered caller never gets this far ─────────────────
# _fetch_identity is what raises for those, and it is the real function's job —
# here we only prove the gates propagate a refusal rather than defaulting open.
from fastapi import HTTPException


async def _refuse(authorization=None):
    raise HTTPException(status_code=403, detail="Your account is not registered with AEGIS.")


main._fetch_identity = _refuse
assert client.get("/api/knowledge-base/documents", headers=HEADERS).status_code == 403
assert client.get("/api/knowledge-base/candidates", headers=HEADERS).status_code == 403
r = upload("should-not-exist.md", "nope")
assert r.status_code == 403, "an unregistered caller must not be able to upload"

print("ok: one knowledge-base scope shared by every caller, reads and uploads need only "
      "authentication, the candidate queue and approve/reject require role=admin from "
      "PostgreSQL, and a refused identity is propagated rather than defaulting open")
