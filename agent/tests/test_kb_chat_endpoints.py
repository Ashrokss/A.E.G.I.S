"""The Knowledge-Base chat endpoints are admin-gated where they mutate, open
where the browser polls/streams (same convention as the incident endpoints),
and tenant-scoped the same way the KB document endpoints already are.
Run: python -m tests.test_kb_chat_endpoints"""
import os
import tempfile

os.environ.setdefault("SRE_POLL_SECONDS", "0")  # keep the poller off in tests

from fastapi.testclient import TestClient

import main
from tools import kb_chat_store as store

main.DB_PATH = os.path.join(tempfile.mkdtemp(), "test-runs.db")
store.ensure_table(main.DB_PATH)

client = TestClient(main.app)

# ─── Auth is real before we stub it out ───────────────────────────────────────
assert client.post("/api/kb-chat/threads", json={}).status_code in (401, 403)
assert client.get("/api/kb-chat/threads").status_code in (401, 403)
assert client.post("/api/kb-chat/threads/x/messages", json={"role": "user", "content": "hi"}).status_code in (401, 403)

# The snapshot is NO LONGER open. It carries the whole conversation, and a
# chat belongs to whoever started it, so anonymous access here made scoping the
# listing cosmetic: anyone who could reach the agent could read any thread by
# id. Only /stream stays open, because EventSource cannot send a header.
assert client.get("/api/kb-chat/threads/nope/snapshot").status_code in (401, 403),     "the chat snapshot must refuse an anonymous caller"


# The identity SOURCE is stubbed, not the gates. _require_authenticated and
# _require_admin stay real, so the role checks below are the production ones.
async def _stub_identity(authorization=None):
    return {"id": "test-user", "email": "tester@aegis.test", "role": "admin", "enabled": True}


main._fetch_identity = _stub_identity

stepped: list[str] = []


async def fake_step(thread_id, *_a, **_k):
    stepped.append(thread_id)


main.kb_chat_agent.step = fake_step

# ─── Create ────────────────────────────────────────────────────────────────
r = client.post("/api/kb-chat/threads", json={"title": "About the RDS incident"})
assert r.status_code == 200, r.text
thread_id = r.json()["id"]
assert r.json()["status"] == "open"

# ─── Post a message ────────────────────────────────────────────────────────
assert client.post("/api/kb-chat/threads/nope/messages",
                    json={"role": "user", "content": "hi"}).status_code == 404
r = client.post(f"/api/kb-chat/threads/{thread_id}/messages",
                 json={"role": "user", "content": "What caused the RDS connection pool exhaustion?"})
assert r.status_code == 200 and r.json() == {"status": "received"}
assert stepped == [thread_id], stepped

# ─── Snapshot ──────────────────────────────────────────────────────────────
snap = client.get(f"/api/kb-chat/threads/{thread_id}/snapshot").json()
assert snap["thread"]["id"] == thread_id
assert snap["thread"]["title"] == "About the RDS incident", "explicit create title must not be overwritten"
contents = [m["content"] for m in snap["messages"]]
assert "What caused the RDS connection pool exhaustion?" in contents
# is_running is what the chat panel polls to drive "Thinking…" -- it must be
# a real field on the snapshot, not something the frontend has to infer.
assert snap["is_running"] is False, "the stubbed fake_step already completed by the time this polls"

# A title given at creation is not clobbered by the first-message auto-title.
r2 = client.post("/api/kb-chat/threads", json={})
thread_id_2 = r2.json()["id"]
client.post(f"/api/kb-chat/threads/{thread_id_2}/messages",
            json={"role": "user", "content": "A totally different question"})
snap2 = client.get(f"/api/kb-chat/threads/{thread_id_2}/snapshot").json()
assert snap2["thread"]["title"] == "A totally different question", snap2["thread"]

# ─── List (tenant-scoped) ──────────────────────────────────────────────────
threads = client.get("/api/kb-chat/threads").json()["threads"]
assert {thread_id, thread_id_2} <= {t["id"] for t in threads}

# ─── Stop ──────────────────────────────────────────────────────────────────
# The actual cancellation semantics (asyncio.Task.cancel(), "Stopped." message)
# are covered directly against KbChatAgent in test_kb_chat_agent.py; here we
# only need the HTTP contract, so kb_chat_agent.stop is stubbed the same way
# .step already is above.
stopped_ids: list[str] = []


async def fake_stop(thread_id, *_a, **_k):
    stopped_ids.append(thread_id)
    return True


main.kb_chat_agent.stop = fake_stop

assert client.post("/api/kb-chat/threads/nope/stop").status_code == 404
r = client.post(f"/api/kb-chat/threads/{thread_id}/stop")
assert r.status_code == 200 and r.json() == {"stopped": True}, r.json()
assert stopped_ids == [thread_id], stopped_ids

# ─── A chat is private to whoever started it ─────────────────────────────────
# The thing this actually has to guarantee. tenant_id is one constant in this
# deployment, so it partitions nothing: without the created_by filter every
# signed-in user reads every other user's conversation.
async def _stub_other_user(authorization=None):
    return {"id": "other-user", "email": "someone.else@aegis.test", "role": "admin", "enabled": True}


main._fetch_identity = _stub_other_user

assert client.get("/api/kb-chat/threads").json()["threads"] == [],     "another user's chats must not appear in the listing"
assert client.get(f"/api/kb-chat/threads/{thread_id}/snapshot").status_code == 404,     "another user's conversation must not be readable by id"
assert client.post(
    f"/api/kb-chat/threads/{thread_id}/messages", json={"role": "user", "content": "hi"}
).status_code == 404, "another user must not be able to post into the thread"
assert client.post(f"/api/kb-chat/threads/{thread_id}/stop").status_code == 404,     "another user must not be able to stop the thread"

main._fetch_identity = _stub_identity
owned = client.get("/api/kb-chat/threads").json()["threads"]
assert thread_id in [t["id"] for t in owned], "the owner still sees their own chat"

print("ok: kb-chat endpoints gated, snapshot authenticated and owner-scoped, thread create/post/"
      "snapshot/list/stop round-trip, an explicit creation title survives the first-message "
      "auto-title, and one user cannot read, post into or stop another user's chat")
