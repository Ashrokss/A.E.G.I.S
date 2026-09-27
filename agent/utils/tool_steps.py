"""The tool steps behind an agent turn, as the chat panel's reasoning timeline
shows them ("Searched the knowledge base · query: ALB 5xx").

Names, a one-line argument summary, and the model's own short narration
beside the call (capped, once per turn). Tool OUTPUTS never leave the
server this way: they are raw AWS API responses and retrieved documents,
which would bloat every snapshot poll and put data in the browser that the
answer card already summarises. `get_ui_messages` still drops the tool_call
rows themselves, so no existing reader of the transcript changes.

All three chats store tool calls the same way — a JSON list of
`{id, type, function: {name, arguments}}` in a `tool_calls` column, where
`arguments` is the model's own JSON string — so one reader serves them all.
"""
import json
import sqlite3

ARGS_MAX_CHARS = 160

# Tools whose arguments ARE the reply (the answer card, the RCA card): the
# card already shows them, so repeating them under the step is noise, and
# shipping them again on every snapshot poll is waste.
ARGS_HIDDEN = {"submit_kb_answer"}

# The model's narration beside a tool call, shown under that step. Capped: it
# rides on every snapshot poll, and past a few sentences it is the answer
# leaking into the reasoning rather than reasoning.
THOUGHT_MAX_CHARS = 400

# (table, key column) pairs a snapshot may read. Fixed here, never taken from
# a request: they are interpolated into the SQL below.
TABLES = {
    "messages": "run_id",            # provisioning runs
    "kb_chat_messages": "thread_id",  # knowledge-base chat threads
}


def summarize_args(arguments) -> str:
    """`{"query": "ALB 5xx", "k": 5}` -> `query: ALB 5xx · k: 5`, capped."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except ValueError:
            # Not JSON (a model occasionally sends a bare string): show it as is.
            return _cap(arguments.strip())
    if not isinstance(arguments, dict):
        return _cap(json.dumps(arguments)) if arguments else ""
    parts = []
    for key, value in arguments.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"))
        parts.append(f"{key}: {value}")
    return _cap(" · ".join(parts))


def _cap(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= ARGS_MAX_CHARS else text[: ARGS_MAX_CHARS - 1] + "…"


def steps_from_row(created_at, tool_calls_json, content: str = "") -> list[dict]:
    """The steps one assistant row made. `content` is what the model said
    alongside them ("Let me check the ECS service..."): it becomes the first
    step's `thought`, once per row -- attaching it to every call repeated the
    same paragraph under each of them."""
    try:
        calls = json.loads(tool_calls_json or "[]")
    except ValueError:
        return []
    if not isinstance(calls, list):
        return []
    steps = []
    thought = _cap_thought(content)
    for call in calls:
        fn = (call or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        hidden = name in ARGS_HIDDEN
        step = {"created_at": created_at, "name": name, "args": "" if hidden else summarize_args(fn.get("arguments"))}
        # Beside submit_kb_answer / submit_rca the narration is usually the
        # answer itself, which the card right below already shows.
        if thought and not hidden:
            step["thought"] = thought
            thought = ""
        steps.append(step)
    return steps


def _cap_thought(content) -> str:
    text = (content or "").strip()
    return text if len(text) <= THOUGHT_MAX_CHARS else text[: THOUGHT_MAX_CHARS - 1].rstrip() + "…"


def load_steps(db_path: str, table: str, key: str) -> list[dict]:
    """Every tool step recorded for one run / incident / thread, oldest first."""
    key_col = TABLES[table]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT created_at, tool_calls, content FROM {table} "
            f"WHERE {key_col} = ? AND tool_calls IS NOT NULL ORDER BY id ASC",
            (key,),
        ).fetchall()
    finally:
        conn.close()
    return [step for created_at, calls, content in rows for step in steps_from_row(created_at, calls, content)]


if __name__ == "__main__":
    import os
    import tempfile

    assert summarize_args('{"query": "ALB 5xx", "k": 5}') == "query: ALB 5xx · k: 5"
    assert summarize_args("") == ""
    assert summarize_args("not json") == "not json"
    assert summarize_args({"ids": ["a", "b"]}) == 'ids: ["a","b"]'
    long = summarize_args({"q": "x" * 500})
    assert len(long) == ARGS_MAX_CHARS and long.endswith("…")
    assert steps_from_row("t", "garbage") == []
    assert steps_from_row("t", '[{"function": {"arguments": "{}"}}]') == []  # nameless call dropped
    assert steps_from_row("t", '[{"function": {"name": "submit_kb_answer", "arguments": "{\\"answer\\": \\"long\\"}"}}]') == [
        {"created_at": "t", "name": "submit_kb_answer", "args": ""}
    ]

    # Narration: once per row (not per call), on the first call.
    three = json.dumps([{"function": {"name": n, "arguments": "{}"}} for n in ("a", "b", "c")])
    got = steps_from_row("t", three, "  Let me check a, b and c.  ")
    assert [s.get("thought") for s in got] == ["Let me check a, b and c.", None, None], got
    # ...never beside an answer-publishing tool: it is the answer itself.
    answer = json.dumps([{"function": {"name": "submit_kb_answer", "arguments": "{}"}}])
    assert "thought" not in steps_from_row("t", answer, "Yes. The root cause was ...")[0]
    # ...skips a hidden call to land on the next visible one.
    mixed = json.dumps([{"function": {"name": "submit_rca", "arguments": "{}"}},
                        {"function": {"name": "search_logs", "arguments": "{}"}}])
    assert [s.get("thought") for s in steps_from_row("t", mixed, "why")] == [None, "why"]
    # ...and capped.
    capped = steps_from_row("t", three, "x" * 5000)[0]["thought"]
    assert len(capped) == THOUGHT_MAX_CHARS and capped.endswith("…")
    assert "thought" not in steps_from_row("t", three, "   ")[0]  # blank narration: none

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, run_id TEXT, created_at TEXT, tool_calls TEXT, content TEXT)")
        conn.executemany(
            "INSERT INTO messages (run_id, created_at, tool_calls) VALUES (?, ?, ?)",
            [
                ("r1", "2026-09-23T10:00:01", json.dumps([
                    {"id": "1", "type": "function", "function": {"name": "search_knowledge_base", "arguments": '{"query": "5xx"}'}},
                    {"id": "2", "type": "function", "function": {"name": "get_metrics", "arguments": "{}"}},
                ])),
                ("r1", "2026-09-23T10:00:02", None),
                ("r2", "2026-09-23T10:00:03", json.dumps([{"function": {"name": "other", "arguments": "{}"}}])),
            ],
        )
        conn.commit()
        conn.close()
        steps = load_steps(path, "messages", "r1")
        assert [s["name"] for s in steps] == ["search_knowledge_base", "get_metrics"], steps
        assert steps[0] == {"created_at": "2026-09-23T10:00:01", "name": "search_knowledge_base", "args": "query: 5xx"}
        try:
            load_steps(path, "users; DROP TABLE messages", "r1")
            raise AssertionError("an unlisted table must be refused")
        except KeyError:
            pass
    finally:
        os.remove(path)
    print("tool_steps self-check OK")
