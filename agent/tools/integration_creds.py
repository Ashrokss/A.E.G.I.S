"""
Per-run JIRA / GitHub connection credentials.

Structural twin of tools/aws_creds.py: a run optionally names a connection
(jira_connection_id / github_connection_id, chosen in the UI's Run settings).
If given, this fetches that connection's resolved credentials from the Node
backend's internal endpoints — which own the encrypted store and, for OAuth
connections, do any token refresh before answering. Creds are cached per
run_id and read back by jira_client.py / github_push.py. When no connection
is named (or the backend can't resolve one), those modules fall back to the
JIRA_*/GITHUB_* environment variables, exactly as before this feature.

The backend hands back a ready-to-use `apiBaseUrl` + `authHeader` pair for
JIRA rather than raw credentials, so this side never needs to know whether a
connection is basic-auth (API token) or bearer (OAuth) — that difference is
entirely a string the backend produced.
"""
import os
import logging

import httpx

logger = logging.getLogger("uvicorn.error")

BACKEND_URL = os.getenv("BACKEND_INTERNAL_URL", "http://aws-backend:3005").rstrip("/")

# run_id -> resolved creds blob from the backend
_run_jira: dict[str, dict] = {}
_run_github: dict[str, dict] = {}
# run_id -> connection id (remembered so approve-time can re-fetch fresh creds)
_run_jira_conn: dict[str, str] = {}
_run_github_conn: dict[str, str] = {}


async def _fetch(path: str, authorization: str | None, connection_id: str | None) -> dict | None:
    if not authorization or not connection_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{BACKEND_URL}{path}",
                params={"connectionId": connection_id},
                headers={"Authorization": authorization},
            )
            if resp.status_code == 200:
                return resp.json()
            logger.info("[integration_creds] backend returned %s for %s", resp.status_code, path)
    except Exception as ex:  # backend unreachable / not configured
        logger.info("[integration_creds] could not fetch creds from %s: %s", path, ex)
    return None


async def fetch_jira_creds(authorization: str | None, connection_id: str | None) -> dict | None:
    return await _fetch("/api/integrations/internal/jira-creds", authorization, connection_id)


async def fetch_github_creds(authorization: str | None, connection_id: str | None) -> dict | None:
    return await _fetch("/api/integrations/internal/github-creds", authorization, connection_id)


def set_run_integrations(
    run_id: str,
    *,
    jira: dict | None = None,
    github: dict | None = None,
    jira_connection_id: str | None = None,
    github_connection_id: str | None = None,
) -> None:
    if jira:
        _run_jira[run_id] = jira
    if github:
        _run_github[run_id] = github
    if jira_connection_id:
        _run_jira_conn[run_id] = jira_connection_id
    if github_connection_id:
        _run_github_conn[run_id] = github_connection_id


def jira_creds_for(run_id: str | None) -> dict | None:
    return _run_jira.get(run_id) if run_id else None


def github_creds_for(run_id: str | None) -> dict | None:
    return _run_github.get(run_id) if run_id else None


def clear_run_integrations(run_id: str) -> None:
    _run_jira.pop(run_id, None)
    _run_github.pop(run_id, None)
    _run_jira_conn.pop(run_id, None)
    _run_github_conn.pop(run_id, None)
