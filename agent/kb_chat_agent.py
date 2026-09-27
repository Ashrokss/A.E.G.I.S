"""
The Knowledge-Base chat agent.

Separate from SreAgent on purpose, for the same reason sre_agent.py gives for
not importing agent.py: "a much smaller loop that shares the plumbing... and
nothing else." This loop has no AWS incident, no alarm, no JIRA ticket, no
credentials, no AlarmContext -- it never imports tools/aws_observability.py.
It answers questions against a tenant's knowledge base and, only on explicit
per-turn approval, the public web, with the full thread history always in
context so a follow-up ("what about its dependencies?", "that service") can
resolve against what was already discussed.

There is no status machine (NEW -> INVESTIGATING -> ...) the way incidents
have one: a kb-chat thread is just an open conversation, so submit_kb_answer
is offered on every turn rather than gated to a single "investigation" phase.

The FLOW itself -- which tools exist, in what order they may be called, what
a permission ask looks like, and how an answer is normalised before it is
published -- is not defined here. It lives in tools/knowledge_turn.py and is
shared verbatim with the incident chat, so the two surfaces cannot drift into
two different products again.
"""
import asyncio
import json
import logging
import os

from tools import knowledge_base as kb
from tools import kb_chat_store
from tools import knowledge_turn as kt
from tools import web_research
from utils.logger import add_run_log
from utils.model_client import ModelClient, repair_tool_call_history
from utils.skills import load_skill

logger = logging.getLogger("uvicorn.error")

DB_PATH = os.getenv("AGENT_DB_PATH", "runs.db")  # see main.py
_SKILL_PROMPT = load_skill("kb-chat")

KB_CHAT_MAX_TOOL_ITERATIONS = int(os.getenv("KB_CHAT_MAX_TOOL_ITERATIONS", "8"))
KB_CHAT_MAX_MODEL_RETRIES = int(os.getenv("KB_CHAT_MAX_MODEL_RETRIES", "3"))
# Low, not zero: this loop's job is grounded fact-retrieval, not creative
# writing, and live testing found the default (provider-side, ~1.0) sampling
# temperature let an otherwise-correctly-instructed model confirm a
# fabricated premise on a meaningful fraction of otherwise-identical runs of
# the exact same question. Kept low rather than 0 so a genuinely ambiguous
# "I don't know" phrasing isn't forced into false determinism.
KB_CHAT_TEMPERATURE = float(os.getenv("KB_CHAT_TEMPERATURE", "0.2"))

# Both re-exported from tools/knowledge_turn.py rather than defined here: the
# incident chat offers the identical pair, and a second copy is exactly how
# the two surfaces drifted apart before. with_aws_evidence=False is the one
# real difference -- this loop has no AWS evidence to weigh, and saying it
# does would invite reasoning about a capability it doesn't have.
REQUEST_WEB_RESEARCH_SCHEMA = kt.request_web_research_schema(with_aws_evidence=False)
SUBMIT_KB_ANSWER_SCHEMA = kt.SUBMIT_KB_ANSWER_SCHEMA


