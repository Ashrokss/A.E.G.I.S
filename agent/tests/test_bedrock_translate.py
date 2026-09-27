"""Converse translation: the rules that break in production if they regress.

Pure — no AWS, no credentials, no network. Run directly:
    python tests/test_bedrock_translate.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.bedrock import (  # noqa: E402
    _Function, _Message, _ToolCall, from_converse, to_converse, tools_to_toolconfig,
)
from utils.model_client import repair_tool_call_history  # noqa: E402


def test_system_messages_hoist():
    """System prompts become a top-level param, not a message role."""
    system, msgs = to_converse([
        {"role": "system", "content": "first"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "second"},
    ])
    assert system == [{"text": "first"}, {"text": "second"}], system
    assert [m["role"] for m in msgs] == ["user"], msgs
    assert all(m["role"] != "system" for m in msgs)


def test_tool_results_collapse_into_one_user_turn():
    """THE rule. The agents emit one `tool` message per call; Converse requires
    every toolResult for an assistant turn in a SINGLE following user message."""
    _, msgs = to_converse([
        {"role": "user", "content": "check three things"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "function": {"name": "t1", "arguments": "{}"}},
            {"id": "b", "function": {"name": "t2", "arguments": "{}"}},
            {"id": "c", "function": {"name": "t3", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "a", "content": "ra"},
        {"role": "tool", "tool_call_id": "b", "content": "rb"},
        {"role": "tool", "tool_call_id": "c", "content": "rc"},
    ])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"], [m["role"] for m in msgs]
    results = msgs[2]["content"]
    assert len(results) == 3, results
    assert [b["toolResult"]["toolUseId"] for b in results] == ["a", "b", "c"]
    # The assistant turn carried blank content -> no empty text block survives.
    assert all("text" not in b for b in msgs[1]["content"]), msgs[1]["content"]
    assert len(msgs[1]["content"]) == 3


def test_no_empty_text_blocks_and_no_empty_messages():
    """Converse rejects both an empty content list and {"text": ""}."""
    _, msgs = to_converse([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": ""},          # dropped entirely
        {"role": "assistant", "content": "   "},       # whitespace -> dropped
        {"role": "user", "content": "still here"},
    ])
    for m in msgs:
        assert m["content"], m
        for block in m["content"]:
            if "text" in block:
                assert block["text"].strip(), block
    assert [m["role"] for m in msgs] == ["user"], msgs   # both users merged
    assert len(msgs[0]["content"]) == 2


def test_alternation_and_first_turn_is_user():
    """A replayed transcript can open with an assistant draft."""
    _, msgs = to_converse([
        {"role": "assistant", "content": "a draft from last time"},
        {"role": "user", "content": "go on"},
        {"role": "assistant", "content": "ok"},
        {"role": "assistant", "content": "and also"},
    ])
    assert msgs[0]["role"] == "user", msgs
    for prev, nxt in zip(msgs, msgs[1:]):
        assert prev["role"] != nxt["role"], msgs
    # A trailing assistant turn is prefill; current Claude models reject it.
    assert msgs[-1]["role"] == "user", msgs


def test_toolconfig_shape():
    params = {"type": "object", "properties": {"region": {"type": "string"}},
              "required": ["region"]}
    cfg = tools_to_toolconfig([
        {"type": "function", "function": {
            "name": "scan", "description": "d", "parameters": params}},
        {"type": "function", "function": {"name": "ping"}},  # no parameters
    ])
    assert cfg["tools"][0]["toolSpec"]["inputSchema"]["json"] == params
    assert cfg["tools"][0]["toolSpec"]["name"] == "scan"
    # A tool taking no arguments still needs a valid object schema, not {}.
    assert cfg["tools"][1]["toolSpec"]["inputSchema"]["json"] == {
        "type": "object", "properties": {}}
    assert tools_to_toolconfig(None) is None
    assert tools_to_toolconfig([]) is None


def test_from_converse_round_trip():
    """The shim must satisfy every attribute the call sites and stores read."""
    resp = from_converse({
        "output": {"message": {"content": [
            {"text": "thinking"},
            {"toolUse": {"toolUseId": "call_1", "name": "scan",
                         "input": {"region": "us-east-1"}}},
        ]}},
        "stopReason": "tool_use",
        "usage": {"inputTokens": 10, "outputTokens": 3},
    })
    msg = resp.choices[0].message
    assert msg.content == "thinking"
    tc = msg.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.type == "function"
    assert tc.function.name == "scan"
    # arguments must be a JSON *string* — callers json.loads() it.
    assert isinstance(tc.function.arguments, str)
    assert json.loads(tc.function.arguments) == {"region": "us-east-1"}
    # sre_store / kb_chat_store duck-type with hasattr.
    assert hasattr(tc, "id") and hasattr(tc, "function")
    assert hasattr(tc.function, "name") and hasattr(tc.function, "arguments")
    assert resp.choices[0].finish_reason == "tool_use"
    assert resp.usage["inputTokens"] == 10


def test_from_converse_without_tools_is_falsy():
    """agent.py:1931 tests tool_calls truthiness directly, not `is not None`."""
    resp = from_converse({"output": {"message": {"content": [{"text": "plain"}]}},
                          "stopReason": "end_turn"})
    assert not resp.choices[0].message.tool_calls
    assert resp.choices[0].message.content == "plain"


def test_repaired_dangling_call_survives_translation():
    """repair_tool_call_history runs first; its output must translate cleanly."""
    repaired = repair_tool_call_history([
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "user", "content": "actually, stop"},   # call never answered
    ])
    _, msgs = to_converse(repaired)
    used = {b["toolUse"]["toolUseId"] for m in msgs for b in m["content"] if "toolUse" in b}
    got = {b["toolResult"]["toolUseId"] for m in msgs for b in m["content"] if "toolResult" in b}
    assert used == got, (used, got)   # every toolUse answered — a Converse rule too
    assert msgs[0]["role"] == "user"


def test_malformed_arguments_do_not_raise():
    """A bad argument string must not blow up the whole turn."""
    _, msgs = to_converse([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "z", "function": {"name": "t", "arguments": "{not json"}}]},
        {"role": "tool", "tool_call_id": "z", "content": "r"},
    ])
    assert msgs[1]["content"][0]["toolUse"]["input"] == {}


def test_tool_use_ids_are_sanitized_consistently():
    """A sanitized toolUse id must still match its toolResult id, or Converse
    rejects the pair."""
    _, msgs = to_converse([
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call/with:bad chars", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call/with:bad chars", "content": "r"},
    ])
    use_id = msgs[1]["content"][0]["toolUse"]["toolUseId"]
    result_id = msgs[2]["content"][0]["toolResult"]["toolUseId"]
    assert use_id == result_id, (use_id, result_id)
    assert use_id == "call_with_bad_chars", use_id


def test_shim_classes_are_constructible_by_test_stubs():
    """Existing tests fake responses; they must be able to build the real shape."""
    m = _Message(content="x", tool_calls=[
        _ToolCall(id="i", function=_Function(name="n", arguments="{}"))])
    assert m.tool_calls[0].function.name == "n"


def test_opening_assistant_with_tool_calls_does_not_orphan_tool_results():
    """If a transcript starts with an assistant turn containing tool calls,
    to_converse must not drop it and orphan the subsequent toolResult blocks."""
    _, msgs = to_converse([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_xyz", "function": {"name": "get_metrics", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_xyz", "content": "{\"metric\": 42}"},
        {"role": "user", "content": "What caused this?"},
    ])
    assert msgs[0]["role"] == "user"
    assert "toolResult" not in msgs[0]["content"][0]
    # Tool call must still be present in the assistant message
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0]["toolUse"]["toolUseId"] == "call_xyz"
    # Tool result must match in the following user message
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0]["toolResult"]["toolUseId"] == "call_xyz"



if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} checks passed")
