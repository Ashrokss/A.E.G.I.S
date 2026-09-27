import os
import uuid
import logging
from datetime import datetime
from typing import List, Optional
import hashlib
import hmac
from contextvars import ContextVar
from urllib.parse import unquote
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Request, File, UploadFile, Form
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import shutil
from pydantic import BaseModel
from dotenv import load_dotenv
import sqlite3
import asyncio
import zipfile
import httpx
import jwt
from jwt import PyJWKClient
from sse_starlette.sse import EventSourceResponse
from agent import ProvisioningAgent
from kb_chat_agent import KbChatAgent
from utils.logger import log_queues
from tools.aws_creds import (
    require_aws_creds, set_run_aws,
    AwsAccountForbidden,
)
from tools import github_push, github_repo, jira_client, repo_profile
from tools import kb_report
from tools import knowledge_base as kb
from tools import kb_chat_store
from utils import doc_convert
from utils import tool_steps
from utils.workspace_paths import (
    EXCLUDED_DIR_NAMES,
    EXCLUDED_FILE_NAMES,
    EXCLUDED_FILE_SUFFIXES,
    TF_FILE_EXTS,
)
from tools.integration_creds import (
    fetch_jira_creds,
    fetch_github_creds,
    set_run_integrations,
)

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '../.env'))

from utils.validate_env import validate_aws_agent_env
validate_aws_agent_env()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

app = FastAPI(title="AEGIS Agent API")
agent = ProvisioningAgent()
# The Knowledge-Base chat lane: a multi-turn conversation over the knowledge
# base with no live AWS incident behind it — see kb_chat_agent.py.
kb_chat_agent = KbChatAgent()

@app.middleware("http")
async def _strip_public_prefix(request: Request, call_next):
    """The browser reaches this service at /agent-api/* (CloudFront → ALB path
    rule, or the Vite dev proxy). An ALB cannot rewrite paths, so map the public
    prefix onto this app's /api/* routes here."""
    path = request.scope["path"]
    if path == "/agent-api" or path.startswith("/agent-api/"):
        request.scope["path"] = "/api" + path[len("/agent-api"):]
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Database Setup
# Relative by default, which resolves against the process CWD (/app in the
# image) — fine for compose, but on a hosted platform that path is ephemeral and
# every run, message and incident is lost on restart. AGENT_DB_PATH points it at
# durable storage without changing local behaviour.
DB_PATH = os.getenv("AGENT_DB_PATH", "runs.db")

# sqlite3.connect() does NOT create missing parent directories, and init_db()
# runs at import time — so pointing AGENT_DB_PATH at a fresh location crashes
# the process on startup with "unable to open database file" unless the
# directory exists first.
_db_parent = os.path.dirname(DB_PATH)
if _db_parent:
    os.makedirs(_db_parent, exist_ok=True)

# Terraform SILENTLY ignores TF_PLUGIN_CACHE_DIR when the directory is absent,
# which quietly turns one shared ~700 MB provider download back into one per
# run. Create it here so the cache actually takes effect.
_tf_plugin_cache = os.getenv("TF_PLUGIN_CACHE_DIR", "").strip()
if _tf_plugin_cache:
    os.makedirs(_tf_plugin_cache, exist_ok=True)
AWS_COGNITO_ISSUER_URL = os.getenv("AWS_COGNITO_ISSUER_URL", "").strip()
AWS_COGNITO_CLIENT_ID = os.getenv("AWS_COGNITO_CLIENT_ID", "").strip()
AWS_COGNITO_JWKS_URL = os.getenv("AWS_COGNITO_JWKS_URL", "").strip()
JWKS_CLIENT = (
    PyJWKClient(AWS_COGNITO_JWKS_URL or f"{AWS_COGNITO_ISSUER_URL}/.well-known/jwks.json")
    if (AWS_COGNITO_JWKS_URL or AWS_COGNITO_ISSUER_URL) else None
)

# The Node backend owns the `users` table, so it owns the authorization answer.
# This service asks it — with the CALLER'S OWN token — rather than opening a
# second connection to PostgreSQL and reimplementing the same lookup. One
# implementation of "what may this identity do", in one place.
BACKEND_INTERNAL_URL = os.getenv("BACKEND_INTERNAL_URL", "http://aws-backend:3005").rstrip("/")

# There is ONE knowledge-base scope. Every kb_* table has a tenant_id column
# that is just an opaque scoping string, and with tenants gone there is exactly
# one value for it.
#
# The value stays "platform" deliberately: it is what the existing rows were
# written under, so keeping it means every RCA document, pattern and chat thread
# already uploaded remains reachable. Renaming it would orphan them for no gain —
# the string is a partition key, never a role or a permission.
_KB_SCOPE = "platform"

# The identity already resolved for THIS request, keyed by its Authorization
# header. Most endpoints authorize and then ask _caller_label for an audit name,
# which used to authorize a second time: two round trips to the backend (and
# PostgreSQL) per chat poll. Starlette runs every request in its own copy of
# the context, so this never outlives the request that set it — the role is
# still read from PostgreSQL on every request, as the product requires.
_request_identity: ContextVar[tuple[str, dict] | None] = ContextVar("_request_identity", default=None)


async def _fetch_identity(authorization: str | None) -> dict:
    """_authorize, at most once per request (see _request_identity)."""
    cached = _request_identity.get()
    if cached is not None and cached[0] == authorization:
        return cached[1]
    user = await _authorize(authorization)
    _request_identity.set((authorization, user))
    return user


