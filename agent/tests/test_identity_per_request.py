"""Authorization is resolved once per REQUEST, never once per session.

The chat panels poll a snapshot every 600ms, and each kb-chat endpoint both
authorizes and names the caller for its audit trail. _fetch_identity keeps the
answer for the rest of the request, so that is one trip to the backend (and
PostgreSQL) per poll instead of two. What must NOT happen is the answer
surviving into the next request: CLAUDE.md requires the role to be re-read on
every call, so a disable or a promotion is in force on the caller's very next
request. This file proves both halves against the real endpoints.

The uncached check (_authorize, which calls the Node backend) is stubbed; the
memo (_fetch_identity) and the gates are production code.

Run: python tests/test_identity_per_request.py
"""
import os
import tempfile

os.environ.setdefault("SRE_POLL_SECONDS", "0")

from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from tools import kb_chat_store
from tools import knowledge_base as kb

main.DB_PATH = os.path.join(tempfile.mkdtemp(), "test-runs.db")
kb_chat_store.ensure_table(main.DB_PATH)
kb.ensure_table(main.DB_PATH)  # the candidate queue, for the promotion check

calls = []
state = {"enabled": True, "role": "user"}


async def _stub_authorize(authorization=None):
    calls.append(authorization)
    if not state["enabled"]:
        raise HTTPException(status_code=403, detail="This account has been disabled.")
    return {"id": "u-1", "email": "user@aegis.test", "role": state["role"], "enabled": True}


main._authorize = _stub_authorize
client = TestClient(main.app)
HEADERS = {"Authorization": "Bearer same-token-every-time"}
SNAPSHOT = "/api/kb-chat/threads/no-such-thread/snapshot"

# ─── Within one request: one authorization, however many callers ask ───────
# The snapshot runs _require_authenticated AND _caller_label.
r = client.get(SNAPSHOT, headers=HEADERS)
assert r.status_code == 404, r.text  # authorized, then no such thread
assert len(calls) == 1, f"one request must authorize once, not {len(calls)} times"

# ─── Across requests: authorized afresh every time ──────────────────────────
r = client.get(SNAPSHOT, headers=HEADERS)
assert r.status_code == 404, r.text
assert len(calls) == 2, "the next request must ask again — a cached role would outlive a disable"

# A disable takes effect on the very next request, with the same token.
state["enabled"] = False
r = client.get(SNAPSHOT, headers=HEADERS)
assert r.status_code == 403, f"a disabled account must be refused at once, got {r.status_code}"
assert len(calls) == 3

# And a promotion is visible on the next request too.
state.update(enabled=True, role="admin")
r = client.get("/api/knowledge-base/candidates", headers=HEADERS)
assert r.status_code != 403, f"a freshly promoted admin was still refused: {r.status_code} {r.text}"

# A different token in the same process is never served another caller's answer.
before = len(calls)
client.get(SNAPSHOT, headers={"Authorization": "Bearer someone-else"})
assert calls[before:] == ["Bearer someone-else"], calls[before:]

print("test_identity_per_request OK")
