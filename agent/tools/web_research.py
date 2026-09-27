"""
The search_web tool: live, verified web research, gated behind explicit user
approval — sre_agent.py only ever offers this tool's schema for the one model
turn immediately following a request_web_research call the user replied to
(see SreAgent._step_locked's pending_web_research handling). This module has
no opinion on that gate; it just runs the search -> verify pipeline and
streams staged progress through the same SSE trace every other tool call
uses (utils/logger.add_run_log), so the user sees what stage the research is
at rather than a silent pause.
"""
import asyncio
import json

from utils import web_search_client as web
from utils.logger import add_run_log

MAX_TOOL_OUTPUT_CHARS = 20_000

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the public web for information not found in the tenant's knowledge base "
                "or AWS evidence. Only available after the user has explicitly approved a "
                "request_web_research call — never call this preemptively. Each result is "
                "authority-scored and mechanically checked against the fetched page; an "
                "unverified result should be treated with more caution in your answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to search for, described generically. Never include account ids, "
                            "ARNs, resource ids, internal hostnames, or incident ids — this is "
                            "enforced server-side too, but write it clean."
                        ),
                    },
                    "top_n": {"type": "integer", "description": "Max sources to return. Default 5."},
                },
                "required": ["query"],
            },
        },
    },
]


def _dump(payload: dict) -> str:
    """Same truncate-the-list-until-it-fits budget as
    aws_observability._dump / knowledge_base._dump."""
    text = json.dumps(payload, default=str, separators=(",", ":"))
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    results = payload.get("results") or []
    kept = list(results)
    while kept and len(text) > MAX_TOOL_OUTPUT_CHARS:
        kept = kept[:-1]
        text = json.dumps({**payload, "results": kept}, default=str, separators=(",", ":"))
    return text


async def dispatch(incident_id: str, name: str, args: dict) -> str:
    """Run the search_web tool call. The staged add_run_log lines below are
    UX narration checkpoints around two real steps (search, then verify),
    not five separately-timed phases — the user asked for visible progress
    text, not a literal five-stage pipeline."""
    if name != "search_web":
        return _dump({"error": "unknown_tool", "message": f"{name} is not an available tool."})

    query = (args.get("query") or "").strip()
    if not query:
        return _dump({"results": [], "message": "search_web needs a query."})
    top_n = int(args.get("top_n") or 5)

    add_run_log(incident_id, "INFO: Searching for relevant documentation…")
    results = await asyncio.to_thread(web.search, query, top_n)
    if not results:
        add_run_log(incident_id, "INFO: No web results found.")
        return _dump({"results": [], "message": "No web results found for that query."})

    add_run_log(incident_id, "INFO: Reviewing service/provider documentation…")
    add_run_log(incident_id, "INFO: Investigating possible causes…")
    verified = await asyncio.to_thread(lambda: [web.verify_result(r) for r in results])

    add_run_log(incident_id, "INFO: Verifying findings across sources…")
    add_run_log(incident_id, "INFO: Preparing the final answer…")
    return _dump({"results": verified})