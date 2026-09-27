"""kb_chat_agent.py's deterministic parts: research-candidate capture, the
submit_kb_answer incomplete-args guard, and the no-model-configured failure
path (real, not mocked -- ModelClient().available is reliably False in this
test environment, same fact test_kb_report.py already relies on). The full
multi-turn tool-dispatch loop needs a real model and is exercised manually,
same standard sre_agent.py's own loop is already held to (no test_sre_agent.py
exists either). Run: python tests/test_kb_chat_agent.py"""
import asyncio
import os
import tempfile

import kb_chat_agent
from tools import knowledge_base as kb
from tools import kb_chat_store as store
from utils.logger import log_queues

db = os.path.join(tempfile.mkdtemp(), "runs.db")
store.ensure_table(db)
kb.ensure_table(db)
kb_chat_agent.DB_PATH = db

TENANT_A = "tenant-a"

# ─── _publish_answer: incomplete args are rejected, not silently accepted ────
agent = kb_chat_agent.KbChatAgent()
thread_id = store.create_thread(db, TENANT_A)
row = store.get_thread_by_id(db, thread_id)

result = asyncio.run(agent._publish_answer(thread_id, row, {"answer": ""}))
assert '"error": "incomplete"' in result, result
result = asyncio.run(agent._publish_answer(thread_id, row, {"confidence": "high"}))
assert '"error": "incomplete"' in result, result

# A complete answer publishes as a kb_answer message.
complete = {
    "answer": "The RDS connection pool was exhausted by unpooled Lambda connections.",
    "doc_citations": ["[inc-2025-0101 · Root Cause]"],
    "web_citations": [],
    "confidence": "high",
    "confidence_reason": "Directly stated in the matched incident doc.",
}
result = asyncio.run(agent._publish_answer(thread_id, row, complete))
assert '"published": true' in result, result
ui = store.get_ui_messages(db, thread_id)
assert any(m["type"] == "kb_answer" for m in ui), ui
import json as _json
published_payload = _json.loads(next(m["content"] for m in ui if m["type"] == "kb_answer"))
assert published_payload["doc_citations"] == ["inc-2025-0101 · Root Cause"], (
    "enclosing brackets must be stripped to match extract_citations' own format", published_payload
)

# ─── pending_candidate_capture must always clear, even with no citations ────
# A regression guard: search_web finding nothing produces an answer with
# empty doc/web citations. If the flag were only cleared inside
# _maybe_capture_research_candidate (only called when citations exist), it
# would stay set forever and wrongly tag a LATER, unrelated answer in the
# same thread.
thread_id_empty = store.create_thread(db, TENANT_A)
store.set_fields(db, thread_id_empty, pending_candidate_capture="1")
row_empty = store.get_thread_by_id(db, thread_id_empty)
candidates_before = len(kb.list_research_candidates(db, TENANT_A))
empty_answer = {
    "answer": "No relevant information was found in the knowledge base or the web.",
    "doc_citations": [],
    "web_citations": [],
    "confidence": "high",
    "confidence_reason": "Both sources returned nothing.",
}
asyncio.run(agent._publish_answer(thread_id_empty, row_empty, empty_answer))
assert store.get_thread_by_id(db, thread_id_empty)["pending_candidate_capture"] is None, (
    "an empty-citation answer must still clear pending_candidate_capture"
)
assert len(kb.list_research_candidates(db, TENANT_A)) == candidates_before, (
    "no candidate row should be created when there is nothing worth an admin reviewing"
)

# ...but the search still happened with the user's permission, so the question
# it could not answer lands in Knowledge Gaps rather than disappearing. Both
# lanes go through knowledge_turn.record_empty_research for this.
thread_empty_q = store.create_thread(db, TENANT_A)
store.set_fields(db, thread_empty_q, pending_candidate_capture="1")
store.add_message(db, thread_empty_q, "user", "Does AWS document a fix for Y?", "text")
asyncio.run(agent._publish_answer(
    thread_empty_q, store.get_thread_by_id(db, thread_empty_q), empty_answer
))
recorded = [g for g in kb.list_gaps(db, TENANT_A)
            if g["question"] == "Does AWS document a fix for Y?"]
assert len(recorded) == 1 and recorded[0]["reason"] == "web_research_empty", (
    "a consented web search that found nothing must still be recorded as a gap", recorded,
)

# ─── _maybe_capture_research_candidate ───────────────────────────────────────
thread_id_2 = store.create_thread(db, TENANT_A)
store.add_message(db, thread_id_2, "user", "What does AWS say about X?", "text")
asyncio.run(agent._maybe_capture_research_candidate(
    thread_id_2, TENANT_A, "What does AWS say about X?", "X is caused by Y, per an official source.",
    ["inc-0001 · Summary"], ["https://example.com/docs"],
))
candidates = kb.list_research_candidates(db, TENANT_A, status="pending_approval")
assert any(c["created_by"] == "kb_chat_agent" and c["question"] == "What does AWS say about X?" for c in candidates), candidates

