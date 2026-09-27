"""Knowledge-Base chat storage: thread CRUD + tenant isolation, message
round-tripping (kb_answer excluded from model replay, tool plumbing excluded
from the UI feed), and the pending_* flag lifecycle. Fully deterministic, no
model involved. Run: python tests/test_kb_chat_store.py"""
import os
import tempfile

from tools import kb_chat_store as store

db = os.path.join(tempfile.mkdtemp(), "runs.db")
store.ensure_table(db)
store.ensure_table(db)  # must be safe to call twice

TENANT_A = "tenant-a"
TENANT_B = "tenant-b"

# ─── Thread CRUD + tenant isolation ───────────────────────────────────────────
thread_id = store.create_thread(db, TENANT_A, created_by="alice")
assert store.get_thread(db, TENANT_A, thread_id) is not None
assert store.get_thread(db, TENANT_B, thread_id) is None, "wrong tenant must not see the thread"
assert store.get_thread_by_id(db, thread_id) is not None, "unscoped lookup ignores tenant"
assert store.get_thread_by_id(db, "no-such-thread") is None

store.set_title_if_unset(db, thread_id, "First title")
store.set_title_if_unset(db, thread_id, "Second title")
assert store.get_thread(db, TENANT_A, thread_id)["title"] == "First title", "must only stamp once"

store.set_fields(db, thread_id, pending_web_research="1")
assert store.get_thread(db, TENANT_A, thread_id)["pending_web_research"] == "1"
store.set_fields(db, thread_id, pending_web_research=None)
assert store.get_thread(db, TENANT_A, thread_id)["pending_web_research"] is None

thread_b = store.create_thread(db, TENANT_B)
threads_a = store.list_threads(db, TENANT_A)
assert len(threads_a) == 1 and threads_a[0]["id"] == thread_id
threads_b = store.list_threads(db, TENANT_B)
assert len(threads_b) == 1 and threads_b[0]["id"] == thread_b

# ─── Message round-tripping ───────────────────────────────────────────────────
store.add_message(db, thread_id, "user", "What caused the RDS incident?", "text")
store.add_message(db, thread_id, "assistant", "", "tool_call", tool_calls=[
    {"id": "call_1", "type": "function", "function": {"name": "search_knowledge_base", "arguments": "{}"}},
])
store.add_message(db, thread_id, "tool", '{"results": []}', "tool_output", tool_call_id="call_1")
store.add_message(db, thread_id, "agent", "It was probably a connection leak.", "draft")
store.add_message(db, thread_id, "agent", '{"answer": "It was a connection leak.", "doc_citations": [], "web_citations": []}', "kb_answer")
store.add_message(db, thread_id, "user", "Publish that as the answer by calling submit_kb_answer now.", "system")

model_messages = store.get_model_messages(db, thread_id)
model_types_content = [m["content"] for m in model_messages]
assert '{"answer": "It was a connection leak.", "doc_citations": [], "web_citations": []}' not in model_types_content, \
    "kb_answer rows must be excluded from model replay"
assert "It was probably a connection leak." in model_types_content, \
    "a draft (bare-prose, pre-nudge) answer must still be replayed to the model, so it isn't repeating itself blind"
assert any(m.get("tool_calls") for m in model_messages), "the tool_call message must survive with its tool_calls intact"
assert any(m["role"] == "tool" and m.get("tool_call_id") == "call_1" for m in model_messages)
assert any(m["content"] == "Publish that as the answer by calling submit_kb_answer now." for m in model_messages), \
    "the submit_kb_answer nudge (role=user, type=system) must still be replayed to the model"

ui_messages = store.get_ui_messages(db, thread_id)
ui_contents = [m["content"] for m in ui_messages]
assert "What caused the RDS incident?" in ui_contents
assert '{"answer": "It was a connection leak.", "doc_citations": [], "web_citations": []}' in ui_contents, \
    "kb_answer rows must be included in the UI feed"
assert '{"results": []}' not in ui_contents, "tool_output must be excluded from the UI feed"
assert "It was probably a connection leak." not in ui_contents, (
    "a draft answer must be hidden from the UI feed -- otherwise the user sees the same "
    "content twice, once as this draft and again moments later as the kb_answer card"
)
assert "Publish that as the answer by calling submit_kb_answer now." not in ui_contents, \
    "the submit_kb_answer nudge (type=system) must be hidden from the UI feed"
assert not any(m["content"] == "" for m in ui_messages), "the empty tool_call assistant row must be excluded from the UI feed"

# last_message / updated_at get stamped by every add_message call
assert store.get_thread(db, TENANT_A, thread_id)["last_message"] == \
    "Publish that as the answer by calling submit_kb_answer now."[:140]

# ─── retype_stale_drafts: the cross-question contamination fix ──────────────
# Regression guard for a real bug: a draft + its "publish that now" nudge,
# left in place forever, could get replayed into a LATER, unrelated
# question's context and get mistaken for that question's own answer.
store.retype_stale_drafts(db, thread_id)

