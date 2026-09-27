"""
Storage for the Knowledge-Base chat: a multi-turn, tenant-scoped conversation
over the knowledge base and (on explicit approval) the web, with no live AWS
incident behind it.

Deliberately NOT built on tools/sre_store.py's `incidents`/`messages` tables,
even though the shapes look similar. Two concrete reasons, not a style
preference: sre_store.add_message unconditionally runs
`UPDATE incidents SET updated_at = ?, last_message = ? WHERE id = ?` after
every insert -- a kb-chat thread id would never match a row there, so every
message would silently perform a permanent, matching-nothing UPDATE. And
sre_store's `_MODEL_EXCLUDED_TYPES`/`_UI_EXCLUDED_TYPES` are shaped around
incident-only message types (`rca`, `tf_step`, `jira_tickets`). Mirrors that
module's shape closely enough to stay familiar; kb_chat_agent.py is the
kb-chat analogue of sre_agent.py for the same reason sre_agent.py itself
gives for not reusing agent.py's loop: "a much smaller loop that shares the
plumbing... and nothing else."
"""
import json
import logging
import sqlite3
import uuid

from datetime import datetime


def utc_iso() -> str:
    """Naive-UTC ISO, matching `datetime.utcnow().isoformat()` used elsewhere."""
    return datetime.utcnow().isoformat()

logger = logging.getLogger("uvicorn.error")

# Excluded from model replay: a kb_answer row is the rendered card, and its
# content duplicates the submit_kb_answer tool_call's own arguments already
# present in the transcript -- replaying both would waste context for nothing,
# same reasoning sre_store.py gives for excluding `rca`. `draft_stale` is what
# a `draft`/its paired `system` nudge get retyped to once the turn they
# belonged to actually publishes (see retype_to_stale_draft) -- once an
# answer is published, replaying the scratch work that led to it is not just
# wasted context, it is actively harmful: a later, unrelated question in the
# same thread can end up answered from that leftover content instead of its
# own topic (confirmed root cause of a real cross-question contamination bug).
_MODEL_EXCLUDED_TYPES = ("kb_answer", "draft_stale")

_ROLE_MAP = {
    "agent": "assistant",
    "assistant": "assistant",
    "user": "user",
    "system": "system",
    "developer": "developer",
    "tool": "tool",
    "function": "function",
}