# ─── No-model-configured path is real, not mocked ────────────────────────────
thread_id_3 = store.create_thread(db, TENANT_A)
assert agent.mc.available is False, "this test environment must have no AI credentials configured"
asyncio.run(agent.step(thread_id_3))
ui_3 = store.get_ui_messages(db, thread_id_3)
assert any(m["type"] == "error" for m in ui_3), ui_3
queue = log_queues[thread_id_3]
seen = []
while not queue.empty():
    seen.append(queue.get_nowait())
assert "DONE" in seen, (
    "unlike sre_agent.py's _fail (which never pushes DONE, leaving its SSE stream open on every "
    "failure path), KbChatAgent._fail must push DONE so this feature's stream reliably terminates",
    seen,
)

# ─── start()/stop(): true task cancellation, not just "don't start another" ──
async def _test_stop():
    thread_id_stop = store.create_thread(db, TENANT_A)
    store.add_message(db, thread_id_stop, "user", "A question that will never get answered", "text")

    async def hang_forever(*_a, **_k):
        await asyncio.sleep(30)  # long enough that the test will cancel it well before it returns

    original_client, original_complete = agent.mc.client, agent.mc.complete
    agent.mc.client = object()  # .available is a read-only property computed from .client
    agent.mc.complete = hang_forever
    try:
        assert agent.is_running(thread_id_stop) is False, "nothing has started yet"
        agent.start(thread_id_stop)
        await asyncio.sleep(0.05)  # let the task actually reach the (mocked) model call
        assert agent.is_running(thread_id_stop) is True, (
            "is_running is what the frontend polls to drive its Thinking indicator -- "
            "it must reflect an actually in-flight turn, not just a message-type guess"
        )
        stopped = await agent.stop(thread_id_stop)
        assert stopped is True, "stop() must report that a turn was actually cancelled"
        assert agent.is_running(thread_id_stop) is False, "is_running must clear once stop() has fully awaited the cancellation"
        ui = store.get_ui_messages(db, thread_id_stop)
        assert any(m["content"] == "Stopped." for m in ui), ui
        assert await agent.stop(thread_id_stop) is False, "stopping an already-finished turn is a safe no-op"

        # A second start() while one is already running must not double-step.
        agent.start(thread_id_stop)
        await asyncio.sleep(0.05)
        first_task = agent.tasks[thread_id_stop]
        agent.start(thread_id_stop)
        assert agent.tasks[thread_id_stop] is first_task, "a duplicate start() must not launch a second, interleaved turn"
        await agent.stop(thread_id_stop)
    finally:
        agent.mc.client, agent.mc.complete = original_client, original_complete

asyncio.run(_test_stop())

# ─── Confidence guardrail: a citation-free, evidence-free "high confidence" ──
# is mechanically downgraded to low. Regression guard for a real bug: a
# plain out-of-scope decline ("I can't help with that") was observed live
# labeled high confidence, when rule 7 says exactly that situation should be
# low -- this makes it true even when the model doesn't apply it itself.
thread_no_evidence = store.create_thread(db, TENANT_A)
row_no_evidence = store.get_thread_by_id(db, thread_no_evidence)
no_evidence_answer = {
    "answer": "I can't help with that -- it's outside what this knowledge base covers.",
    "doc_citations": [], "web_citations": [], "confidence": "high",
    "confidence_reason": "The question is out of scope.",
}
result_no_evidence = asyncio.run(agent._publish_answer(thread_no_evidence, row_no_evidence, no_evidence_answer))
assert '"published": true' in result_no_evidence, result_no_evidence
published_no_evidence = _json.loads(
    next(m["content"] for m in store.get_ui_messages(db, thread_no_evidence) if m["type"] == "kb_answer")
)
assert published_no_evidence["confidence"] == "low", (
    "a citation-free, evidence-free answer must never be stored as high confidence, "
    "regardless of what the model itself claimed", published_no_evidence,
)

# With real grounding evidence (a relevant search_knowledge_base result) but
# still no citation quoted, high confidence must NOT be force-downgraded --
# this is exactly the aggregate-count case ("There are 25 documents"),
# which has no doc_citations by design but is genuinely, deterministically
# correct.
thread_with_evidence = store.create_thread(db, TENANT_A)
store.add_message(db, thread_with_evidence, "user", "How many documents are there?", "text")
store.add_message(db, thread_with_evidence, "assistant", "", "tool_call", tool_calls=[
    {"id": "call_agg", "type": "function", "function": {"name": "search_knowledge_base", "arguments": "{}"}},
])
store.add_message(db, thread_with_evidence, "tool",
                   '{"results": [], "relevant": true, "document_count": 25}',
                   "tool_output", tool_call_id="call_agg")
row_with_evidence = store.get_thread_by_id(db, thread_with_evidence)
aggregate_answer = {
    "answer": "There are 25 documents in the knowledge base.",
    "doc_citations": [], "web_citations": [], "confidence": "high",
    "confidence_reason": "The count came directly from the search results.",
}
asyncio.run(agent._publish_answer(thread_with_evidence, row_with_evidence, aggregate_answer))
published_with_evidence = _json.loads(
    next(m["content"] for m in store.get_ui_messages(db, thread_with_evidence) if m["type"] == "kb_answer")
)
assert published_with_evidence["confidence"] == "high", (
    "a genuinely evidenced answer (relevant=true from this turn's own search) must "
    "keep high confidence even with no doc_citations -- an aggregate count has none by design",
    published_with_evidence,
)