model_messages_after = store.get_model_messages(db, thread_id)
model_contents_after = [m["content"] for m in model_messages_after]
assert "It was probably a connection leak." not in model_contents_after, (
    "a draft row must stop being replayed to the model once its turn has "
    "actually published -- this is the fix for real cross-question content bleed"
)
assert "Publish that as the answer by calling submit_kb_answer now." not in model_contents_after, (
    "the nudge that accompanied a now-stale draft must also stop being replayed"
)
assert '{"answer": "It was a connection leak.", "doc_citations": [], "web_citations": []}' not in model_contents_after, \
    "kb_answer must still be excluded from model replay after retyping"

ui_messages_after = store.get_ui_messages(db, thread_id)
assert not any(m["content"] == "It was probably a connection leak." for m in ui_messages_after), \
    "a retyped draft must still be hidden from the UI feed"

print("ok: thread CRUD + tenant isolation, title-stamped-once, pending flag set/clear, "
      "kb_answer excluded from model replay but shown in UI, tool plumbing excluded from UI, "
      "the submit_kb_answer nudge is replayed to the model but hidden from the UI, and "
      "retype_stale_drafts removes a resolved turn's draft+nudge from all future replay")

# ─── Context collapsing: old turns lose their scaffolding, recent ones don't ─
# Regression guard for a real bug: a long thread replayed EVERY past turn's
# full tool_call/tool_output scaffolding into every new question's context
# forever -- once a thread covered a dozen unrelated topics, that's a lot of
# other documents' raw text competing for the model's attention, and a real
# contributor to a later question answering itself from an old, unrelated
# turn's retrieved evidence instead of its own fresh lookup.
collapse_thread = store.create_thread(db, TENANT_A)


def _add_full_turn(question, answer_text, cite_key):
    store.add_message(db, collapse_thread, "user", question, "text")
    store.add_message(db, collapse_thread, "assistant", "", "tool_call", tool_calls=[
        {"id": f"call-{cite_key}", "type": "function",
         "function": {"name": "search_knowledge_base", "arguments": "{}"}},
    ])
    store.add_message(db, collapse_thread, "tool",
                       f'{{"results": [{{"cite_key": "{cite_key}"}}], "relevant": true}}',
                       "tool_output", tool_call_id=f"call-{cite_key}")
    store.add_message(db, collapse_thread, "agent",
                       f'{{"answer": "{answer_text}", "doc_citations": ["{cite_key}"], "web_citations": []}}',
                       "kb_answer")


# 5 full turns -- with _KEEP_FULL_RECENT_TURNS=3, the first 2 must collapse
# and the last 3 must stay fully intact.
for i in range(1, 6):
    _add_full_turn(f"Question number {i}?", f"Answer number {i}.", f"doc-{i}")

collapsed_messages = store.get_model_messages(db, collapse_thread)
collapsed_contents = [m["content"] for m in collapsed_messages]

# Turns 1-2 (older than the last 3): their tool_call/tool_output scaffolding
# must be gone, replaced by just question + plain-text answer.
for i in (1, 2):
    assert f"Question number {i}?" in collapsed_contents, (i, collapsed_contents)
    assert f"Answer number {i}." in collapsed_contents, (
        f"turn {i} must collapse to a plain-text question+answer pair, not vanish entirely", collapsed_contents,
    )
    assert not any(f'"cite_key": "doc-{i}"' in (c or "") for c in collapsed_contents), (
        f"turn {i}'s raw tool_output JSON must NOT survive collapsing -- that's the whole point", collapsed_contents,
    )

# Turns 3-5 (the most recent 3): full scaffolding must survive untouched,
# including their raw tool_output JSON.
for i in (3, 4, 5):
    assert any(f'"cite_key": "doc-{i}"' in (c or "") for c in collapsed_contents), (
        f"turn {i} is within the most recent {3} turns and must keep its full tool_output", collapsed_contents,
    )

# A short thread (fewer turns than the keep-full window) must be completely
# unaffected -- collapsing is a no-op until the thread is actually long.
short_thread = store.create_thread(db, TENANT_A)
store.add_message(db, short_thread, "user", "Just one question?", "text")
store.add_message(db, short_thread, "agent", '{"answer": "Just one answer.", "doc_citations": [], "web_citations": []}', "kb_answer")
short_messages = store.get_model_messages(db, short_thread)
# The user's own question survives verbatim -- nothing to collapse yet.
assert any(m["content"] == "Just one question?" for m in short_messages)

print("ok: get_model_messages collapses every resolved turn older than the most "
      "recent 3 down to a plain question+answer pair, keeps the 3 most recent turns' "
      "full tool-call scaffolding intact, and leaves a short thread completely unaffected")
