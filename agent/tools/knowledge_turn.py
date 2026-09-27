"""
The single knowledge-retrieval-and-approval contract, shared by every chat
surface in the product.

There used to be two copies of this: one in kb_chat_agent.py (the standalone
knowledge-base chat) and one, partially, in sre_agent.py (the incident chat).
They drifted -- the incident chat asked for web-research permission as plain
prose (so the UI could not offer Yes/No), never published a citation-separated
answer card, and never enforced "check the knowledge base before asking to
search the web". That made "the Chat tab" and "the Incidents chat" two
different products sharing a database.

Everything that defines the flow now lives here, exactly once:

    search_knowledge_base  ─▶ enough?  ─yes─▶ submit_kb_answer  (the answer card)
                              │
                              no
                              ▼
                     request_web_research  ─▶ permission_ask message (Yes/No)
                              │
                        user approves
                              ▼
                          search_web  ─▶ submit_kb_answer
                                              │
                                              ▼
                              kb_research_candidates (Web Findings queue)
                                              │
                                        admin approves
                                              ▼
                                       kb_documents (the knowledge base)

Both agents import this module rather than defining their own schemas,
ordering guards or confidence backstops. The two stores stay separate (an
incident's transcript lives in `messages`, a thread's in `kb_chat_messages`),
so every helper here takes already-fetched message ROWS instead of a db_path
-- the row shape both stores produce is {role, type, content, tool_calls}.
record_empty_research is the one exception: the gap it writes is keyed on
(tenant, question) with no incident or thread in it at all, so it needs the
db_path and nothing store-shaped.
"""
import asyncio
import json

from tools import knowledge_base as kb

CONFIDENCE = ("high", "medium", "low")

# The terminal tool for a knowledge question. Offered on every turn of the
# kb-chat lane and on every non-investigating turn of the incident lane --
# during an active investigation submit_rca is the terminal tool instead, so
# a half-finished investigation cannot end on a chat answer.
SUBMIT_KB_ANSWER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_kb_answer",
        "description": (
            "Publish the final answer to the user's question. Call this exactly once per "
            "turn, after you have gathered enough evidence from the knowledge base (and, if "
            "approved, the web) to answer. This renders the answer in the chat as a "
            "structured card that keeps knowledge-base-sourced and web-sourced content "
            "visibly separate. Never write the final answer as an ordinary chat message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {
                    "type": "string",
                    "description": (
                        "The consolidated answer, in Markdown. Cite a knowledge-base passage "
                        "inline with its citation_tag exactly as shown, e.g. '[cite_key · "
                        "section]'; cite a web source inline as '(web: <url>)'. If nothing in "
                        "the knowledge base or approved web research answers this, say so "
                        "plainly instead of guessing."
                    ),
                },
                "doc_citations": {
                    "type": "array",
                    "description": (
                        "Every knowledge-base citation_tag actually relied on (matching a tag "
                        "search_knowledge_base returned). Empty array if none were used. "
                        "Never invent one."
                    ),
                    "items": {"type": "string"},
                },
                "web_citations": {
                    "type": "array",
                    "description": (
                        "Every web source URL actually relied on, exactly as search_web "
                        "returned it. Empty array unless search_web was called and approved "
                        "for this thread."
                    ),
                    "items": {"type": "string"},
                },
                "confidence": {
                    "type": "string",
                    "enum": list(CONFIDENCE),
                    "description": "Use low when neither the knowledge base nor approved web research support a firm answer.",
                },
                "confidence_reason": {
                    "type": "string",
                    "description": "One clause explaining the confidence level.",
                },
            },
            "required": ["answer", "doc_citations", "web_citations", "confidence"],
        },
    },
}


def request_web_research_schema(with_aws_evidence: bool = False) -> dict:
    """Asking permission is itself a tool call, not free text, so it is
    trackable: calling it sets the store's `pending_web_research` flag, the
    mechanical gate that is the ONLY way search_web's schema is ever offered.
    A model choosing to write a plain-text question instead would not open
    that gate.

    `with_aws_evidence` is the one legitimate difference between the two
    lanes: the incident chat really does have AWS evidence to weigh first,
    and the standalone knowledge-base chat really does not -- claiming
    otherwise there would invite reasoning about a capability that loop
    doesn't have. Everything else about the flow is identical, which is why
    this is a parameter rather than a second schema.
    """
    missing = (
        "the knowledge base and AWS evidence gathered so far are"
        if with_aws_evidence
        else "the tenant's knowledge base gathered so far is"
    )
    return {
        "type": "function",
        "function": {
            "name": "request_web_research",
            "description": (
                f"Ask the user's permission to search the public web, because {missing} not "
                "enough to answer confidently. Call this instead of guessing or refusing "
                "outright when web research could plausibly help. After calling it, stop and "
                "wait — search_web only becomes available once the user replies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "One sentence, shown to the user, on what is missing.",
                    },
                },
                "required": ["reason"],
            },
        },
    }


# ─── Standard tool results ──────────────────────────────────────────────────
# Written once so both lanes return byte-identical guidance to the model; the
# incident chat previously had no equivalent of the first two at all.

KB_NOT_CHECKED_RESULT = json.dumps({
    "error": "kb_not_checked",
    "message": "Call search_knowledge_base for this exact question first — "
               "you have not done that yet this turn. Never request web "
               "research before checking the knowledge base, regardless of "
               "how the question is phrased.",
})

WEB_NOT_APPROVED_RESULT = json.dumps({
    "error": "not_approved",
    "message": "search_web is only available immediately after the user "
               "approves a request_web_research call.",
})

