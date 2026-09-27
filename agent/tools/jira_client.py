"""
JIRA REST API client for the provisioning agent.

Exposes plain async functions (same module-level shape as tools/aws_creds.py)
backing two LLM tools in agent.py — fetch_jira_tickets / get_jira_ticket —
plus the write-back calls (add_comment/transition_ticket) used by the
deterministic post-apply hook once a run's Terraform has been applied and
pushed to GitHub.

Credentials resolve per run: a run that named a JIRA connection in the UI
uses that connection (see tools/integration_creds.py); otherwise it falls
back to the JIRA_* environment variables. Both cases produce the same
JiraCreds shape — an API base URL plus a ready `Authorization` header value
— so basic-auth (API token) and bearer (OAuth) connections share one code
path here. OAuth connections are also cloudId-scoped
(https://api.atlassian.com/ex/jira/{cloudId}), which is why the base URL is
resolved rather than assumed to be the site URL.
"""
import base64
import logging
import os
from dataclasses import dataclass

import httpx

from tools import integration_creds

logger = logging.getLogger("uvicorn.error")


@dataclass
class JiraCreds:
    api_base_url: str
    auth_header: str
    default_project: str = ""
    review_transition_name: str = "In Review"
    site_url: str = ""


def _env_creds() -> JiraCreds | None:
    base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    email = os.getenv("JIRA_EMAIL", "")
    api_token = os.getenv("JIRA_API_TOKEN", "")
    if not (base_url and email and api_token):
        return None
    token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
    return JiraCreds(
        api_base_url=base_url,
        auth_header=f"Basic {token}",
        default_project=os.getenv("JIRA_DEFAULT_PROJECT", "").strip(),
        review_transition_name=os.getenv("JIRA_REVIEW_TRANSITION_NAME", "In Review").strip() or "In Review",
        site_url=base_url,
    )


def _resolve(run_id: str | None) -> JiraCreds | None:
    resolved = integration_creds.jira_creds_for(run_id)
    if resolved and resolved.get("apiBaseUrl") and resolved.get("authHeader"):
        return JiraCreds(
            api_base_url=str(resolved["apiBaseUrl"]).rstrip("/"),
            auth_header=str(resolved["authHeader"]),
            default_project=(resolved.get("defaultProject") or "").strip(),
            review_transition_name=(resolved.get("reviewTransitionName") or "In Review").strip() or "In Review",
            site_url=resolved.get("siteUrl") or resolved.get("apiBaseUrl") or "",
        )
    # The JIRA_* environment connection is the OPERATOR's, used only by the
    # background intake poller (run_id None). A run only ever uses its owner's
    # own connection, never the operator's.
    return _env_creds() if run_id is None else None


def is_configured(run_id: str | None = None) -> bool:
    return _resolve(run_id) is not None


def _require(run_id: str | None) -> JiraCreds:
    creds = _resolve(run_id)
    if not creds:
        raise RuntimeError(
            "No JIRA connection available. Connect a Jira workspace on the Integrations page, "
            "or set JIRA_BASE_URL/JIRA_EMAIL/JIRA_API_TOKEN."
        )
    return creds


def _headers(creds: JiraCreds) -> dict[str, str]:
    return {"Authorization": creds.auth_header, "Accept": "application/json"}


def _raise_for_jira(resp) -> None:
    """Surface JIRA's own error text instead of httpx's generic
    "Client error '400 Bad Request' for url ..." — the useful detail is in the
    body's errorMessages, and it's what ends up in the run log and the chat."""
    if resp.status_code < 400:
        return
    detail = ""
    try:
        body = resp.json()
        parts = body.get("errorMessages") or []
        if isinstance(body.get("errors"), dict):
            parts = parts + [f"{k}: {v}" for k, v in body["errors"].items()]
        detail = "; ".join(str(p) for p in parts)
    except Exception:
        detail = (resp.text or "")[:300]
    raise RuntimeError(f"JIRA returned {resp.status_code}" + (f": {detail}" if detail else ""))


def _adf_to_text(node) -> str:
    """Best-effort plain-text extraction from JIRA's Atlassian Document Format."""
    if not isinstance(node, dict):
        return ""
    parts = []
    if node.get("type") == "text":
        parts.append(node.get("text", ""))
    for child in node.get("content", []) or []:
        parts.append(_adf_to_text(child))
    joined = " ".join(p for p in parts if p)
    if node.get("type") in ("paragraph", "heading"):
        joined += "\n"
    return joined


def _text_to_adf(body: str) -> dict:
    """Plain text -> Atlassian Document Format, one paragraph per blank-line block.

    Everything that writes to JIRA goes through here. Putting a whole multi-line
    body in a single ADF text node — which is what add_comment used to do — makes
    JIRA render it as one run-on blob, so the close-out comment's bullet list and
    the SRE incident's alarm facts both came out unreadable.
    """
    paragraphs = [p.strip() for p in (body or "").split("\n\n")]
    paragraphs = [p for p in paragraphs if p] or [""]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]}
            for p in paragraphs
        ],
    }