# Excluded from the chat panel: tool plumbing the user never needs to see.
# `draft` is a bare-prose answer that missed submit_kb_answer and is about to
# be re-asked for on the very next turn -- showing it would mean the user
# sees the same content twice (once as this draft, again as the eventual
# kb_answer card). Still replayed to the model (not in _MODEL_EXCLUDED_TYPES)
# so it isn't just repeating itself blind. `draft_stale` is the same content
# after its turn has actually published -- never shown either. No
# `internal_log` here -- this module never writes that type.
_UI_EXCLUDED_TYPES = ("tool_output", "tool", "system", "tool_call", "draft", "draft_stale")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_table(db_path: str) -> None:
    """Create the kb-chat tables if absent. Called from main.init_db(), same
    as sre_store.ensure_table() / knowledge_base.ensure_table()."""
    conn = _connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_threads (
            id                        TEXT PRIMARY KEY,
            tenant_id                 TEXT NOT NULL,
            title                     TEXT,
            created_by                TEXT,
            pending_web_research      TEXT,
            pending_candidate_capture TEXT,
            last_message              TEXT,
            created_at                TIMESTAMP NOT NULL,
            updated_at                TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_threads_tenant ON kb_threads(tenant_id, updated_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kb_chat_messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_id    TEXT NOT NULL,
            role         TEXT NOT NULL,
            content      TEXT,
            type         TEXT NOT NULL DEFAULT 'text',
            tool_calls   TEXT,
            tool_call_id TEXT,
            created_at   TIMESTAMP NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_kb_chat_messages_thread ON kb_chat_messages(thread_id, id)"
    )
    conn.commit()
    conn.close()


# ─── Threads ────────────────────────────────────────────────────────────────

def create_thread(db_path: str, tenant_id: str, created_by: str | None = None) -> str:
    thread_id = str(uuid.uuid4())
    now = utc_iso()
    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO kb_threads (id, tenant_id, created_by, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (thread_id, tenant_id, created_by, now, now),
    )
    conn.commit()
    conn.close()
    return thread_id


def get_thread(
    db_path: str, tenant_id: str, thread_id: str, created_by: str | None = None
) -> dict | None:
    """Tenant-scoped in the SQL, not a post-fetch Python comparison -- a wrong
    tenant_id returns None rather than relying on every call site to remember
    to check, same convention knowledge_base.delete_document uses.

    `created_by` narrows that to one person's own threads. A chat is private to
    whoever started it: tenant_id is a single constant in this deployment, so
    without this every signed-in user reads every other user's conversation.
    Passing it here rather than comparing after the fetch means a call site that
    forgets gets a wider result, never a leak dressed up as a check."""
    sql = "SELECT * FROM kb_threads WHERE id = ? AND tenant_id = ?"
    params: list = [thread_id, tenant_id]
    if created_by is not None:
        sql += " AND created_by = ?"
        params.append(created_by)
    conn = _connect(db_path)
    row = conn.execute(sql, tuple(params)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_thread_by_id(db_path: str, thread_id: str) -> dict | None:
    """Unscoped lookup for the agent loop's own internal use, once the
    endpoint that queued it has already tenant-checked the thread."""
    conn = _connect(db_path)
    row = conn.execute("SELECT * FROM kb_threads WHERE id = ?", (thread_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_threads(
    db_path: str, tenant_id: str, created_by: str | None = None, limit: int = 50
) -> list[dict]:
    """`created_by` restricts the list to that person's own chats. See
    get_thread for why a chat is private to whoever started it."""
    sql = "SELECT * FROM kb_threads WHERE tenant_id = ?"
    params: list = [tenant_id]
    if created_by is not None:
        sql += " AND created_by = ?"
        params.append(created_by)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    conn = _connect(db_path)
    rows = conn.execute(sql, tuple(params)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_fields(db_path: str, thread_id: str, **fields) -> None:
    """Column names are never user input -- call sites pass literals."""
    if not fields:
        return
    assignments = ", ".join(f"{col} = ?" for col in fields)
    conn = _connect(db_path)
    conn.execute(
        f"UPDATE kb_threads SET {assignments}, updated_at = ? WHERE id = ?",
        (*fields.values(), utc_iso(), thread_id),
    )
    conn.commit()
    conn.close()


def set_title_if_unset(db_path: str, thread_id: str, title: str) -> None:
    conn = _connect(db_path)
    conn.execute(
        "UPDATE kb_threads SET title = ? WHERE id = ? AND (title IS NULL OR title = '')",
        (title, thread_id),
    )
    conn.commit()
    conn.close()


# ─── Messages ───────────────────────────────────────────────────────────────

def add_message(
    db_path: str,
    thread_id: str,
    role: str,
    content: str,
    msg_type: str = "text",
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
) -> None:
    now = utc_iso()
    tool_calls_json = None
    if tool_calls:
        tool_calls_json = json.dumps([
            {
                "id": tc.id if hasattr(tc, "id") else tc.get("id"),
                "type": tc.type if hasattr(tc, "type") else tc.get("type", "function"),
                "function": {
                    "name": tc.function.name if hasattr(tc, "function") else tc.get("function", {}).get("name"),
                    "arguments": tc.function.arguments if hasattr(tc, "function") else tc.get("function", {}).get("arguments"),
                },
            }
            for tc in tool_calls
        ]) if isinstance(tool_calls, list) else json.dumps(tool_calls)

    conn = _connect(db_path)
    conn.execute(
        "INSERT INTO kb_chat_messages (thread_id, role, content, type, tool_calls, tool_call_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (thread_id, role, content, msg_type, tool_calls_json, tool_call_id, now),
    )
    conn.execute(
        "UPDATE kb_threads SET updated_at = ?, last_message = ? WHERE id = ?",
        (now, (content or "")[:140], thread_id),
    )
    conn.commit()
    conn.close()


def retype_stale_drafts(db_path: str, thread_id: str) -> None:
    """Called once a turn actually publishes a kb_answer: retypes every
    `draft`/`system` row in this thread (the bare-prose miss and its "publish
    that now" nudge) to `draft_stale`, so neither is ever replayed to the
    model again. Left in place (as `draft`/`system`) they linger in every
    future turn's context forever, and a later, unrelated question can end up
    answered from that stale content instead of its own -- an anaphoric
    "publish THAT now" instruction sitting next to a complete answer-shaped
    paragraph is exactly what the model latches onto when it has nothing else
    answer-shaped for a new, equally-unanswerable question. `system` rows are
    safe to retype unconditionally: this module's only writer of that type is
    the nudge itself (kb_chat_agent.py's `not tool_calls` branch)."""
    conn = _connect(db_path)
    conn.execute(
        "UPDATE kb_chat_messages SET type = 'draft_stale' "
        "WHERE thread_id = ? AND type IN ('draft', 'system')",
        (thread_id,),
    )
    conn.commit()
    conn.close()


# How many of the most recent RESOLVED turns (ending in kb_answer or error)
# keep their full tool-call/tool-output scaffolding when replayed to the
# model. Anything older collapses to just its original question + the
# published answer's own text. Two real problems this fixes, not just
# token cost: (1) a long thread's full tool-output history is mostly raw
# retrieved-chunk JSON from many DIFFERENT, often-unrelated documents --
# once a thread has covered a dozen topics, that's a lot of unrelated
# document text competing for the model's attention on every new question,
# a real contributor to a later, unrelated question answering itself from
# a topically-adjacent OLD turn's evidence instead of doing its own fresh
# lookup; (2) the model never actually saw its OWN past answers before this
# change (kb_answer has always been excluded from replay, since its content
# duplicates the submit_kb_answer tool-call's own arguments) -- collapsing
# gives older turns a clean "Q: ... / A: ..." record instead of nothing at
# all, which is a strictly better summary than either extreme.
_KEEP_FULL_RECENT_TURNS = 3

# A turn is "resolved" once it reaches one of these -- kb_answer (a real
# answer was published) or error (the turn gave up). A permission_ask alone
# does NOT resolve a turn -- the user's reply and the eventual answer are
# still part of the SAME turn, so a whole "asked permission, declined,
# answered honestly" exchange collapses as one Q/A unit, not two.
_TURN_TERMINAL_TYPES = ("kb_answer", "error")


def get_messages_since_last_answer(db_path: str, thread_id: str) -> list[dict]:
    """Every raw row in this thread after the most recent kb_answer (or from
    the start, if none yet) -- the current, still-open turn: the user's
    question, plus any tool_call/tool_output/draft/nudge messages this
    turn's own tool loop has produced so far. Used to mechanically check
    what THIS turn has actually done (e.g. did it call search_knowledge_base
    before request_web_research, or does it have any real evidence to be
    confident about) rather than trusting the model to remember its own
    rules from one call to the next."""
    conn = _connect(db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, role, type, content, tool_calls, tool_call_id FROM kb_chat_messages "
        "WHERE thread_id = ? ORDER BY id ASC", (thread_id,)
    ).fetchall()]
    conn.close()
    last_answer_id = max((r["id"] for r in rows if r["type"] == "kb_answer"), default=-1)
    return [r for r in rows if r["id"] > last_answer_id]


def get_model_messages(db_path: str, thread_id: str) -> list[dict]:
    """Transcript in the shape the model API expects. UI-only types are
    dropped (see _MODEL_EXCLUDED_TYPES) and every resolved turn older than
    the _KEEP_FULL_RECENT_TURNS most recent ones is collapsed to its
    question + published answer -- see _KEEP_FULL_RECENT_TURNS above."""
    conn = _connect(db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, role, content, type, tool_calls, tool_call_id FROM kb_chat_messages "
        "WHERE thread_id = ? ORDER BY id ASC", (thread_id,)
    ).fetchall()]
    conn.close()

    turns: list[list[dict]] = []
    current: list[dict] = []
    for r in rows:
        current.append(r)
        if r["type"] in _TURN_TERMINAL_TYPES:
            turns.append(current)
            current = []
    open_turn = current  # not yet resolved -- never collapsed

    if _KEEP_FULL_RECENT_TURNS and len(turns) > _KEEP_FULL_RECENT_TURNS:
        collapsed_turns = turns[:-_KEEP_FULL_RECENT_TURNS]
        kept_turns = turns[-_KEEP_FULL_RECENT_TURNS:]
    else:
        collapsed_turns, kept_turns = [], turns

    out_rows: list[dict] = []
    for turn in collapsed_turns:
        question = next((r["content"] for r in turn if r["role"] == "user" and r["type"] == "text"), None)
        answer_row = next((r for r in reversed(turn) if r["type"] == "kb_answer"), None)
        if question is None or answer_row is None:
            out_rows.extend(turn)  # an error-ended (or otherwise unusual) turn: leave it uncollapsed
            continue
        try:
            answer_text = json.loads(answer_row["content"]).get("answer") or answer_row["content"]
        except (ValueError, AttributeError, TypeError):
            answer_text = answer_row["content"]
        out_rows.append({"role": "user", "type": "text", "content": question, "tool_calls": None, "tool_call_id": None})
        out_rows.append({"role": "assistant", "type": "text", "content": answer_text, "tool_calls": None, "tool_call_id": None})
    for turn in kept_turns:
        out_rows.extend(turn)
    out_rows.extend(open_turn)

    messages = []
    for r in out_rows:
        if r["type"] in _MODEL_EXCLUDED_TYPES:
            continue
        msg = {"role": _ROLE_MAP.get(r["role"], "assistant"), "content": r["content"]}
        if r["tool_calls"]:
            tc = r["tool_calls"]
            msg["tool_calls"] = json.loads(tc) if isinstance(tc, str) else tc
        if r["tool_call_id"]:
            msg["tool_call_id"] = r["tool_call_id"]
        messages.append(msg)
    return messages


def get_ui_messages(db_path: str, thread_id: str) -> list[dict]:
    """Transcript for the chat panel. Keeps `kb_answer` (the card) and `text`,
    drops the model plumbing -- see _UI_EXCLUDED_TYPES."""
    placeholders = ", ".join("?" for _ in _UI_EXCLUDED_TYPES)
    conn = _connect(db_path)
    rows = conn.execute(
        f"SELECT role, content, type, created_at FROM kb_chat_messages "
        f"WHERE thread_id = ? AND (type IS NULL OR type NOT IN ({placeholders})) "
        f"AND role != 'system' ORDER BY id ASC",
        (thread_id, *_UI_EXCLUDED_TYPES),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