# ─── request_web_research ordering guardrail: mechanically refused until ────
# search_knowledge_base has actually been called THIS turn. Regression guard
# for a real bug: asked a general "what is X" product question, the model
# skipped search_knowledge_base entirely and asked to search the web
# immediately -- even though the knowledge base held a directly relevant,
# well-cited answer. Uses a fully mocked model response sequence (no real
# network/model call) so this is deterministic.
from types import SimpleNamespace


def _fake_response(tool_calls, content=None):
    calls = [
        SimpleNamespace(
            id=f"call_{i}", type="function",
            function=SimpleNamespace(name=name, arguments=_json.dumps(args)),
        )
        for i, (name, args) in enumerate(tool_calls)
    ]
    message = SimpleNamespace(content=content, tool_calls=calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


async def _test_web_research_ordering_guardrail():
    thread_id = store.create_thread(db, TENANT_A)
    store.add_message(db, thread_id, "user", "What is some obscure AWS service?", "text")

    call_count = {"n": 0}

    async def fake_complete(messages, tools=None, model=None, timeout=None, temperature=None):
        call_count["n"] += 1
        n = call_count["n"]
        if n == 1:
            # Skips search_knowledge_base entirely -- must be refused.
            return _fake_response([("request_web_research", {"reason": "no KB info"})])
        if n == 2:
            return _fake_response([("search_knowledge_base", {"query": "obscure service"})])
        if n == 3:
            # Now that search_knowledge_base has run this turn, this must succeed.
            return _fake_response([("request_web_research", {"reason": "still no KB info"})])
        return _fake_response([], content="unreachable")

    original_client, original_complete = agent.mc.client, agent.mc.complete
    agent.mc.client = object()
    agent.mc.complete = fake_complete
    try:
        await agent.step(thread_id)
    finally:
        agent.mc.client, agent.mc.complete = original_client, original_complete

    import sqlite3
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    raw = [dict(r) for r in conn.execute(
        "SELECT role, type, content FROM kb_chat_messages WHERE thread_id=? ORDER BY id", (thread_id,)
    ).fetchall()]
    conn.close()

    tool_outputs = [r["content"] for r in raw if r["type"] == "tool_output"]
    assert any('"error": "kb_not_checked"' in o for o in tool_outputs), (
        "the FIRST request_web_research call (before search_knowledge_base) must be "
        "mechanically refused", tool_outputs,
    )
    assert any(m["type"] == "permission_ask" for m in raw), (
        "the SECOND request_web_research call, after search_knowledge_base has run, must succeed",
    )
    assert store.get_thread_by_id(db, thread_id)["pending_web_research"] == "1"


asyncio.run(_test_web_research_ordering_guardrail())


# ─── A rejected submit_kb_answer must NOT end the turn ─────────────────────
# Regression guard for a real bug: the model sent submit_kb_answer with its
# arguments cut short, _publish_answer correctly refused it ("incomplete"),
# and the loop still treated the turn as answered — logged DONE and stopped.
# The user saw the reasoning finish and then nothing, forever. The refusal is
# there so the model can call again; the loop has to give it that chance.
async def _test_incomplete_answer_is_retried():
    thread_id = store.create_thread(db, TENANT_A)
    store.add_message(db, thread_id, "user", "What exhausted the RDS pool?", "text")
    call_count = {"n": 0}

    async def fake_complete(messages, tools=None, model=None, timeout=None, temperature=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _fake_response([("search_knowledge_base", {"query": "rds pool"})])
        if call_count["n"] == 2:
            return _fake_response([("submit_kb_answer", {"answer": "The pool was"})])  # no confidence
        return _fake_response([("submit_kb_answer", complete)])

    original_client, original_complete = agent.mc.client, agent.mc.complete
    agent.mc.client = object()
    agent.mc.complete = fake_complete
    try:
        await agent.step(thread_id)
    finally:
        agent.mc.client, agent.mc.complete = original_client, original_complete

    assert call_count["n"] == 3, f"the model must get another call after a refused answer, got {call_count['n']}"
    answers = [m for m in store.get_ui_messages(db, thread_id) if m["type"] == "kb_answer"]
    assert len(answers) == 1, ("the retried, complete answer must reach the chat", answers)


asyncio.run(_test_incomplete_answer_is_retried())

print("ok: submit_kb_answer rejects incomplete args, a complete answer publishes with brackets "
      "stripped from doc_citations, research-candidate capture records the right tenant/question/"
      "created_by, the no-model-configured path both stores an error message and pushes DONE, "
      "start()/stop() actually cancel a mid-flight turn rather than just gating future ones, an "
      "evidence-free 'high confidence' answer is downgraded to low while a genuinely evidenced "
      "one keeps it, and request_web_research is mechanically refused until search_knowledge_base "
      "has actually been called this turn")