class KbChatAgent:
    def __init__(self):
        self.mc = ModelClient()
        self.locks: dict[str, asyncio.Lock] = {}
        # Tracked as real asyncio.Tasks (not FastAPI BackgroundTasks callbacks)
        # specifically so stop() can cancel a turn that is actually mid-flight
        # -- e.g. awaiting the model -- not just prevent a future one.
        self.tasks: dict[str, asyncio.Task] = {}

    def _lock(self, thread_id: str) -> asyncio.Lock:
        return self.locks.setdefault(thread_id, asyncio.Lock())

    def is_running(self, thread_id: str) -> bool:
        """Whether this thread's turn is still actually in flight. The
        frontend uses this to drive its "Thinking…" indicator instead of
        guessing from the last message's type -- a guess that broke as soon
        as a turn could end on an ordinary text reply (the request_web_research
        permission question, or the "Stopped." message itself), since neither
        looks different from a mid-turn reply by shape alone."""
        task = self.tasks.get(thread_id)
        return bool(task and not task.done())

    def start(self, thread_id: str) -> None:
        """Launch this thread's turn as a trackable Task. A duplicate call
        while one is already running (e.g. a double-click) is a no-op rather
        than a second, interleaved turn."""
        existing = self.tasks.get(thread_id)
        if existing and not existing.done():
            return
        self.tasks[thread_id] = asyncio.create_task(self.step(thread_id))

    async def stop(self, thread_id: str) -> bool:
        """Cancel this thread's in-flight turn, if any. Returns False if
        nothing was running (the caller can't tell the difference between
        "already finished" and "never started" from this alone, which is
        fine -- both mean there's nothing left to stop)."""
        task = self.tasks.get(thread_id)
        if not task or task.done():
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def step(self, thread_id: str, iterations: int = 0, retries: int = 0):
        """One turn of the conversation. Serialised per thread, same reasoning
        as SreAgent.step: a message arriving mid-turn queues rather than
        interleaving tool calls."""
        async with self._lock(thread_id):
            try:
                await self._step_locked(thread_id, iterations, retries)
            except asyncio.CancelledError:
                kb_chat_store.add_message(DB_PATH, thread_id, "agent", "Stopped.", "text")
                add_run_log(thread_id, "INFO: stopped by user")
                add_run_log(thread_id, "DONE")
                raise
            except Exception as ex:
                logger.exception("[kb-chat] step failed for %s", thread_id)
                self._fail(thread_id, f"I stopped unexpectedly: {ex}")

    def _fail(self, thread_id: str, message: str):
        """Unlike sre_agent.py's _fail, this also pushes "DONE" -- that one
        leaves /api/incidents/{id}/stream hanging open on every failure path
        until the browser's own timeout, which looks like a pre-existing gap
        rather than something intentional. Not fixing that file (out of
        scope here), but not repeating the gap in new code either."""
        kb_chat_store.add_message(DB_PATH, thread_id, "agent", message, "error")
        add_run_log(thread_id, f"ERROR: {message}")
        add_run_log(thread_id, "DONE")

    async def _step_locked(self, thread_id: str, iterations: int, retries: int):
        row = kb_chat_store.get_thread_by_id(DB_PATH, thread_id)
        if not row:
            logger.warning("[kb-chat] step for unknown thread %s", thread_id)
            return

        if not self.mc.available:
            self._fail(thread_id, "No AI model is configured, so I can't answer this.")
            return

        if iterations >= KB_CHAT_MAX_TOOL_ITERATIONS:
            self._fail(
                thread_id,
                f"I used my whole budget of {KB_CHAT_MAX_TOOL_ITERATIONS} tool calls without "
                f"reaching an answer. Ask a narrower question, or check the evidence gathered above.",
            )
            return

        tenant_id = row["tenant_id"]
        messages = [{"role": "system", "content": _SKILL_PROMPT}]
        messages += repair_tool_call_history(kb_chat_store.get_model_messages(DB_PATH, thread_id))

        tools = list(kb.TOOL_SCHEMAS) + [REQUEST_WEB_RESEARCH_SCHEMA, SUBMIT_KB_ANSWER_SCHEMA]
        if row.get("pending_web_research"):
            # One-shot window: opened by request_web_research, closed right
            # after this model call succeeds, regardless of whether it used
            # search_web -- identical mechanics to sre_agent.py.
            tools = tools + list(web_research.TOOL_SCHEMAS)

        try:
            response = await self.mc.complete(
                messages, tools=tools, model=self.mc.model_chat, temperature=KB_CHAT_TEMPERATURE,
            )
        except Exception as ex:
            text = str(ex)
            if retries < KB_CHAT_MAX_MODEL_RETRIES and self.mc.is_transient(text):
                is_rl = self.mc.is_rate_limit(text)
                delay = self.mc.retry_after(ex) or self.mc.backoff(retries, is_rl)
                add_run_log(thread_id, f"INFO: model call failed ({text[:120]}), retrying in {delay:.0f}s")
                await asyncio.sleep(delay)
                return await self._step_locked(thread_id, iterations, retries + 1)
            self._fail(thread_id, f"The model call failed: {text[:300]}")
            return

        message = response.choices[0].message
        tool_calls = getattr(message, "tool_calls", None) or []
        content = (message.content or "").strip()

        if row.get("pending_web_research"):
            # Cleared AFTER a successful response, not before the try/except
            # above -- a transient-error retry must still see the flag.
            kb_chat_store.set_fields(DB_PATH, thread_id, pending_web_research=None)

        if not tool_calls:
            # submit_kb_answer is always offered, so bare prose is always a
            # miss -- keep it (as "draft", not "text": visible to the model on
            # the next turn so it isn't just repeating itself blind, but
            # hidden from the chat panel -- otherwise the user sees this same
            # content twice: once here, and again moments later as the
            # properly-cited kb_answer card once the nudge below lands) and
            # nudge once, every turn (no "only while investigating"
            # carve-out; there is no such phase here).
            kb_chat_store.add_message(DB_PATH, thread_id, "agent", content or "(no answer)", "draft")
            current_question = _last_user_message(thread_id)
            kb_chat_store.add_message(
                DB_PATH, thread_id, "user",
                f"Publish your answer above to \"{current_question}\" by calling "
                "submit_kb_answer now, not to any earlier question in this thread.", "system",
            )
            add_run_log(thread_id, "INFO: prompting for submit_kb_answer")
            return await self._step_locked(thread_id, iterations + 1, retries)

        kb_chat_store.add_message(DB_PATH, thread_id, "assistant", content, "tool_call", tool_calls=tool_calls)

        published = False
        awaiting_user = False
        for call in tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except ValueError:
                args = {}

            if name == "submit_kb_answer":
                result = await self._publish_answer(thread_id, row, args)
                published = kt.was_published(result)
            elif name in ("search_knowledge_base", "check_known_dependencies"):
                add_run_log(thread_id, f"INFO: {_describe_call(name, args)}")
                result = await kb.dispatch(DB_PATH, tenant_id, name, args, user_question=_last_user_message(thread_id))
            elif name == "request_web_research":
                # Mechanical enforcement of rule 1 ("knowledge-base first,
                # always"), not just a prompt instruction: live testing found
                # the model sometimes skips search_knowledge_base entirely
                # for a question that doesn't read as "incident-shaped"
                # (e.g. a general "what is X" product question) and asks to
                # search the web without ever checking what the tenant's own
                # knowledge base has -- even when it genuinely has a highly
                # relevant, correctly-cited answer sitting right there.
                if "search_knowledge_base" not in _tool_names_called(thread_id):
                    result = kt.KB_NOT_CHECKED_RESULT
                else:
                    reason = (args.get("reason") or "").strip() or kt.DEFAULT_PERMISSION_REASON
                    add_run_log(thread_id, f"INFO: Asking permission to search the web — {reason}")
                    kb_chat_store.add_message(
                        DB_PATH, thread_id, "agent", kt.permission_ask_text(reason), "permission_ask",
                    )
                    kb_chat_store.set_fields(DB_PATH, thread_id, pending_web_research="1")
                    result = kt.PERMISSION_ASKED_RESULT
                    awaiting_user = True
            elif name == "search_web":
                if not row.get("pending_web_research"):
                    result = kt.WEB_NOT_APPROVED_RESULT
                else:
                    result = await web_research.dispatch(thread_id, name, args)
                    kb_chat_store.set_fields(DB_PATH, thread_id, pending_candidate_capture="1")
            else:
                result = json.dumps({"error": "unknown_tool", "message": f"{name} is not an available tool."})

            kb_chat_store.add_message(DB_PATH, thread_id, "tool", result, "tool_output", tool_call_id=call.id)

        if published or awaiting_user:
            add_run_log(thread_id, "DONE")
            return
        return await self._step_locked(thread_id, iterations + 1, retries)

    async def _publish_answer(self, thread_id: str, row: dict, answer: dict) -> str:
        # Validation, bracket-stripping and the "an evidence-free answer can
        # never be high confidence" backstop all live in knowledge_turn so
        # the incident chat publishes byte-identical cards.
        turn_rows = kb_chat_store.get_messages_since_last_answer(DB_PATH, thread_id)
        normalized = kt.normalize_answer(answer, turn_rows)
        if normalized is None:
            return kt.ANSWER_INCOMPLETE_RESULT
        payload, doc_citations, web_citations = normalized
        payload["generated_at"] = kb_chat_store.utc_iso()
        # Captured BEFORE this turn's kb_answer is stored below -- _last_user_message
        # walks messages "since the last answer", and this answer closes that
        # boundary the instant it's inserted. Deriving the question after that
        # point always sees an empty turn and returns "", silently reproducing the
        # untitled-document bug this was meant to fix.
        pending_question = _last_user_message(thread_id) if row.get("pending_candidate_capture") else ""

        kb_chat_store.add_message(DB_PATH, thread_id, "agent", json.dumps(payload), "kb_answer")
        kb_chat_store.retype_stale_drafts(DB_PATH, thread_id)

        tenant_id = row["tenant_id"]
        if row.get("pending_candidate_capture"):
            # Always clear the flag once this answer is published, whether or
            # not it actually gets captured -- otherwise an answer with no
            # citations (search_web found nothing) leaves the flag set
            # forever, and a LATER, unrelated answer in this same thread that
            # happens to cite the knowledge base would be wrongly captured
            # too. Only create a candidate row when there is actually
            # something worth an admin reviewing.
            try:
                if doc_citations or web_citations:
                    await self._maybe_capture_research_candidate(
                        thread_id, tenant_id, pending_question, answer["answer"], doc_citations, web_citations
                    )
                else:
                    # Nothing to review, but the search still happened and the
                    # user consented to it — file the question as a gap so it
                    # is visible somewhere instead of vanishing.
                    await kt.record_empty_research(DB_PATH, tenant_id, pending_question)
                    kb_chat_store.set_fields(DB_PATH, thread_id, pending_candidate_capture=None)
            except Exception as ex:
                logger.warning("[kb-chat] research-candidate capture failed for %s: %s", thread_id, ex)

        return kt.ANSWER_PUBLISHED_RESULT

    async def _maybe_capture_research_candidate(
        self, thread_id: str, tenant_id: str, question: str, answer_text: str,
        doc_citations: list[str], web_citations: list[str],
    ) -> None:
        """Save a web-research-augmented answer to the approval queue -- same
        mechanism sre_agent.py uses, minus an incident_id. Citations come
        straight from the model's own structured submit_kb_answer arguments
        rather than being re-extracted from prose, since we already have
        them as ground truth. `question` is captured by the caller before this
        turn's kb_answer row is stored -- see the comment at that call site."""
        gap_id = kb.gap_id_for_question(tenant_id, question) if question else None
        await asyncio.to_thread(
            kb.create_research_candidate, DB_PATH, tenant_id, gap_id, question,
            answer_text, doc_citations, web_citations, "kb_chat_agent",
        )
        kb_chat_store.set_fields(DB_PATH, thread_id, pending_candidate_capture=None)


# The three turn-scoped checks below are thin thread-id-shaped wrappers over
# tools/knowledge_turn.py's store-agnostic versions -- the incident chat runs
# the identical checks over its own store's rows.

def _tool_names_called(thread_id: str) -> set[str]:
    return kt.tool_names_called(kb_chat_store.get_messages_since_last_answer(DB_PATH, thread_id))


def _has_grounding_evidence(thread_id: str) -> bool:
    return kt.has_grounding_evidence(kb_chat_store.get_messages_since_last_answer(DB_PATH, thread_id))


def _last_user_message(thread_id: str) -> str:
    return kt.first_user_question(kb_chat_store.get_messages_since_last_answer(DB_PATH, thread_id))


def _describe_call(name: str, args: dict) -> str:
    """Human-readable progress line for the SSE trace."""
    if name == "search_knowledge_base":
        query = (args.get("query") or "")[:60]
        return f"Searching the knowledge base for '{query}'"
    if name == "check_known_dependencies":
        return f"Checking learned dependencies for {args.get('service', 'this service')}"
    return f"Running {name}"