def _summarize_issue(issue: dict) -> dict:
    fields = issue.get("fields", {}) or {}
    return {
        "key": issue.get("key"),
        "summary": fields.get("summary"),
        "status": (fields.get("status") or {}).get("name"),
        "priority": (fields.get("priority") or {}).get("name"),
        "issue_type": (fields.get("issuetype") or {}).get("name"),
        "labels": fields.get("labels") or [],
        "description": _adf_to_text(fields.get("description")).strip() if fields.get("description") else "",
    }


# How far back an unscoped "show me the latest tickets" looks. The search API
# rejects a bare "ORDER BY ..." as an unbounded query, so there must always be
# some restriction — a project when we know one, otherwise a time window.
DEFAULT_SEARCH_WINDOW = os.getenv("JIRA_DEFAULT_SEARCH_WINDOW", "-365d").strip() or "-365d"


async def search_tickets(run_id: str | None, jql: str | None = None, project: str | None = None) -> list[dict]:
    creds = _require(run_id)

    query = (jql or "").strip()
    if not query:
        proj = (project or creds.default_project or "").strip()
        query = (
            f'project = "{proj}" ORDER BY updated DESC'
            if proj
            else f"updated >= {DEFAULT_SEARCH_WINDOW} ORDER BY updated DESC"
        )

    async with httpx.AsyncClient(timeout=15) as client:
        # /rest/api/3/search was removed by Atlassian (410 Gone) in favour of
        # /rest/api/3/search/jql, which is token-paginated ({issues,
        # nextPageToken, isLast}) and returns only explicitly requested fields.
        resp = await client.get(
            f"{creds.api_base_url}/rest/api/3/search/jql",
            params={
                "jql": query,
                "maxResults": 20,
                "fields": "summary,status,priority,issuetype,labels,description",
            },
            headers=_headers(creds),
        )
        _raise_for_jira(resp)
        data = resp.json()

    return [_summarize_issue(issue) for issue in data.get("issues", [])]


async def get_ticket(run_id: str | None, key: str) -> dict:
    creds = _require(run_id)

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{creds.api_base_url}/rest/api/3/issue/{key}",
            params={"fields": "summary,status,priority,issuetype,labels,description"},
            headers=_headers(creds),
        )
        _raise_for_jira(resp)
        return _summarize_issue(resp.json())


async def add_comment(run_id: str | None, key: str, body: str) -> str | None:
    """Returns the new comment's id. Callers that also poll for replies should
    record it, so our own comment isn't mistaken for the user answering."""
    creds = _resolve(run_id)
    if not creds:
        logger.info("[jira_client] no JIRA connection — skipping add_comment for %s", key)
        return None
    payload = {"body": _text_to_adf(body)}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{creds.api_base_url}/rest/api/3/issue/{key}/comment",
            json=payload,
            headers=_headers(creds),
        )
        _raise_for_jira(resp)
        return str((resp.json() or {}).get("id") or "") or None


async def get_comments(run_id: str | None, key: str, after_id: str | None = None) -> list[dict]:
    """Comments on a ticket, oldest first, optionally only those newer than
    `after_id`. Comment ids are numeric strings that increase monotonically, so
    a plain int compare is enough to find replies we haven't processed."""
    creds = _require(run_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{creds.api_base_url}/rest/api/3/issue/{key}/comment",
            params={"orderBy": "created", "maxResults": 50},
            headers=_headers(creds),
        )
        _raise_for_jira(resp)
        comments = resp.json().get("comments", [])

    out = []
    for c in comments:
        cid = str(c.get("id") or "")
        if after_id and cid.isdigit() and after_id.isdigit() and int(cid) <= int(after_id):
            continue
        out.append({
            "id": cid,
            "author": ((c.get("author") or {}).get("displayName") or "someone"),
            "body": _adf_to_text(c.get("body")).strip(),
        })
    return out


async def transition_ticket(run_id: str | None, key: str, target_status_name: str | None = None) -> bool:
    """Best-effort: finds a transition whose target status name matches
    (case-insensitive) and applies it. Returns False (logged, not raised) if
    no matching transition exists — workflows vary per project/board."""
    creds = _resolve(run_id)
    if not creds:
        logger.info("[jira_client] no JIRA connection — skipping transition for %s", key)
        return False
    target = (target_status_name or creds.review_transition_name or "").strip()
    if not target:
        return False

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{creds.api_base_url}/rest/api/3/issue/{key}/transitions",
            headers=_headers(creds),
        )
        _raise_for_jira(resp)
        transitions = resp.json().get("transitions", [])

        match = next(
            (t for t in transitions if (t.get("name") or "").strip().lower() == target.lower()),
            None,
        )
        if not match:
            logger.warning(
                "[jira_client] no transition named %r on %s (available: %s)",
                target, key, [t.get("name") for t in transitions],
            )
            return False

        resp = await client.post(
            f"{creds.api_base_url}/rest/api/3/issue/{key}/transitions",
            json={"transition": {"id": match["id"]}},
            headers=_headers(creds),
        )
        _raise_for_jira(resp)
        return True