PERMISSION_ASKED_RESULT = json.dumps({
    "ok": True,
    "message": "Permission request shown to the user. Wait for their reply — "
               "search_web is not available until they approve.",
})

ANSWER_INCOMPLETE_RESULT = json.dumps({
    "error": "incomplete",
    "message": "submit_kb_answer needs at least answer and confidence. Call it again with both.",
})

ANSWER_PUBLISHED_RESULT = json.dumps({
    "ok": True, "published": True, "message": "The answer is published in the chat.",
})


def was_published(result: str) -> bool:
    """Whether a submit_kb_answer / submit_rca call actually stored its card.

    Only then is the turn over. A refusal (ANSWER_INCOMPLETE_RESULT, or the
    RCA's own "incomplete") exists so the model can call again with the
    missing fields -- ending the turn on it leaves the user with a finished
    reasoning trace and no answer, which is what both lanes used to do.
    """
    try:
        return json.loads(result).get("published") is True
    except (ValueError, TypeError, AttributeError):
        return False

DEFAULT_PERMISSION_REASON = (
    "I don't have enough information in the knowledge base to answer confidently."
)


def permission_ask_text(reason: str | None) -> str:
    """The exact prose shown in the `permission_ask` bubble both chat panels
    render Yes/No buttons under."""
    reason = (reason or "").strip() or DEFAULT_PERMISSION_REASON
    return f"{reason} Would you like me to search the web?"


# ─── Turn-scoped mechanical checks ──────────────────────────────────────────
# These operate on the rows of the CURRENT, still-open turn (everything after
# the last published answer), which each store fetches its own way. They exist
# because the model cannot be trusted to remember its own rules from one call
# to the next -- live testing proved that for both of them.

def tool_names_called(rows: list[dict]) -> set[str]:
    """Every tool name this turn has actually called so far -- used to enforce
    call ORDER (search_knowledge_base before request_web_research)."""
    names: set[str] = set()
    for m in rows:
        if m.get("type") != "tool_call" or not m.get("tool_calls"):
            continue
        raw = m["tool_calls"]
        try:
            calls = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        for tc in calls or []:
            fn = (tc.get("function") or {}).get("name") if isinstance(tc, dict) else None
            if fn:
                names.add(fn)
    return names


def has_grounding_evidence(rows: list[dict]) -> bool:
    """Whether this turn's own tool calls produced anything a confident answer
    could actually rest on: a genuinely relevant knowledge-base result (this
    also covers an aggregate document_count/document_index answer, whose
    branch always forces relevant=True) or a non-empty dependency-graph
    lookup. Live testing found a plain, evidence-free decline ("I can't help
    with that") labeled high confidence often enough that publishing needs
    this as a real backstop, not just the prompt asking nicely."""
    for m in rows:
        if m.get("type") != "tool_output":
            continue
        try:
            payload = json.loads(m.get("content") or "{}")
        except ValueError:
            continue
        if payload.get("relevant") is True:
            return True
        if payload.get("downstream"):
            return True
    return False


def first_user_question(rows: list[dict]) -> str:
    """The question that opened the current turn -- the FIRST user message
    since the last resolved answer, not the most recent one. A turn that goes
    through the web-research permission flow has a later user message that is
    just "yes", which is not a question worth filing a research candidate
    under."""
    for m in rows:
        if m.get("role") == "user" and m.get("type") == "text":
            return m.get("content") or ""
    return ""


def normalize_answer(args: dict, rows: list[dict]) -> tuple[dict, list[str], list[str]] | None:
    """Validate and normalise a submit_kb_answer payload, or None when it is
    too incomplete to publish. Returns (payload, doc_citations, web_citations).

    `rows` is the current turn's transcript, used only for the confidence
    backstop -- see has_grounding_evidence.
    """
    if not args.get("answer") or not args.get("confidence"):
        return None

    confidence = args.get("confidence") if args.get("confidence") in CONFIDENCE else "low"
    # Stripped of enclosing brackets to match the "cite_key · section" shape
    # knowledge_base.extract_citations produces, so one approval-queue UI can
    # render candidates from either lane.
    doc_citations = [str(c).strip().strip("[]").strip() for c in (args.get("doc_citations") or [])]
    web_citations = [str(c).strip() for c in (args.get("web_citations") or [])]
    if confidence == "high" and not doc_citations and not web_citations \
            and not has_grounding_evidence(rows):
        confidence = "low"

    payload = {
        "answer": args["answer"],
        "doc_citations": doc_citations,
        "web_citations": web_citations,
        "confidence": confidence,
        "confidence_reason": args.get("confidence_reason") or "",
    }
    return payload, doc_citations, web_citations


async def record_empty_research(db_path: str, tenant_id: str, question: str) -> None:
    """A consented web search that produced no citations still has to leave a
    mark somewhere the operator can find.

    Both lanes already skip the Web Findings queue in this case, and rightly
    so -- there is no finding to review, and filing an empty candidate would
    put a row in front of an admin with nothing in it to approve. But the
    result was that granting permission, watching a search run, and getting
    "I could not find anything" left no trace in ANY queue, which reads as the
    feature being broken rather than as the honest answer it is.

    A question the knowledge base could not answer and the web could not
    answer either is precisely a knowledge gap, so that is where it lands.
    record_gap keys on (tenant, question), so asking the same thing twice
    updates the one row instead of piling up duplicates.
    """
    if not question.strip():
        return
    await asyncio.to_thread(
        kb.record_gap, db_path, tenant_id, question, "web_research_empty", 0.0,
    )
