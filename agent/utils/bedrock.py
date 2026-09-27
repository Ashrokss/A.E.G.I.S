"""Bedrock Converse <-> OpenAI-shaped plumbing.

Two jobs, both pure and both testable without AWS:

1. Translate the OpenAI-shaped message dicts this repo stores in SQLite into
   what the Converse API accepts, and the tool list into a `toolConfig`.
2. Translate a Converse response back into the `.choices[0].message.tool_calls`
   shape every caller already reads.

The response shim exists so the swap stays inside `ModelClient`. Callers reach
`response.choices[0].message.tool_calls[0].function.arguments` and `json.loads`
it, and two store modules (`sre_store`, `kb_chat_store`) duck-type the same
objects with `hasattr` while also handling replayed dicts. Preserving the shape
costs ~20 lines here; changing it would spread three shapes across eight files.

Converse is stricter than the OpenAI API in four ways this repo actively
violates, so `to_converse` normalises rather than translating one-to-one. See
the comments on each pass — every one of them corresponds to a real transcript
this codebase produces.
"""
import json
import re
from dataclasses import dataclass

# Bedrock requires toolUseId to match ^[a-zA-Z0-9_-]+$, 1-64 chars. Azure's ids
# are already in that alphabet, but a replayed transcript can carry anything.
_ID_ALLOWED = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_id(raw: str) -> str:
    cleaned = _ID_ALLOWED.sub("_", str(raw or ""))[:64]
    return cleaned or "tool_0"


# ─── Response shim ────────────────────────────────────────────────────────────
# Field names and nesting are fixed by the callers; do not "tidy" them.


@dataclass(frozen=True)
class _Function:
    name: str
    arguments: str  # JSON string — callers json.loads() this


@dataclass(frozen=True)
class _ToolCall:
    id: str
    function: _Function
    type: str = "function"


@dataclass(frozen=True)
class _Message:
    content: str | None
    tool_calls: list[_ToolCall] | None  # None, not [], when absent


@dataclass(frozen=True)
class _Choice:
    message: _Message
    finish_reason: str | None = None


@dataclass(frozen=True)
class Response:
    choices: list[_Choice]
    usage: dict | None = None


# ─── Request translation ──────────────────────────────────────────────────────


def _blocks_for(msg: dict) -> list[dict]:
    """The Converse content blocks for one OpenAI-shaped message."""
    role = msg.get("role")
    text = str(msg.get("content") or "")

    if role == "tool":
        # A tool result is a *user* turn in Converse, not its own role.
        return [{
            "toolResult": {
                "toolUseId": _sanitize_id(msg.get("tool_call_id")),
                "content": [{"text": text or "(no output)"}],
                "status": "success",
            }
        }]

    blocks: list[dict] = []
    if text.strip():
        blocks.append({"text": text})

    for tc in msg.get("tool_calls") or []:
        get = tc.get if isinstance(tc, dict) else lambda k, _tc=tc: getattr(_tc, k, None)
        fn = get("function") or {}
        fn_get = fn.get if isinstance(fn, dict) else lambda k, _fn=fn: getattr(_fn, k, None)
        raw_args = fn_get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (TypeError, ValueError):
            # A malformed argument string would fail Converse validation with a
            # message that names the schema, not the transcript. Send an empty
            # object so the model sees its own bad call and can correct it.
            args = {}
        blocks.append({
            "toolUse": {
                "toolUseId": _sanitize_id(get("id")),
                "name": fn_get("name") or "unknown_tool",
                "input": args,
            }
        })

    return blocks


def to_converse(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """OpenAI-shaped messages -> (system_blocks, converse_messages)."""
    system: list[dict] = []
    turns: list[dict] = []

    for msg in messages or []:
        role = msg.get("role")
        if role == "system":
            # Converse takes system prompts as a separate top-level parameter.
            text = str(msg.get("content") or "")
            if text.strip():
                system.append({"text": text})
            continue

        blocks = _blocks_for(msg)
        if not blocks:
            # An assistant turn with blank content and no tool calls yields no
            # blocks. This repo writes exactly that (add_message(..., "", ...)),
            # and Converse rejects both an empty content list and {"text": ""}.
            continue

        turns.append({
            "role": "assistant" if role == "assistant" else "user",
            "content": blocks,
        })

    # Merge consecutive same-role turns. The load-bearing case: the agents emit
    # one `role: "tool"` message per tool call, but Converse requires ALL of an
    # assistant turn's toolResults in a SINGLE following user message.
    merged: list[dict] = []
    for turn in turns:
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"].extend(turn["content"])
        else:
            merged.append(turn)

    # Converse requires the first turn be `user`. Prepend a user turn rather
    # than popping the assistant turn, which would orphan any toolResult blocks
    # that follow it and trigger a ValidationException from Bedrock.
    if merged and merged[0]["role"] == "assistant":
        merged.insert(0, {"role": "user", "content": [{"text": "Begin."}]})

    # A trailing assistant turn is prefill, which current Claude models reject.
    # The nudge paths always append a user message, so this should never fire —
    # it is one comparison against an intermittent 400.
    if merged and merged[-1]["role"] == "assistant":
        merged.append({"role": "user", "content": [{"text": "Continue."}]})

    return system, merged


def tools_to_toolconfig(tools: list[dict] | None) -> dict | None:
    """OpenAI function-tool list -> Converse toolConfig."""
    if not tools:
        return None

    specs = []
    for tool in tools:
        fn = tool.get("function") or {}
        specs.append({
            "toolSpec": {
                "name": fn.get("name") or "unknown_tool",
                "description": fn.get("description") or "",
                # A tool taking no arguments still needs a valid object schema;
                # a bare {} is rejected.
                "inputSchema": {"json": fn.get("parameters") or {"type": "object", "properties": {}}},
            }
        })
    return {"tools": specs} if specs else None


# ─── Response translation ─────────────────────────────────────────────────────


def from_converse(raw: dict) -> Response:
    """Converse response dict -> the OpenAI-shaped shim above."""
    blocks = (((raw or {}).get("output") or {}).get("message") or {}).get("content") or []

    text = "".join(b["text"] for b in blocks if isinstance(b, dict) and "text" in b)
    calls = [
        _ToolCall(
            id=b["toolUse"].get("toolUseId") or "",
            function=_Function(
                name=b["toolUse"].get("name") or "",
                # Back to a JSON string: callers json.loads() this.
                arguments=json.dumps(b["toolUse"].get("input") or {}),
            ),
        )
        for b in blocks
        if isinstance(b, dict) and "toolUse" in b
    ]

    return Response(
        choices=[_Choice(
            message=_Message(content=text or None, tool_calls=calls or None),
            finish_reason=(raw or {}).get("stopReason"),
        )],
        usage=(raw or {}).get("usage"),
    )