async def _authorize(authorization: str | None) -> dict:
    """The caller's verified identity AND their PostgreSQL role.

    Two independent checks, in this order:

      1. THE TOKEN IS REAL — verified here against the Cognito pool's JWKS
         (RS256, issuer, token_use=access, client_id). Done locally so a forged
         or expired token is refused without a network hop, and so a
         misconfigured backend can never make an unsigned token acceptable.
      2. THE IDENTITY IS AUTHORIZED — asked of the Node backend's
         /api/auth/authorize, which resolves it from `users` in PostgreSQL
         (/api/auth/me minus its sign-in side effects: no Cognito round trip,
         no last_login write — this runs on every poll). Its 403 NOT_REGISTERED
         and 403 ACCOUNT_DISABLED are passed straight through, so this service
         gives exactly the same answers as the rest of the product.

    NOTHING is read from a token claim to authorize anything: not
    `cognito:groups`, not a `role` claim. Fails closed — an unreachable backend
    is a refusal, never a pass.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No token provided")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid token")

    if not AWS_COGNITO_CLIENT_ID or not JWKS_CLIENT:
        logger.error("[Auth] AWS_COGNITO_CLIENT_ID / JWKS URL missing — cannot verify tokens")
        raise HTTPException(status_code=500, detail="Authentication is not configured")

    try:
        signing_key = JWKS_CLIENT.get_signing_key_from_jwt(token).key
        # Cognito ACCESS tokens carry no `aud` claim — only ID tokens do — so
        # token_use and client_id are checked explicitly below instead of
        # passing audience= to jwt.decode (which would reject every valid one).
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=AWS_COGNITO_ISSUER_URL or None,
        )
    except Exception as ex:
        logger.warning(f"[Auth] JWT validation failed: {ex}")
        raise HTTPException(status_code=401, detail="Invalid token")

    if claims.get("token_use") != "access":
        raise HTTPException(
            status_code=401,
            detail=f"Expected an access token, got token_use={claims.get('token_use')}",
        )
    if claims.get("client_id") != AWS_COGNITO_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Token was not issued for this app client")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{BACKEND_INTERNAL_URL}/api/auth/authorize",
                headers={"Authorization": authorization},
            )
    except Exception as ex:
        logger.error(f"[Auth] could not reach the backend for authorization: {ex}")
        raise HTTPException(status_code=503, detail="Authorization is unavailable")

    if resp.status_code == 200:
        user = (resp.json() or {}).get("user") or {}
        if not user.get("role"):
            logger.error("[Auth] backend returned no role for an authenticated caller")
            raise HTTPException(status_code=503, detail="Authorization is unavailable")
        return user

    # Pass the backend's own refusal through — NOT_REGISTERED and
    # ACCOUNT_DISABLED must read identically wherever they are hit.
    body = {}
    try:
        body = resp.json() or {}
    except Exception:
        pass
    detail = body.get("error") or "Access denied"
    if resp.status_code in (401, 403):
        raise HTTPException(status_code=resp.status_code, detail=detail)
    logger.error(f"[Auth] backend authorization returned {resp.status_code}")
    raise HTTPException(status_code=503, detail="Authorization is unavailable")


async def _require_authenticated(authorization: str | None) -> dict:
    """A signed-in, registered, enabled AEGIS user — any role.

    This is the floor for the ordinary product: provisioning, SRE, the knowledge
    base. A `user` gets all of it; the role only matters for the admin-only
    surfaces below.
    """
    return await _fetch_identity(authorization)


async def _require_admin(authorization: str | None) -> dict:
    """role == 'admin', from PostgreSQL."""
    identity = await _fetch_identity(authorization)
    if identity.get("role") != "admin":
        raise HTTPException(status_code=403, detail="This operation requires an administrator.")
    return identity


def _run_row(run_id: str) -> dict | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


async def _require_run_owner(run_id: str, authorization: str | None) -> tuple[dict, dict]:
    """(identity, run row) — or 404. Someone else's run is indistinguishable
    from one that does not exist."""
    identity = await _require_authenticated(authorization)
    row = _run_row(run_id)
    if not row or not row.get("owner_user_id") or row["owner_user_id"] != identity.get("id"):
        raise HTTPException(status_code=404, detail="Run not found")
    return identity, row


async def _bind_run(
    run_id: str,
    authorization: str | None,
    aws_account_id: str | None,
    jira_connection_id: str | None,
    github_connection_id: str | None,
) -> None:
    """Resolve the run's credentials — the caller's OWN account and connections
    (the api enforces ownership on every lookup) — into the per-run caches.

    A refusal is an error, never a fallback: Terraform has no other identity to
    run as (tools/terraform.py strips the container's own)."""
    if aws_account_id:
        try:
            aws_creds = await require_aws_creds(authorization, aws_account_id)
        except AwsAccountForbidden as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not aws_creds:
            raise HTTPException(
                status_code=400,
                detail=f"PROVISIONING_ACCOUNT_UNKNOWN: AWS account {aws_account_id} is not onboarded, "
                       "or its role could not be assumed.",
            )
        set_run_aws(run_id, aws_creds, aws_account_id)
    set_run_integrations(
        run_id,
        jira=await fetch_jira_creds(authorization, jira_connection_id),
        github=await fetch_github_creds(authorization, github_connection_id),
        jira_connection_id=jira_connection_id,
        github_connection_id=github_connection_id,
    )


async def _rebind_run(run_id: str, authorization: str | None, row: dict) -> None:
    """Re-resolve credentials from what the run row persisted — STS creds expire
    (~1h) and in-memory caches do not survive a restart."""
    await _bind_run(
        run_id, authorization,
        row.get("aws_account_id"), row.get("jira_connection_id"), row.get("github_connection_id"),
    )


def _resolve_kb_scope_id(authorization: str | None = None) -> str:
    """The knowledge-base scope. One deployment, one scope — see _KB_SCOPE.

    Keeps its parameter so the call sites read the same as before; it is
    deliberately unused, because the scope no longer depends on who is asking.
    """
    return _KB_SCOPE


async def _require_admin_scope(authorization: str | None) -> str:
    """Admin-only knowledge-base scope: promoting web-sourced content into the
    knowledge base, and approving or rejecting a research candidate.

    Admin is required because "may use the knowledge base" and "may decide what
    becomes knowledge" are deliberately different permissions.
    """
    await _require_admin(authorization)
    return _KB_SCOPE


async def _caller_id(authorization: str | None) -> str:
    """users.id — the immutable owner key for anything a user owns (runs, chat
    threads). The email is for display and audit lines only: it can change."""
    identity = await _fetch_identity(authorization)
    return str(identity["id"])


async def _caller_label(authorization: str | None) -> str:
    """A human-readable name for an audit trail / "approved by" line.

    The email as PostgreSQL holds it — the same address the Admin Portal shows.
    A Cognito access token carries no email claim at all, so this cannot be read
    off the token: it comes from the identity, which is the record of who this
    is.
    """
    identity = await _fetch_identity(authorization)
    return identity.get("email") or identity.get("id") or "unknown"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            status TEXT,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            goal TEXT,
            last_message TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            role TEXT,
            content TEXT,
            type TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            created_at TIMESTAMP,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        )
    ''')
    # Every snapshot poll reads a run's or incident's rows twice (transcript and
    # tool steps); without this each read is a full scan of a table that keeps
    # every run ever made.
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_run_id ON messages(run_id, id)")
    # Simple migration: Add tool_calls and tool_call_id if they don't exist
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN tool_calls TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
    try:
        cursor.execute("ALTER TABLE messages ADD COLUMN step TEXT")  # locked-workflow phase chip
    except sqlite3.OperationalError:
        pass # Already exists
    try:
        cursor.execute("ALTER TABLE runs ADD COLUMN jira_ticket_key TEXT")  # set when a run executes a JIRA ticket
    except sqlite3.OperationalError:
        pass # Already exists
    # Persisted (not just cached in memory) so a restarted agent can still
    # re-resolve credentials at approve/delivery time, long after run creation.
    try:
        cursor.execute("ALTER TABLE runs ADD COLUMN jira_connection_id TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
    try:
        cursor.execute("ALTER TABLE runs ADD COLUMN github_connection_id TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
    try:
        cursor.execute("ALTER TABLE runs ADD COLUMN jira_last_comment_id TEXT")  # replies already consumed
    except sqlite3.OperationalError:
        pass # Already exists
    # Which workspace subdirectory holds this run's root module (empty/NULL = the
    # workspace root). Persisted because the in-memory value is lost if the agent
    # restarts while a run sits in AWAITING_APPROVAL, and the post-apply push
    # would then deliver code somewhere other than the plan-time message said.
    try:
        cursor.execute("ALTER TABLE runs ADD COLUMN github_target_subdir TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
    # Who owns the run (users.id from PostgreSQL) — every /api/runs/{id}* call
    # checks it. NULL only for Jira-intake runs nobody has claimed yet.
    try:
        cursor.execute("ALTER TABLE runs ADD COLUMN owner_user_id TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
    # The AWS account the run is bound to. Persisted so a restarted agent
    # re-resolves credentials for the SAME account (an in-memory-only binding
    # would, and a restart silently fell back to the container's identity).
    try:
        cursor.execute("ALTER TABLE runs ADD COLUMN aws_account_id TEXT")
    except sqlite3.OperationalError:
        pass # Already exists
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_owner ON runs(owner_user_id, updated_at DESC)")
    conn.commit()
    conn.close()
    # Cached repository conventions, keyed by repo+branch rather than by run or
    # connection: a connection is shared across many runs, two connections can
    # point at one repo, and a webhook-started run has no token of its own.
    repo_profile.ensure_table(DB_PATH)
    # Per-tenant RCA knowledge base backing search_knowledge_base.
    kb.ensure_table(DB_PATH)
    # Knowledge-Base chat threads — see kb_chat_agent.py.
    kb_chat_store.ensure_table(DB_PATH)

init_db()

# Models
class Message(BaseModel):
    role: str
    content: str
    type: str = "text"

class RunCreate(BaseModel):
    goal: str
    aws_account_id: Optional[str] = None
    jira_connection_id: Optional[str] = None
    jira_ticket_key: Optional[str] = None
    github_connection_id: Optional[str] = None

class RunResponse(BaseModel):
    id: str
    status: str
    goal: str
    created_at: str

class RunStatusResponse(BaseModel):
    id: str
    status: str
    goal: str
    created_at: str
    updated_at: str
    last_message: Optional[str] = None
    # Which ticket this run is executing, so the UI can stop offering to start
    # work it is already doing.
    jira_ticket_key: Optional[str] = None

class RunSnapshotResponse(BaseModel):
    run: RunStatusResponse
    messages: List[dict]
    # Tool calls behind the agent's turns (name + argument summary, never the
    # output) for the chat's reasoning timeline. See utils/tool_steps.py.
    steps: List[dict] = []

# Endpoints
# ─── JIRA auto-intake poller ──────────────────────────────────────────────
# Off unless JIRA_POLL_SECONDS is set. JIRA Cloud can't reach a laptop, so a
# webhook is out; polling is the only option that works locally.
# Picks up tickets carrying JIRA_AUTO_LABEL and starts a run for each. It only
# reaches the plan -> approval gate: nothing is applied to AWS without a human
# clicking Approve, same as a hand-started run.
JIRA_POLL_SECONDS = int(os.getenv("JIRA_POLL_SECONDS", "0"))
JIRA_AUTO_LABEL = os.getenv("JIRA_AUTO_LABEL", "aegis-auto").strip()


def _start_run_for_ticket(ticket: dict) -> str | None:
    """Queue a run from a ticket, unless one already exists for it. Dedupe is
    the runs.jira_ticket_key column — no extra state to keep in sync.

    The run is parked at PENDING_INTAKE and the agent is NOT stepped: there is
    no user session behind a webhook, so no AWS account is attached yet and
    Terraform would fail with "No valid credential sources found". It shows up
    on the Provisioning tab's incoming-tickets list, and POST
    /api/runs/{id}/attach binds the operator's account and starts it."""
    key = (ticket.get("key") or "").strip().upper()
    if not key:
        return None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM runs WHERE jira_ticket_key = ? LIMIT 1", (key,))
    if cursor.fetchone():
        conn.close()
        return None

    goal = f"Execute {key}: {ticket.get('summary') or ''}".strip()
    if ticket.get("description"):
        goal += f"\n\n{ticket['description']}"
    run_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    cursor.execute(
        "INSERT INTO runs (id, status, created_at, updated_at, goal, jira_ticket_key) VALUES (?, ?, ?, ?, ?, ?)",
        (run_id, "PENDING_INTAKE", now, now, goal, key),
    )
    cursor.execute(
        "INSERT INTO messages (run_id, role, content, type, created_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, "user", goal, "text", now),
    )
    conn.commit()
    conn.close()
    return run_id


async def _pull_jira_replies():
    """Feed new ticket comments into runs that are waiting on an answer, so a
    requester can reply in JIRA instead of in the app."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, jira_ticket_key, jira_last_comment_id FROM runs "
        "WHERE jira_ticket_key IS NOT NULL AND status = 'AWAITING_INPUT'"
    ).fetchall()
    conn.close()

    for row in rows:
        try:
            new = await jira_client.get_comments(row["id"], row["jira_ticket_key"], row["jira_last_comment_id"])
        except Exception as e:
            logger.warning(f"[jira-poll] comments for {row['jira_ticket_key']} failed: {e}")
            continue
        if not new:
            continue
        text = "\n".join(f"{c['author']}: {c['body']}" for c in new if c["body"])
        if not text.strip():
            continue
        agent._set_run_field(row["id"], "jira_last_comment_id", new[-1]["id"])
        agent._add_message(row["id"], "user", f"(reply on {row['jira_ticket_key']})\n{text}", "text")
        logger.info(f"[jira-poll] {row['jira_ticket_key']} replied -> resuming run {row['id']}")
        asyncio.create_task(agent.step(row["id"]))


async def _poll_jira_tickets():
    # No user token out here, so credentials come from the JIRA_*/GITHUB_* env
    # fallback rather than a UI-selected connection.
    while True:
        await asyncio.sleep(JIRA_POLL_SECONDS)
        # No label configured = every recent ticket is fair game.
        scope = f'labels = "{JIRA_AUTO_LABEL}" AND ' if JIRA_AUTO_LABEL else ""
        try:
            tickets = await jira_client.search_tickets(
                None, jql=f"{scope}created >= -7d ORDER BY created DESC"
            )
        except Exception as e:
            logger.warning(f"[jira-poll] search failed: {e}")
            tickets = []
        try:
            await _pull_jira_replies()
        except Exception as e:
            logger.warning(f"[jira-poll] reply sweep failed: {e}")
        for t in tickets:
            try:
                run_id = _start_run_for_ticket(t)
            except Exception as e:
                logger.warning(f"[jira-poll] could not start run for {t.get('key')}: {e}")
                continue
            if run_id:
                logger.info(f"[jira-poll] {t.get('key')} queued as run {run_id}")


@app.on_event("startup")
async def _start_jira_poller():
    if JIRA_POLL_SECONDS > 0:
        logger.info(f"[jira-poll] every {JIRA_POLL_SECONDS}s for label '{JIRA_AUTO_LABEL}'")
        asyncio.create_task(_poll_jira_tickets())


# ─── JIRA webhook ─────────────────────────────────────────────────────────
# Same two jobs as the poller (new labelled ticket -> run, new comment ->
# resume a waiting run), but pushed by JIRA instead of pulled. Reaches this
# process at {public}/agent-api/jira/webhook via the existing nginx
# /agent-api/ -> agent /api/ proxy, so it needs no nginx change — only a
# public URL (ngrok/Cloudflare Tunnel locally).
#
# The endpoint is unauthenticated by nature: JIRA carries no user token. The
# guard is the HMAC-SHA256 signature JIRA computes over the raw body with the
# secret configured on the webhook. No secret set = endpoint disabled, rather
# than open.
JIRA_WEBHOOK_SECRET = os.getenv("JIRA_WEBHOOK_SECRET", "").strip()


@app.post("/api/jira/webhook")
async def jira_webhook(request: Request):
    if not JIRA_WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="JIRA webhook not enabled")

    raw = await request.body()
    sent = (request.headers.get("x-hub-signature") or "").split("=", 1)[-1]
    expected = hmac.new(JIRA_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sent.lower(), expected):
        logger.warning("[jira-hook] rejected: bad signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = await request.json()
    event = payload.get("webhookEvent") or ""
    issue = payload.get("issue") or {}
    key = (issue.get("key") or "").strip().upper()
    if not key:
        return {"ok": True, "ignored": event}

    # A comment can land on a ticket whose run is waiting for an answer. Reuse
    # the poller's sweep so the last-seen-comment dedupe stays in one place.
    if event.startswith("comment_"):
        await _pull_jira_replies()
        return {"ok": True, "event": event, "key": key}

    if event in ("jira:issue_created", "jira:issue_updated"):
        ticket = jira_client._summarize_issue(issue)
        if JIRA_AUTO_LABEL and JIRA_AUTO_LABEL not in (ticket.get("labels") or []):
            return {"ok": True, "skipped": f"missing label {JIRA_AUTO_LABEL}"}
        run_id = _start_run_for_ticket(ticket)
        if run_id:
            logger.info(f"[jira-hook] {key} queued as run {run_id}")
            return {"ok": True, "run_id": run_id}
        return {"ok": True, "skipped": "run already exists"}

    return {"ok": True, "ignored": event}


@app.get("/health")
def health_check():
    return {"status": "healthy"}

@app.post("/api/runs", response_model=RunResponse)
async def create_run(run_req: RunCreate, background_tasks: BackgroundTasks, authorization: str | None = Header(default=None)):
    """Start a run bound to one of the caller's AWS accounts. The account is
    REQUIRED: AEGIS never provisions as its own identity."""
    identity = await _require_authenticated(authorization)
    if not (run_req.aws_account_id or "").strip():
        raise HTTPException(status_code=400, detail="PROVISIONING_ACCOUNT_REQUIRED: choose an AWS account for this run.")
    run_id = str(uuid.uuid4())
    await _bind_run(
        run_id, authorization,
        run_req.aws_account_id, run_req.jira_connection_id, run_req.github_connection_id,
    )
    now = datetime.utcnow().isoformat()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO runs (id, status, created_at, updated_at, goal, jira_ticket_key, jira_connection_id, "
        "github_connection_id, owner_user_id, aws_account_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, "IDLE", now, now, run_req.goal,
            (run_req.jira_ticket_key or "").strip().upper() or None,
            run_req.jira_connection_id,
            run_req.github_connection_id,
            identity["id"],
            run_req.aws_account_id,
        )
    )
    cursor.execute(
        "INSERT INTO messages (run_id, role, content, type, created_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, "user", run_req.goal, "text", now)
    )
    conn.commit()
    conn.close()

    background_tasks.add_task(agent.step, run_id)

    return {
        "id": run_id,
        "status": "IDLE",
        "goal": run_req.goal,
        "created_at": now
    }


@app.get("/api/runs")
async def list_runs(authorization: str | None = Header(default=None)):
    """The caller's own runs, newest activity first — the run rail in the UI."""
    identity = await _require_authenticated(authorization)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, status, goal, created_at, updated_at, jira_ticket_key, aws_account_id FROM runs "
        "WHERE owner_user_id = ? ORDER BY updated_at DESC LIMIT 100",
        (identity["id"],),
    ).fetchall()
    conn.close()
    return {"runs": [
        {**dict(r), "title": (r["goal"] or "").split("\n", 1)[0][:140]}
        for r in rows
    ]}

class AttachRequest(BaseModel):
    aws_account_id: Optional[str] = None
    jira_connection_id: Optional[str] = None
    github_connection_id: Optional[str] = None


# Ticket runs that are under way. Several can be in flight at once — each run is
# independent server-side — so the UI lists them and lets the operator switch.
_ACTIVE_STATUSES = ("IDLE", "PLANNING", "REPAIRING", "AWAITING_INPUT", "AWAITING_APPROVAL", "APPLYING")


@app.get("/api/runs/inbox")
async def list_inbox(authorization: str | None = Header(default=None)):
    """Tickets waiting to be picked up, plus ticket runs already in progress.
    Polled by the Provisioning tab. `state` distinguishes them."""
    identity = await _require_authenticated(authorization)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in _ACTIVE_STATUSES)
    # Unclaimed intake runs are visible to every user (whoever attaches one owns
    # it); in-progress ticket runs only to their owner.
    rows = conn.execute(
        "SELECT id, jira_ticket_key, goal, created_at, status FROM runs "
        "WHERE (status = 'PENDING_INTAKE' AND owner_user_id IS NULL) "
        f"OR (owner_user_id = ? AND jira_ticket_key IS NOT NULL AND status IN ({placeholders})) "
        "ORDER BY created_at DESC LIMIT 30",
        (identity["id"], *_ACTIVE_STATUSES),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "ticket_key": r["jira_ticket_key"],
            # goal is "Execute KEY: summary\n\ndescription" — the first line is
            # all the card needs.
            "summary": (r["goal"] or "").split("\n", 1)[0],
            "created_at": r["created_at"],
            "status": r["status"],
            "state": "pending" if r["status"] == "PENDING_INTAKE" else "active",
        }
        for r in rows
    ]


@app.post("/api/runs/{run_id}/attach")
async def attach_run(
    run_id: str,
    req: AttachRequest,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    """Claim a ticket-created run: bind the caller's AWS account and
    integrations, make them its owner, then start it. The claim is one
    conditional UPDATE, so two users attaching at once cannot both win."""
    identity = await _require_authenticated(authorization)
    if not (req.aws_account_id or "").strip():
        raise HTTPException(status_code=400, detail="PROVISIONING_ACCOUNT_REQUIRED: choose an AWS account for this run.")
    row = _run_row(run_id)
    if not row or row.get("owner_user_id"):
        raise HTTPException(status_code=404, detail="Run not found")
    if row["status"] != "PENDING_INTAKE":
        raise HTTPException(status_code=409, detail=f"Run already started (status {row['status']})")

    await _bind_run(run_id, authorization, req.aws_account_id, req.jira_connection_id, req.github_connection_id)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "UPDATE runs SET status = 'IDLE', updated_at = ?, owner_user_id = ?, aws_account_id = ?, "
        "jira_connection_id = ?, github_connection_id = ? "
        "WHERE id = ? AND owner_user_id IS NULL AND status = 'PENDING_INTAKE'",
        (datetime.utcnow().isoformat(), identity["id"], req.aws_account_id,
         req.jira_connection_id, req.github_connection_id, run_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount != 1:
        raise HTTPException(status_code=409, detail="Run was claimed by someone else")

    background_tasks.add_task(agent.step, run_id)
    return {"id": run_id, "status": "IDLE"}


@app.get("/api/runs/{run_id}/messages")
async def get_messages(run_id: str, authorization: str | None = Header(default=None)):
    await _require_run_owner(run_id, authorization)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT role, content, type, created_at FROM messages WHERE run_id = ? ORDER BY id ASC", (run_id,))
    rows = cursor.fetchall()
    conn.close()

    return [{"role": r[0], "content": r[1], "type": r[2], "created_at": r[3]} for r in rows]

@app.get("/api/runs/{run_id}", response_model=RunStatusResponse)
async def get_run_status(run_id: str, authorization: str | None = Header(default=None)):
    await _require_run_owner(run_id, authorization)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, status, goal, created_at, updated_at, last_message, jira_ticket_key FROM runs WHERE id = ?",
        (run_id,)
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        raise HTTPException(status_code=404, detail="Run not found")

    return {
        "id": row[0],
        "status": row[1],
        "goal": row[2],
        "created_at": row[3],
        "updated_at": row[4],
        "last_message": row[5],
        "jira_ticket_key": row[6],
    }

@app.get("/api/runs/{run_id}/snapshot", response_model=RunSnapshotResponse)
async def get_run_snapshot(run_id: str, authorization: str | None = Header(default=None)):
    await _require_run_owner(run_id, authorization)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, status, goal, created_at, updated_at, last_message, jira_ticket_key FROM runs WHERE id = ?",
        (run_id,)
    )
    run_row = cursor.fetchone()
    if not run_row:
        conn.close()
        raise HTTPException(status_code=404, detail="Run not found")

    # "error" is deliberately NOT excluded: a credential failure must be
    # visible, not a silent FAILED chip.
    cursor.execute(
        "SELECT role, content, type, created_at, step FROM messages WHERE run_id = ? AND type NOT IN "
        "('internal_log', 'tool_output', 'tool', 'discard', 'assistant') AND role != 'system' ORDER BY id ASC",
        (run_id,)
    )
    msg_rows = cursor.fetchall()
    conn.close()

    return {
        "run": {
            "id": run_row[0],
            "status": run_row[1],
            "goal": run_row[2],
            "created_at": run_row[3],
            "updated_at": run_row[4],
            "last_message": run_row[5],
            "jira_ticket_key": run_row[6],
        },
        "messages": [
            {"role": r[0], "content": r[1], "type": r[2], "created_at": r[3], "step": r[4]}
            for r in msg_rows
        ],
        "steps": tool_steps.load_steps(DB_PATH, "messages", run_id),
    }

@app.post("/api/runs/{run_id}/messages")
async def send_message(run_id: str, msg: Message, background_tasks: BackgroundTasks, authorization: str | None = Header(default=None)):
    _, row = await _require_run_owner(run_id, authorization)
    if msg.role != "user":
        raise HTTPException(status_code=400, detail="Only user messages can be posted.")
    # A follow-up can trigger a plan or a Jira call: re-resolve credentials from
    # the persisted bindings (fresh STS creds; survives an agent restart).
    await _rebind_run(run_id, authorization, row)
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO messages (run_id, role, content, type, created_at) VALUES (?, ?, ?, ?, ?)",
        (run_id, msg.role, msg.content, msg.type, now)
    )
    conn.commit()
    conn.close()

    # Resume Agent Loop
    background_tasks.add_task(agent.step, run_id)

    return {"status": "received"}

@app.post("/api/runs/{run_id}/approve")
async def approve_run(run_id: str, background_tasks: BackgroundTasks, authorization: str | None = Header(default=None)):
    _, row = await _require_run_owner(run_id, authorization)
    # STS creds expire (~1h) — this is the moment closest to when apply will
    # actually run, so re-resolve here from the run's persisted account.
    await _rebind_run(run_id, authorization, row)
    agent._add_message(run_id, "user", "Approved. Proceed with apply.", "approval")
    background_tasks.add_task(agent.step, run_id)
    return {"status": "approved"}

# Statuses in which the agent is mid-work on a run; a drift check or fix waits.
_BUSY_STATUSES = {"IDLE", "PLANNING", "APPLYING", "REPAIRING", "CHECKING_DRIFT"}


async def _require_idle_run(run_id: str, authorization: str | None) -> dict:
    _, row = await _require_run_owner(run_id, authorization)
    lock = agent.run_locks.get(run_id)
    if row.get("status") in _BUSY_STATUSES or (lock and lock.locked()):
        raise HTTPException(status_code=409, detail="RUN_BUSY: AEGIS is still working on this run. Try again when it finishes.")
    # Terraform talks to the account (and its state bucket): fresh STS creds.
    await _rebind_run(run_id, authorization, row)
    return row


@app.post("/api/runs/{run_id}/drift-check")
async def check_run_drift(run_id: str, background_tasks: BackgroundTasks, authorization: str | None = Header(default=None)):
    """Compare the account with this run's state (read-only). The result
    arrives as a `drift_report` message."""
    await _require_idle_run(run_id, authorization)
    background_tasks.add_task(agent.check_drift, run_id)
    return {"status": "checking"}


@app.post("/api/runs/{run_id}/drift-fix")
async def fix_run_drift(run_id: str, background_tasks: BackgroundTasks, authorization: str | None = Header(default=None)):
    """Plan the run's own configuration, which puts drifted resources back.
    Nothing changes until the plan is approved."""
    await _require_idle_run(run_id, authorization)
    background_tasks.add_task(agent.plan_drift_fix, run_id)
    return {"status": "planning"}


@app.post("/api/runs/{run_id}/discard")
async def discard_run(run_id: str, background_tasks: BackgroundTasks, authorization: str | None = Header(default=None)):
    await _require_run_owner(run_id, authorization)
    agent._add_message(run_id, "user", "The user has decided to discard this plan. Please acknowledge this and ask the user what adjustments or changes they would like to make.", "discard")
    agent._update_status(run_id, "AWAITING_INPUT")
    background_tasks.add_task(agent.step, run_id)
    return {"status": "discarded"}

class PushRequest(BaseModel):
    # Lets the UI push with a repo the run itself never picked — e.g. a run the
    # JIRA poller started, which has no UI-selected connection.
    github_connection_id: Optional[str] = None


@app.post("/api/runs/{run_id}/push-to-github")
async def push_run_to_github(run_id: str, req: PushRequest | None = None, authorization: str | None = Header(default=None)):
    """Manually push this run's Terraform to the connected repo. Unlike the
    automatic post-apply delivery, this works at any point once files exist —
    so a plan that was never approved, or a run whose apply failed, can still
    be pushed for someone to look at."""
    _, row = await _require_run_owner(run_id, authorization)
    await _rebind_run(run_id, authorization, row)
    run = agent._get_run(run_id)

    chosen = (req.github_connection_id if req else None)
    if chosen:
        creds_blob = await fetch_github_creds(authorization, chosen)
        if creds_blob:
            set_run_integrations(run_id, github=creds_blob, github_connection_id=chosen)
            agent._set_run_field(run_id, "github_connection_id", chosen)
    creds = github_push.resolve_github_creds(run_id)
    if not creds:
        raise HTTPException(
            status_code=400,
            detail="No GitHub repository is connected. Add one on the Integrations page, then try again.",
        )

    goal = run.get("goal") or "Infrastructure provisioning"
    ticket = run.get("jira_ticket_key")
    applied = (run.get("status") or "").upper() == "COMPLETED"

    try:
        result = await asyncio.to_thread(
            github_push.push_workspace_to_github, run_id, goal, ticket, creds, applied,
            agent._vendored_dirs.get(run_id) or []
        )
    except github_push.GitHubPushError as e:
        raise HTTPException(status_code=400, detail=str(e))

    url = result.get("url", "")
    verb = "updated" if result.get("updated") else "opened"
    agent._add_message(
        run_id,
        "agent",
        f"Terraform pushed to `{creds.repo_full_name}` on branch `{result.get('branch')}` — pull request {verb}: {url}"
        + ("" if applied else "\n- **Note**: this configuration has not been applied yet."),
        "text",
    )
    # Tell the ticket too — the requester lives in JIRA, not in this app. Only on
    # first PR creation (or the applied delivery), so repeated pushes to the same
    # PR don't spam the ticket with near-identical comments.
    if ticket and (result.get("created") or applied):
        await agent.close_out_jira(run_id, ticket, pr_url=url, applied=applied)

    logger.info(f"[{run_id}] Manual GitHub push -> {result.get('branch')} ({verb})")
    return {"ok": True, **result, "repo": creds.repo_full_name, "applied": applied}

@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str, authorization: str | None = Header(default=None)):
    await _require_run_owner(run_id, authorization)
    await agent.stop(run_id)
    return {"status": "stopping"}

@app.get("/api/runs/{run_id}/stream")
async def stream_logs(run_id: str, authorization: str | None = Header(default=None)):
    # Authenticated: the AEGIS UI reads this with fetch() + an Authorization
    # header rather than EventSource (which cannot send one).
    await _require_run_owner(run_id, authorization)

    async def log_generator():
        queue = log_queues[run_id]
        while True:
            log = await queue.get()
            if log == "DONE":
                break
            yield {"data": log}

    return EventSourceResponse(log_generator())


class CandidateReject(BaseModel):
    reason: str


# ─── Knowledge base (RCA docs) ─────────────────────────────────────────────
# Documents backing the SRE agent's search_knowledge_base tool (sre_agent.py),
# scoped by _resolve_kb_scope_id: a tenant's own knowledge base when signed in
# via a tenant session, else the shared platform scope for a platform Cognito
# admin. PDF/Word/Excel/CSV are converted to markdown on upload (utils/
# doc_convert.py) before storage — chunk_text and _extract_title in
# tools/knowledge_base.py both assume markdown.

MAX_KB_UPLOAD_BYTES = 10 * 1024 * 1024  # 10MB — covers PDFs/workbooks, not just plain text
_KB_ALLOWED_EXTS = doc_convert.SUPPORTED_EXTENSIONS


@app.post("/api/knowledge-base/documents")
async def upload_kb_document(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    """Upload one RCA/postmortem doc into the caller's knowledge-base scope
    (their tenant, or the shared platform scope). Processing (chunk + embed)
    runs as a background task; poll GET /api/knowledge-base/documents for the
    status flip to "ready"."""
    await _require_authenticated(authorization)
    tenant_id = _resolve_kb_scope_id(authorization)

    filename = file.filename or "upload.md"
    if not filename.lower().endswith(_KB_ALLOWED_EXTS):
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type. Supported: {', '.join(_KB_ALLOWED_EXTS)}.",
        )

    raw = await file.read()
    if len(raw) > MAX_KB_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File is too large (10MB limit).")
    try:
        # Off the event loop: the scanned-PDF path makes a synchronous LLM
        # OCR call that can take real time, and would otherwise stall every
        # other request this process is serving for the duration.
        text = await asyncio.to_thread(doc_convert.convert_to_markdown, filename, raw)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    if not text.strip():
        raise HTTPException(status_code=400, detail="File is empty.")

    # Content is now markdown regardless of source format -- the stored
    # filename follows, so the RCA docs list doesn't show a ".pdf" next to
    # rendered markdown. Original .md/.txt uploads keep their extension.
    ext = filename.lower().rsplit(".", 1)[-1]
    stored_filename = filename if ext in ("md", "txt") else f"{filename.rsplit('.', 1)[0]}.md"

    doc_id = kb.create_document(DB_PATH, tenant_id, stored_filename, text, uploaded_by=await _caller_label(authorization))
    background_tasks.add_task(kb.ingest_document, DB_PATH, doc_id)
    return {"id": doc_id, "status": "processing"}


@app.get("/api/knowledge-base/documents")
async def list_kb_documents(authorization: str | None = Header(default=None)):
    await _require_authenticated(authorization)
    tenant_id = _resolve_kb_scope_id(authorization)
    docs = kb.list_documents(DB_PATH, tenant_id)
    return {
        "documents": [
            {
                "id": d["id"],
                "filename": d["filename"],
                "title": d["title"],
                "status": d["status"],
                "error": d.get("error"),
                "uploaded_at": d["uploaded_at"],
                "chunk_count": d["chunk_count"],
            }
            for d in docs
        ]
    }


@app.get("/api/knowledge-base/documents/{doc_id}")
async def get_kb_document(doc_id: str, authorization: str | None = Header(default=None)):
    """Full content for the "RCA docs" tab's openable document view — list_kb_documents
    above deliberately omits raw_text (it's not needed for the list, and it can be
    sizeable). get_document isn't tenant-scoped in its SQL, so the tenant check
    happens here, the same way delete_kb_document already guards it."""
    await _require_authenticated(authorization)
    tenant_id = _resolve_kb_scope_id(authorization)
    doc = kb.get_document(DB_PATH, doc_id)
    if not doc or doc.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return {
        "id": doc["id"],
        "filename": doc["filename"],
        "title": doc["title"],
        "status": doc["status"],
        "error": doc.get("error"),
        "uploaded_at": doc["uploaded_at"],
        "uploaded_by": doc.get("uploaded_by"),
        "content": doc["raw_text"],
    }


@app.delete("/api/knowledge-base/documents/{doc_id}")
async def delete_kb_document(doc_id: str, authorization: str | None = Header(default=None)):
    await _require_authenticated(authorization)
    tenant_id = _resolve_kb_scope_id(authorization)
    if not kb.delete_document(DB_PATH, tenant_id, doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"status": "deleted"}


# ─── Knowledge base: web-research approval queue ────────────────────────────
# Every web-research-augmented answer search_web produces is saved as a
# pending_approval candidate (sre_agent.py::_maybe_capture_research_candidate)
# before it can ever become retrievable knowledge. Approving/rejecting is an
# org-level action, gated to a tenant admin (their own tenant's
# queue) or any platform Cognito admin (the shared platform queue) — see
# _require_kb_admin_scope.

@app.get("/api/knowledge-base/candidates")
async def list_kb_candidates(
    status: str | None = "pending_approval", authorization: str | None = Header(default=None)
):
    tenant_id = await _require_admin_scope(authorization)
    return {"candidates": kb.list_research_candidates(DB_PATH, tenant_id, status=status)}


@app.post("/api/knowledge-base/candidates/{candidate_id}/approve")
async def approve_kb_candidate(candidate_id: str, authorization: str | None = Header(default=None)):
    tenant_id = await _require_admin_scope(authorization)
    reviewer = await _caller_label(authorization)
    doc_id = await kb.approve_research_candidate(DB_PATH, tenant_id, candidate_id, reviewer)
    if not doc_id:
        raise HTTPException(status_code=404, detail="Candidate not found or already reviewed")
    return {"status": "approved", "document_id": doc_id}


@app.post("/api/knowledge-base/candidates/{candidate_id}/reject")
async def reject_kb_candidate(
    candidate_id: str, body: CandidateReject, authorization: str | None = Header(default=None)
):
    tenant_id = await _require_admin_scope(authorization)
    reviewer = await _caller_label(authorization)
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="A rejection needs a reason.")
    if not kb.reject_research_candidate(DB_PATH, tenant_id, candidate_id, reviewer, reason):
        raise HTTPException(status_code=404, detail="Candidate not found or already reviewed")
    return {"status": "rejected"}


# ─── Knowledge base: knowledge gaps ──────────────────────────────────────────
# A gap is recorded automatically the moment search_knowledge_base scores a
# question too low to answer confidently (knowledge_base.record_gap) — this
# is a DIFFERENT queue from the candidates above, which only ever holds
# already-answered chat turns. A gap has no answer yet at all; "Perform
# Research" is what generates one (as a pending_approval document, reviewed
# in the RCA docs tab below), and "reject" just dismisses the question
# without researching it. Same admin gating as the candidates queue.

@app.get("/api/knowledge-base/gaps")
async def list_kb_gaps(authorization: str | None = Header(default=None)):
    tenant_id = await _require_admin_scope(authorization)
    return {"gaps": kb.list_gaps(DB_PATH, tenant_id, status="open")}


@app.post("/api/knowledge-base/gaps/{gap_id}/research")
async def research_kb_gap(
    gap_id: str, background_tasks: BackgroundTasks, authorization: str | None = Header(default=None)
):
    tenant_id = await _require_admin_scope(authorization)
    reviewer = await _caller_label(authorization)
    gap = kb.get_gap(DB_PATH, tenant_id, gap_id)
    if not gap or gap["status"] != "open":
        raise HTTPException(status_code=404, detail="Gap not found or already being researched")
    kb.set_gap_status(DB_PATH, gap_id, "researching")
    background_tasks.add_task(
        kb_report.run_gap_research, DB_PATH, tenant_id, gap_id, gap["question"], reviewer
    )
    return {"status": "researching"}


@app.post("/api/knowledge-base/gaps/{gap_id}/reject")
async def reject_kb_gap(gap_id: str, authorization: str | None = Header(default=None)):
    tenant_id = await _require_admin_scope(authorization)
    if not kb.reject_gap(DB_PATH, tenant_id, gap_id):
        raise HTTPException(status_code=404, detail="Gap not found or not open")
    return {"status": "rejected"}


# ─── Knowledge base: pending-approval documents ──────────────────────────────
# A document created by "Perform Research" above (status='pending_approval')
# is never auto-ingested — approving it here runs the same chunk/embed/
# dependency-learning pipeline a normal upload goes through, gated behind
# this explicit decision; rejecting deletes it and reopens the gap it came
# from. Ready/processing/failed documents are unaffected by these two routes
# (they 404 on anything not currently pending_approval).

@app.post("/api/knowledge-base/documents/{doc_id}/approve")
async def approve_kb_document(doc_id: str, authorization: str | None = Header(default=None)):
    tenant_id = await _require_admin_scope(authorization)
    reviewer = await _caller_label(authorization)
    if not await kb.approve_pending_document(DB_PATH, tenant_id, doc_id, reviewer):
        raise HTTPException(status_code=404, detail="Document not found, not pending approval, or ingestion failed")
    return {"status": "approved"}


@app.post("/api/knowledge-base/documents/{doc_id}/reject-pending")
async def reject_pending_kb_document(doc_id: str, authorization: str | None = Header(default=None)):
    tenant_id = await _require_admin_scope(authorization)
    if not kb.reject_pending_document(DB_PATH, tenant_id, doc_id):
        raise HTTPException(status_code=404, detail="Document not found or not pending approval")
    return {"status": "rejected"}


# ─── Knowledge base: chat ───────────────────────────────────────────────────
# A multi-turn conversation over the knowledge base, with the same
# gap-detection / permission-gated web research / staged-progress shape as
# the live incident chat (sre_agent.py), but with no AWS incident behind it
# — see kb_chat_agent.py's module docstring. Distinct from the report
# generator above: this is stateful, conversational Q&A; that is a stateless,
# deterministic multi-section report. Both stay available.

class KbChatThreadCreate(BaseModel):
    title: Optional[str] = None


@app.post("/api/kb-chat/threads")
async def create_kb_chat_thread(body: KbChatThreadCreate, authorization: str | None = Header(default=None)):
    await _require_authenticated(authorization)
    tenant_id = _resolve_kb_scope_id(authorization)
    thread_id = kb_chat_store.create_thread(DB_PATH, tenant_id, created_by=await _caller_id(authorization))
    if body.title:
        kb_chat_store.set_title_if_unset(DB_PATH, thread_id, body.title.strip()[:140])
    return {"id": thread_id, "status": "open"}


@app.get("/api/kb-chat/threads")
async def list_kb_chat_threads(authorization: str | None = Header(default=None)):
    await _require_authenticated(authorization)
    tenant_id = _resolve_kb_scope_id(authorization)
    # Your own chats only. tenant_id is one constant in this deployment, so it
    # partitions nothing on its own — the caller is what makes a thread private.
    owner = await _caller_id(authorization)
    return {"threads": kb_chat_store.list_threads(DB_PATH, tenant_id, created_by=owner)}


@app.post("/api/kb-chat/threads/{thread_id}/messages")
async def send_kb_chat_message(
    thread_id: str,
    msg: Message,
    authorization: str | None = Header(default=None),
):
    await _require_authenticated(authorization)
    tenant_id = _resolve_kb_scope_id(authorization)
    owner = await _caller_id(authorization)
    if not kb_chat_store.get_thread(DB_PATH, tenant_id, thread_id, created_by=owner):
        # 404, not 403: someone else's thread should not be distinguishable
        # from one that does not exist.
        raise HTTPException(status_code=404, detail="Thread not found")
    kb_chat_store.add_message(DB_PATH, thread_id, msg.role, msg.content, msg.type)
    if msg.role == "user":
        kb_chat_store.set_title_if_unset(DB_PATH, thread_id, msg.content[:140])
    # A real asyncio.Task (not BackgroundTasks) so the /stop endpoint below
    # can actually cancel a turn that's mid-flight, not just prevent a future one.
    kb_chat_agent.start(thread_id)
    return {"status": "received"}


@app.post("/api/kb-chat/threads/{thread_id}/stop")
async def stop_kb_chat_thread(thread_id: str, authorization: str | None = Header(default=None)):
    await _require_authenticated(authorization)
    tenant_id = _resolve_kb_scope_id(authorization)
    owner = await _caller_id(authorization)
    if not kb_chat_store.get_thread(DB_PATH, tenant_id, thread_id, created_by=owner):
        raise HTTPException(status_code=404, detail="Thread not found")
    stopped = await kb_chat_agent.stop(thread_id)
    return {"stopped": stopped}


@app.get("/api/kb-chat/threads/{thread_id}/snapshot")
async def get_kb_chat_snapshot(thread_id: str, authorization: str | None = Header(default=None)):
    """Authenticated and owner-scoped. This used to be an open GET "same
    convention as /api/incidents/{id}/snapshot", but the two are not the same
    thing: an incident is shared operational state, while a chat is one
    person's conversation. Open, it handed the full message history of any
    thread to anyone who could reach the agent and knew an id — which made
    filtering the listing cosmetic. It is a fetch, not an EventSource, so it
    can carry the Bearer token the /stream below cannot."""
    await _require_authenticated(authorization)
    tenant_id = _resolve_kb_scope_id(authorization)
    owner = await _caller_id(authorization)
    row = kb_chat_store.get_thread(DB_PATH, tenant_id, thread_id, created_by=owner)
    if not row:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {
        "thread": row,
        "messages": kb_chat_store.get_ui_messages(DB_PATH, thread_id),
        "is_running": kb_chat_agent.is_running(thread_id),
        "steps": tool_steps.load_steps(DB_PATH, "kb_chat_messages", thread_id),
    }


@app.get("/api/kb-chat/threads/{thread_id}/stream")
async def stream_kb_chat_logs(thread_id: str, authorization: str | None = Header(default=None)):
    """Per-tool progress. Owner-only, read with fetch() + Authorization."""
    await _require_authenticated(authorization)
    owner = await _caller_id(authorization)
    if not kb_chat_store.get_thread(DB_PATH, _resolve_kb_scope_id(), thread_id, created_by=owner):
        raise HTTPException(status_code=404, detail="Thread not found")

    async def log_generator():
        queue = log_queues[thread_id]
        while True:
            log = await queue.get()
            if log == "DONE":
                break
            yield {"data": log}

    return EventSourceResponse(log_generator())


class RepoAnalysisRequest(BaseModel):
    github_connection_id: str
    force: bool = False


def _analysis_view(profile: dict) -> dict:
    """What the Integrations page needs. Excerpts are deliberately omitted: they
    are repository file content and the card only summarises conventions."""
    layout = profile.get("layout") or {}
    return {
        "status": profile.get("status"),
        "repo": profile.get("repo_full_name"),
        "base_branch": profile.get("base_branch"),
        "head_sha": (profile.get("head_sha") or "")[:8],
        "analyzed_at": profile.get("_analyzed_at"),
        "error": profile.get("_error"),
        "confidence": profile.get("confidence"),
        "tf_file_count": profile.get("tf_file_count"),
        "tree_truncated": profile.get("tree_truncated"),
        "layout_style": layout.get("style"),
        "target_dir": layout.get("target_dir"),
        "modules_dir": layout.get("modules_dir"),
        "local_modules": [m.get("path") for m in (layout.get("local_modules") or [])],
        "aws_version_constraint": (profile.get("providers") or {}).get("aws_version_constraint"),
        "required_version": (profile.get("providers") or {}).get("required_version"),
        "regions": (profile.get("providers") or {}).get("regions") or [],
        "versions_file": (profile.get("files") or {}).get("versions_file"),
        "tag_mechanism": (profile.get("tagging") or {}).get("mechanism"),
        "tag_keys": (profile.get("tagging") or {}).get("required_keys") or [],
        "resource_label_case": (profile.get("naming") or {}).get("resource_label_case"),
        "backend_type": (profile.get("backend") or {}).get("type"),
        "sampled": profile.get("sampled") or [],
        "prompt_block": profile.get("_prompt_block", ""),
    }


@app.post("/api/repo-analysis")
async def analyze_repository(req: RepoAnalysisRequest, authorization: str | None = Header(default=None)):
    """Read a connected repository and cache its Terraform conventions."""
    await _require_authenticated(authorization)
    if not github_repo.ANALYSIS_ENABLED:
        raise HTTPException(status_code=503, detail="Repository analysis is disabled on this deployment.")

    blob = await fetch_github_creds(authorization, req.github_connection_id)
    if not blob or not blob.get("token") or not blob.get("repoFullName"):
        raise HTTPException(status_code=400, detail="That GitHub connection could not be resolved.")
    creds = github_push.GitHubCreds(
        token=str(blob["token"]),
        repo_full_name=str(blob["repoFullName"]),
        base_branch=str(blob.get("baseBranch") or "main"),
    )
    await agent.analyze_repo(creds, force=req.force)
    cached = agent.cached_profile(creds.repo_full_name, creds.base_branch)
    if not cached:
        raise HTTPException(status_code=500, detail="The analysis produced no result.")
    return _analysis_view(cached)


@app.get("/api/repo-analysis/by-connection/{connection_id}")
async def get_repository_analysis(connection_id: str, authorization: str | None = Header(default=None)):
    """The cached conventions for a connection, without re-reading the repository."""
    await _require_authenticated(authorization)
    blob = await fetch_github_creds(authorization, connection_id)
    if not blob or not blob.get("repoFullName"):
        raise HTTPException(status_code=400, detail="That GitHub connection could not be resolved.")
    cached = agent.cached_profile(str(blob["repoFullName"]), str(blob.get("baseBranch") or "main"))
    if not cached:
        return {"status": "not_analyzed", "repo": blob["repoFullName"]}
    return _analysis_view(cached)


@app.get("/api/runs/{run_id}/download")
async def download_workspace(run_id: str, authorization: str | None = Header(default=None)):
    await _require_run_owner(run_id, authorization)
    workspace_path = os.path.join(os.getenv("AGENT_WORKSPACE_DIR", "data/runs"), run_id, "workspace")
    if not os.path.exists(workspace_path):
        raise HTTPException(status_code=404, detail="Workspace not found")

    zip_path = os.path.join(os.getenv("AGENT_WORKSPACE_DIR", "data/runs"), run_id, f"workspace_{run_id[:8]}")
    zip_file = f"{zip_path}.zip"

    with zipfile.ZipFile(zip_file, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(workspace_path):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
            for file_name in files:
                if file_name in EXCLUDED_FILE_NAMES:
                    continue
                if file_name.endswith(EXCLUDED_FILE_SUFFIXES):
                    continue
                abs_path = os.path.join(root, file_name)
                # Zip entry names must use forward slashes; a Windows host would
                # otherwise emit "modules\vpc\main.tf" as a single odd filename.
                rel_path = os.path.relpath(abs_path, workspace_path).replace(os.sep, "/")
                zf.write(abs_path, rel_path)

    return FileResponse(
        path=zip_file,
        filename=f"terraform_workspace_{run_id[:8]}.zip",
        media_type='application/zip'
    )

# --- Workspace source-file browsing (the file list + editor in the Provisioning
# workspace panel) ---

def _workspace_tf_files(run_id: str):
    workspace_path = os.path.join(os.getenv("AGENT_WORKSPACE_DIR", "data/runs"), run_id, "workspace")
    results = []
    if not os.path.isdir(workspace_path):
        return results
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
        for fn in files:
            if not fn.endswith(TF_FILE_EXTS):
                continue
            abs_path = os.path.join(root, fn)
            rel_path = os.path.relpath(abs_path, workspace_path).replace(os.sep, "/")
            try:
                size = os.path.getsize(abs_path)
            except OSError:
                size = 0
            results.append((rel_path, abs_path, size))
    results.sort(key=lambda r: r[0])
    return results

@app.get("/api/runs/{run_id}/files")
async def list_workspace_files(run_id: str, authorization: str | None = Header(default=None)):
    await _require_run_owner(run_id, authorization)
    files = _workspace_tf_files(run_id)
    return {"files": [{"name": rel, "size": size} for rel, _abs, size in files]}

@app.get("/api/runs/{run_id}/files/{file_name:path}")
async def get_workspace_file(run_id: str, file_name: str, authorization: str | None = Header(default=None)):
    await _require_run_owner(run_id, authorization)
    # Only serve a path that exactly matches an enumerated workspace file — this
    # is the path-traversal guard (no direct os.path.join with user input).
    # A client that percent-encodes the slashes still resolves; the guard is
    # unchanged, since only an enumerated file can ever be served.
    wanted = {file_name, unquote(file_name)}
    for rel, abs_path, _size in _workspace_tf_files(run_id):
        if rel in wanted:
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"Could not read file: {e}")
            return {"name": rel, "content": content}
    raise HTTPException(status_code=404, detail="File not found")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
