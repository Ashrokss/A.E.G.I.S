import os
import sqlite3
import logging
import json
import asyncio
import re
from datetime import datetime
from tools.terraform import TerraformTool
from utils.model_client import ModelClient
from utils.workspace_paths import WorkspacePathError
from utils.logger import add_run_log
from utils.browser import (
    doc_url_covers, fetch_terraform_docs, is_allowed_doc_url, registry_url_for, search_terraform_docs,
)
from utils.skills import load_skill
from tools import jira_client
from tools import github_push
from tools import github_repo
from tools import repo_profile
from tools.github_push import push_workspace_to_github, GitHubPushError

logger = logging.getLogger(__name__)

# Constants
DB_PATH = os.getenv("AGENT_DB_PATH", "runs.db")  # see main.py
TF_TOOL = TerraformTool()

_SKILL_PROMPT = load_skill("terraform-provisioning")

# Terraform's wording when the AWS provider has nothing to authenticate with.
# These are configuration problems, not Terraform problems — rewriting .tf files
# cannot fix them, so the repair loop must not be spent on them.
_CREDENTIAL_ERROR_MARKERS = (
    "no valid credential sources found",
    "failed to refresh cached credentials",
    "no ec2 imds role found",
    "unable to locate credentials",
    "the security token included in the request is expired",
    "expiredtoken",
    "invalidclienttokenid",
)


def _is_credentials_error(logs: str) -> bool:
    text = (logs or "").lower()
    return any(marker in text for marker in _CREDENTIAL_ERROR_MARKERS)


class ProvisioningAgent:
    def __init__(self):
        self.run_locks = {} # Lock per run_id
        self.stop_signals = {} # Stop signals per run_id
        self.active_steps = {}
        self._destroy_pending = {} # run_id -> bool: the saved tfplan is a destroy plan
        self._module_dir_restored: set[str] = set() # runs whose target dir was loaded from the DB
        self._repo_profile_ready: set[str] = set()  # runs whose repo profile was resolved
        self._repo_prompt_cache: dict[str, str] = {} # run_id -> rendered conventions block
        self._vendored_dirs: dict[str, list[str]] = {} # run_id -> repo modules copied in for planning
        # Trivial, stable-schema resources that should NOT block codegen on a mandatory
        # doc-fetch (requiring docs for these was deadlocking simple runs).
        self.doc_optional_resources = {"aws_key_pair"}
        # Doc guard strictness: when False, doc guards warn but don't hard-block.
        self.strict_doc_guard = os.getenv("MANDATORY_DOC_FETCH_STRICT", "true").lower() in ("1", "true", "yes", "y")
        # One shared Bedrock client. The 3-model routing config it reads is the
        # same contract this agent used to read itself:
        # - chat model for clarifications/status
        # - codegen model for terraform generation/planning
        # - heavy model for hard repair/escalation loops
        self.mc = ModelClient()
        self.model = self.mc.model
        self.model_chat = self.mc.model_chat
        self.model_codegen = self.mc.model_codegen
        self.model_heavy = self.mc.model_heavy
        # Mirrored so the existing `if not self.client:` mock guards and
        # _select_model_for_step need no edits.
        self.client = self.mc.client
        # Escalation controls (model-specific retry budgets)
        self.model_chat_max_retries = int(os.getenv("MODEL_CHAT_MAX_RETRIES", "1"))
        self.model_codegen_max_retries = int(os.getenv("MODEL_CODEGEN_MAX_RETRIES", "6"))
        self.model_heavy_max_retries = int(os.getenv("MODEL_HEAVY_MAX_RETRIES", "2"))
        # Codegen turns carry a big system prompt + full tool schemas + accumulated
        # fetched-doc content, and genuinely take longer than a trivial ping (verified:
        # the same deployment answers a bare prompt in ~1.5s, so a slow codegen turn
        # is real generation time, not a stuck connection) — give it real headroom.
        self.model_request_timeout = float(os.getenv("MODEL_REQUEST_TIMEOUT_SECONDS", "180"))
        # Total auto-repair budget before stopping and asking user.
        self.model_total_max_retries = (
            self.model_chat_max_retries
            + self.model_codegen_max_retries
            + self.model_heavy_max_retries
        )
        # Benign multi-step tool use (read/list/fetch_docs/successful write) is NOT
        # an error retry — it gets its own, much larger budget so a normal
        # generation (docs + 5 files + plan) never trips the repair budget.
        self.max_tool_iterations = int(os.getenv("MODEL_MAX_TOOL_ITERATIONS", "30"))

    def _messages_indicate_codegen_phase(self, messages: list[dict]) -> bool:
        """Terraform work has started (docs fetched, files written, plan, repair)."""
        for m in messages:
            msg_type = (m.get("type") or "").lower()
            content = str(m.get("content") or "")
            if msg_type in ("approval", "plan_preview"):
                return True
            if m.get("role") == "tool" and (
                "DOC_URL:" in content
                or ("File " in content and " written" in content)
                or "Terraform plan" in content
                or "SYSTEM_ERROR:" in content
            ):
                return True
        return False

    def _user_turn_count(self, messages: list[dict]) -> int:
        return sum(
            1
            for m in messages
            if m.get("role") == "user" and (m.get("type") or "text") not in ("system", "internal_log")
        )

    def _select_model_for_step(self, run_id: str, messages: list[dict], retry_count: int) -> tuple[str, str]:
        """
        Returns (model_name, routing_reason).

        Chat clarifications -> BEDROCK_MODEL_CHAT.
        Terraform codegen / plan / repair -> BEDROCK_MODEL_CODEGEN.
        """
        run = self._get_run(run_id) or {}
        run_status = (run.get("status") or "").upper()

        has_system_error_context = any(
            isinstance(m.get("content"), str) and "SYSTEM_ERROR:" in m.get("content", "")
            for m in messages[-8:]
        )
        codegen_statuses = {"PLANNING", "REPAIRING", "APPLYING", "AWAITING_APPROVAL"}
        chat_statuses = {"IDLE", "AWAITING_INPUT"}

        chat_limit = self.model_chat_max_retries
        codegen_limit = chat_limit + self.model_codegen_max_retries
        total_limit = self.model_total_max_retries

        in_codegen_phase = (
            run_status in codegen_statuses
            or has_system_error_context
            or self._messages_indicate_codegen_phase(messages)
            or self._user_turn_count(messages) >= 2
            or retry_count > chat_limit
        )

        if (has_system_error_context or run_status == "REPAIRING") and retry_count > codegen_limit and retry_count <= total_limit:
            return self.model_heavy, f"heavy_fallback(retry={retry_count},status={run_status or 'NA'})"

        if in_codegen_phase:
            return (
                self.model_codegen,
                f"codegen(status={run_status or 'NA'},retry={retry_count},users={self._user_turn_count(messages)})",
            )

        if run_status in chat_statuses or run_status == "":
            return self.model_chat, f"chat(status={run_status or 'IDLE'},retry={retry_count})"

        return self.model_codegen, f"codegen_fallback(status={run_status or 'NA'},retry={retry_count})"

    # Retry classification lives in ModelClient so the two agents cannot drift.
    # Kept as delegations rather than call-site edits because
    # _retry_backoff_seconds has a different signature from ModelClient.backoff.

    def _is_rate_limit_error(self, err_text: str) -> bool:
        return ModelClient.is_rate_limit(err_text)

    def _is_transient_model_error(self, err_text: str) -> bool:
        return ModelClient.is_transient(err_text)

    def _retry_after_seconds(self, err: Exception) -> float | None:
        return ModelClient.retry_after(err)

    def _retry_backoff_seconds(self, retry_count: int, is_rate_limit: bool = False) -> float:
        return ModelClient.backoff(retry_count, is_rate_limit=is_rate_limit)

    async def stop(self, run_id: str):
        if run_id not in self.stop_signals:
            self.stop_signals[run_id] = asyncio.Event()
        self.stop_signals[run_id].set()
        active_task = self.active_steps.get(run_id)
        if active_task and not active_task.done():
            active_task.cancel()
        await TF_TOOL.stop_run_process(run_id)
        self._update_status(run_id, "AWAITING_INPUT")
        self._add_message(run_id, "agent", "Stop requested. Cancelling current operation...", "internal_log")
        logger.info(f"STOP signal set for run {run_id}")

    def _get_messages(self, run_id: str):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, content, type, tool_calls, tool_call_id "
            # plan_summary/tf_step are UI-only artifacts — never feed them to the model.
            "FROM messages WHERE run_id = ? AND type NOT IN ('internal_log', 'plan_summary', 'tf_step', 'jira_tickets') ORDER BY id ASC",
            (run_id,)
        )
        rows = cursor.fetchall()
        conn.close()

        role_map = {
            "agent": "assistant",
            "assistant": "assistant",
            "user": "user",
            "system": "system",
            "developer": "developer",
            "tool": "tool",
            "function": "function",
        }

        messages = []
        for r in rows:
            msg = {
                "role": role_map.get(r["role"], "assistant"),
                "content": r["content"]
            }
            if r["type"]:
                msg["type"] = r["type"]
            if r["tool_calls"]:
                msg["tool_calls"] = json.loads(r["tool_calls"])
            if r["tool_call_id"]:
                msg["tool_call_id"] = r["tool_call_id"]
            messages.append(msg)
        return messages

    def _repair_tool_call_history(self, messages: list[dict]) -> list[dict]:
        """
        Ensures every assistant tool_call is followed by tool responses before any
        other role appears. This heals persisted runs that may contain dangling
        tool calls from interrupted executions.
        """
        if not messages:
            return messages

        repaired: list[dict] = []
        pending_tool_calls: dict[str, str] = {}

        def flush_pending_tool_calls():
            nonlocal pending_tool_calls
            if not pending_tool_calls:
                return
            for missing_call_id in list(pending_tool_calls.keys()):
                repaired.append({
                    "role": "tool",
                    "tool_call_id": missing_call_id,
                    "content": "Tool call was interrupted in a previous step. Continue with the latest user intent."
                })
            pending_tool_calls = {}

        for msg in messages:
            role = msg.get("role")

            if role == "assistant" and msg.get("tool_calls"):
                # Close any previous dangling calls before introducing new assistant tool_calls.
                flush_pending_tool_calls()
                repaired.append(msg)
                for tc in msg.get("tool_calls") or []:
                    tc_id = tc.get("id") if isinstance(tc, dict) else None
                    if tc_id:
                        pending_tool_calls[tc_id] = "pending"
                continue

            if role == "tool":
                tool_call_id = msg.get("tool_call_id")
                if tool_call_id and tool_call_id in pending_tool_calls:
                    repaired.append(msg)
                    pending_tool_calls.pop(tool_call_id, None)
                # Drop orphan tool messages without a matching pending tool_call_id.
                continue

            if pending_tool_calls:
                flush_pending_tool_calls()
            repaired.append(msg)

        if pending_tool_calls:
            flush_pending_tool_calls()

        return repaired

    def _add_message(self, run_id: str, role: str, content: str, msg_type: str = "text", tool_calls: list = None, tool_call_id: str = None, step: str = None):
        # Sanitize content: remove triple newlines and trim whitespace
        if content:
            import re
            content = re.sub(r'\n{3,}', '\n\n', content).strip()

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat()

        # Stamp the workflow phase on user-facing agent messages, derived from the
        # run's REAL status (deterministic — the model never self-declares the step).
        if step is None and role in ("agent", "assistant") and msg_type in ("text", "error"):
            try:
                cursor.execute("SELECT status FROM runs WHERE id = ?", (run_id,))
                row = cursor.fetchone()
                ph = self._phase_for(row[0] if row else None)
                step = f"{ph['n']}:{ph['name']}"
            except Exception:
                step = None

        tool_calls_json = None
        if tool_calls:
            if isinstance(tool_calls, list):
                tool_calls_json = json.dumps([
                    {
                        "id": tc.id if hasattr(tc, 'id') else tc.get('id'),
                        "type": tc.type if hasattr(tc, 'type') else tc.get('type', 'function'),
                        "function": {
                            "name": tc.function.name if hasattr(tc, 'function') else tc.get('function', {}).get('name'),
                            "arguments": tc.function.arguments if hasattr(tc, 'function') else tc.get('function', {}).get('arguments')
                        }
                    } for tc in tool_calls
                ])
            else:
                tool_calls_json = json.dumps(tool_calls)

        cursor.execute(
            "INSERT INTO messages (run_id, role, content, type, tool_calls, tool_call_id, created_at, step) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, role, content, msg_type, tool_calls_json, tool_call_id, now, step)
        )
        cursor.execute("UPDATE runs SET updated_at = ?, last_message = ? WHERE id = ?", (now, content[:100] if content else "", run_id))
        conn.commit()
        conn.close()

    def _update_status(self, run_id: str, status: str):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))
        conn.commit()
        conn.close()

    def _get_run(self, run_id: str):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def _set_run_jira_ticket(self, run_id: str, ticket_key: str):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE runs SET jira_ticket_key = ? WHERE id = ?", (ticket_key, run_id))
        conn.commit()
        conn.close()

    def _set_run_field(self, run_id: str, column: str, value):
        # column is never user input — call sites pass a literal.
        conn = sqlite3.connect(DB_PATH)
        conn.execute(f"UPDATE runs SET {column} = ? WHERE id = ?", (value, run_id))
        conn.commit()
        conn.close()

    async def _comment_on_ticket(self, run_id: str, key: str, body: str) -> str | None:
        """Post a comment and advance this run's reply watermark past it.

        Every comment the agent makes must move the watermark, or the reply
        poller reads our own words back as if the requester had answered — the
        agent then talks to itself. Advancing it here rather than at each call
        site means that holds for any future comment too, whichever Atlassian
        account the token belongs to.
        """
        cid = await jira_client.add_comment(run_id, key, body)
        if cid:
            self._set_run_field(run_id, "jira_last_comment_id", cid)
        return cid

    async def _ask_on_jira(self, run_id: str, form: dict) -> None:
        """Mirror an ask_user_form question onto the linked JIRA ticket, so a
        requester who never opens this app can answer in the ticket. Recording
        our own comment id keeps the reply-poller from reading it back as an
        answer. Best-effort: the in-app form still works if this fails."""
        run = self._get_run(run_id) or {}
        key = run.get("jira_ticket_key")
        if not key:
            return
        lines = [form.get("title") or "A few details needed"]
        if form.get("description"):
            lines.append(form["description"])
        for f in form.get("fields", []):
            opts = f.get("options") or []
            suffix = f" (options: {', '.join(opts)})" if opts else ""
            default = f" [default: {f['default']}]" if f.get("default") else ""
            lines.append(f"- {f['label']}{suffix}{default}")
        lines.append("Reply to this ticket with the details and I'll continue.")
        try:
            await self._comment_on_ticket(run_id, key, "\n".join(lines))
            add_run_log(run_id, f"INFO: Asked for the missing details on {key}.")
        except Exception as e:
            add_run_log(run_id, f"WARN: Could not comment on {key}: {e}")

    async def analyze_repo(self, creds, *, force: bool = False) -> dict:
        """Derive and cache the connected repository's Terraform conventions.

        Cached per repository+branch, not per run: a connection is shared across
        many runs, and a run started by a JIRA webhook has no user token with
        which to trigger an analysis of its own. Never raises — a repository we
        cannot read simply means the agent falls back to platform defaults.
        """
        repo, branch = creds.repo_full_name, creds.base_branch
        repo_profile.ensure_table(DB_PATH)

        live_sha = None
        try:
            live_sha = await asyncio.to_thread(github_repo.head_sha, creds.token, repo, branch)
        except Exception as e:
            logger.warning("could not read head of %s@%s: %s", repo, branch, e)

        row = repo_profile.get_cached(DB_PATH, repo, branch)
        if row and not force and not repo_profile.is_stale(row, live_sha):
            try:
                return json.loads(row["profile_json"])
            except (TypeError, ValueError):
                pass  # corrupt row — fall through and re-analyze

        try:
            snapshot = await asyncio.to_thread(github_repo.read_repo, creds.token, repo, branch)
            profile = repo_profile.build_profile(snapshot)
            block = repo_profile.render_prompt_block(profile)
            repo_profile.upsert(DB_PATH, profile, block)
            logger.info(
                "analyzed %s@%s: status=%s layout=%s target=%r",
                repo, (profile.get("head_sha") or "")[:8], profile.get("status"),
                (profile.get("layout") or {}).get("style"),
                (profile.get("layout") or {}).get("target_dir"),
            )
            return profile
        except Exception as e:
            logger.warning("repo analysis failed for %s@%s: %s", repo, branch, e)
            failed = repo_profile.validate_profile({
                "repo_full_name": repo, "base_branch": branch,
                "status": repo_profile.STATUS_ERROR,
                "analyzer_version": repo_profile.ANALYZER_VERSION,
            })
            repo_profile.upsert(DB_PATH, failed, "", error=str(e)[:400])
            return failed

    def cached_profile(self, repo_full_name: str, base_branch: str) -> dict | None:
        row = repo_profile.get_cached(DB_PATH, repo_full_name, base_branch)
        if not row:
            return None
        try:
            profile = json.loads(row["profile_json"])
        except (TypeError, ValueError):
            return None
        profile["_prompt_block"] = row.get("prompt_block") or ""
        profile["_analyzed_at"] = row.get("analyzed_at")
        profile["_error"] = row.get("error")
        return profile

    async def vendor_referenced_modules(self, run_id: str) -> list[str]:
        """Make repo modules the generated code references resolvable locally.

        A relative `source` like ../../../../modules/aws/ec2-instance points
        outside the workspace, so `terraform init` cannot resolve it and reuse of
        a customer's module is impossible without this. Copies the module's files
        into the workspace at the same relative offset. Returns the vendored
        workspace-relative directories, which are excluded from the commit —
        they already exist upstream.

        Best-effort: anything unresolvable is left alone and surfaces as a normal
        plan error the model can react to.
        """
        creds = github_push.resolve_github_creds(run_id)
        profile = self.profile_for_run(run_id)
        if not creds or not profile:
            return []
        known = {m.get("path") for m in ((profile.get("layout") or {}).get("local_modules") or [])}
        if not known:
            return []

        module_dir = TF_TOOL.module_dir_for(run_id)
        workspace = TF_TOOL._get_workspace_path(run_id)
        vendored: list[str] = []

        for rel in TF_TOOL.list_workspace_files(run_id):
            if not rel.endswith((".tf", ".tf.json")):
                continue
            body = TF_TOOL.read_tf_file(run_id, rel)
            if body.startswith("Error:"):
                continue
            for m in re.finditer(r'^\s*source\s*=\s*"(\.{1,2}/[^"]+)"', body, re.MULTILINE):
                src = m.group(1)
                # Where the source resolves relative to the terraform working dir,
                # expressed as a repo path.
                repo_path = os.path.normpath(os.path.join(module_dir, src)).replace(os.sep, "/").lstrip("./")
                if repo_path not in known:
                    continue
                dest_rel = os.path.normpath(os.path.join(module_dir, src)).replace(os.sep, "/")
                if dest_rel.startswith("..") or dest_rel in vendored:
                    continue
                if os.path.isdir(os.path.join(workspace, dest_rel)):
                    continue  # the model wrote its own module there
                try:
                    files = await asyncio.to_thread(
                        github_repo.fetch_dir, creds.token, creds.repo_full_name,
                        profile.get("head_sha") or creds.base_branch, repo_path,
                    )
                except Exception as e:
                    add_run_log(run_id, f"WARN: could not vendor `{repo_path}`: {e}")
                    continue
                if not files:
                    continue
                for rf in files:
                    TF_TOOL.write_tf_file(run_id, f"{dest_rel}/{rf.path.rsplit('/', 1)[-1]}", rf.text)
                vendored.append(dest_rel)
                add_run_log(
                    run_id,
                    f"INFO: Using the repository's existing module `{repo_path}` "
                    f"({len(files)} files copied in for planning; not re-committed).",
                )
        self._vendored_dirs[run_id] = vendored
        return vendored

    def _repo_pattern_prompt_from(self, profile: dict | None) -> str:
        """Render a profile's conventions block, or "" when there is nothing to say.

        Empty means the run behaves exactly as it did before this feature: no repo
        connected, no cached analysis, or an analysis that failed.
        """
        if not profile or profile.get("status") == repo_profile.STATUS_ERROR:
            return ""
        cached = profile.get("_prompt_block")
        if cached:
            return cached
        return repo_profile.render_prompt_block(profile)

    def _repo_pattern_prompt(self, run_id: str) -> str:
        """This run's conventions block, memoised.

        The system prompt is rebuilt on every model turn, so this must never
        re-read SQLite or re-render per turn.
        """
        if run_id in self._repo_prompt_cache:
            return self._repo_prompt_cache[run_id]
        block = ""
        creds = github_push.resolve_github_creds(run_id)
        if creds:
            profile = self.cached_profile(creds.repo_full_name, creds.base_branch)
            if profile is not None:
                # Same task-aware directory the platform points terraform at, so
                # the prompt and reality agree.
                profile["_target_dir"] = TF_TOOL.module_dir_for(run_id) or repo_profile.target_dir_for(
                    profile, (self._get_run(run_id) or {}).get("goal") or ""
                )
            block = self._repo_pattern_prompt_from(profile)
        self._repo_prompt_cache[run_id] = block
        return block

    def profile_for_run(self, run_id: str) -> dict | None:
        creds = github_push.resolve_github_creds(run_id)
        if not creds:
            return None
        return self.cached_profile(creds.repo_full_name, creds.base_branch)

    async def ensure_repo_profile_for_run(self, run_id: str) -> None:
        """Once per run: make sure a profile exists, and point this run's Terraform
        at the directory the repository's layout implies. Never raises."""
        if run_id in self._repo_profile_ready:
            return
        self._repo_profile_ready.add(run_id)
        if not github_repo.ANALYSIS_ENABLED:
            return
        creds = github_push.resolve_github_creds(run_id)
        if not creds:
            return
        try:
            profile = self.cached_profile(creds.repo_full_name, creds.base_branch)
            if profile is None or repo_profile.is_stale(
                {"analyzer_version": profile.get("analyzer_version"),
                 "analyzed_at": profile.get("_analyzed_at")}
            ):
                await self.analyze_repo(creds)
                profile = self.cached_profile(creds.repo_full_name, creds.base_branch)
            self._repo_prompt_cache.pop(run_id, None)
            # Task-aware: a per-workload repo gets a NEW sibling directory for this
            # run rather than an existing workload's folder.
            goal = (self._get_run(run_id) or {}).get("goal") or ""
            target = repo_profile.target_dir_for(profile or {}, goal)
            if profile is not None:
                profile["_target_dir"] = target
            if target and not TF_TOOL.module_dir_for(run_id):
                if self.set_run_module_dir(run_id, target):
                    add_run_log(run_id, f"INFO: Following the repository layout — writing to `{target}/`.")
        except Exception as e:
            logger.warning("[%s] repo profile setup skipped: %s", run_id, e)

    def set_run_module_dir(self, run_id: str, rel_dir: str) -> bool:
        """Set (and persist) the workspace subdirectory holding this run's root
        module. Returns False for a value that failed path validation, in which
        case the run keeps using the workspace root."""
        try:
            TF_TOOL.set_run_module_dir(run_id, rel_dir)
        except WorkspacePathError as e:
            add_run_log(run_id, f"WARN: ignoring unsafe target directory {rel_dir!r}: {e}")
            return False
        self._set_run_field(run_id, "github_target_subdir", TF_TOOL.module_dir_for(run_id) or None)
        self._module_dir_restored.add(run_id)
        return True

    def _restore_run_module_dir(self, run_id: str) -> None:
        """Reload the persisted module directory into TF_TOOL once per run, so a
        restarted agent still plans and delivers into the same place."""
        if run_id in self._module_dir_restored:
            return
        self._module_dir_restored.add(run_id)
        stored = (self._get_run(run_id) or {}).get("github_target_subdir")
        if not stored:
            return
        try:
            TF_TOOL.set_run_module_dir(run_id, stored)
        except WorkspacePathError as e:
            add_run_log(run_id, f"WARN: stored target directory {stored!r} is unusable: {e}")

    def _run_context_prompt(self, run_id: str) -> str:
        """Describe THIS run's wiring (which repo/Jira site/ticket are attached) so
        the model can answer questions about its own setup. Without it the prompt is
        static and the model wrongly denies any GitHub/Jira involvement — it has no
        tool for either, so it assumes nothing is connected."""
        run = self._get_run(run_id) or {}
        lines: list[str] = []

        gh = github_push.resolve_github_creds(run_id)
        if gh:
            lines.append(
                f"- GitHub: after a successful apply, the platform will push this run's Terraform to "
                f"`{gh.repo_full_name}` on a new `provisioning/*` branch and open a pull request against "
                f"`{gh.base_branch}`. You have no tool to browse or search this repository on demand, but "
                f"the platform has already read it — its conventions are in the \"Repository conventions\" "
                f"section if one is present — and the delivery above happens automatically, so it is "
                f"correct to tell the user their code WILL be pushed there."
            )
        else:
            lines.append("- GitHub: no repository is connected to this run, so no pull request will be opened.")

        jira = jira_client._resolve(run_id)
        if jira:
            bits = [f"site `{jira.site_url or jira.api_base_url}`"]
            if jira.default_project:
                bits.append(f"default project `{jira.default_project}`")
            if jira.review_transition_name:
                bits.append(f"tickets move to `{jira.review_transition_name}` when a run finishes")
            lines.append(
                "- JIRA: connected — " + ", ".join(bits) +
                ". You CAN read tickets with fetch_jira_tickets / get_jira_ticket."
            )
        else:
            lines.append("- JIRA: not connected for this run; the JIRA tools will fail if called.")

        ticket = run.get("jira_ticket_key")
        if ticket:
            lines.append(f"- This run is linked to JIRA ticket `{ticket}` — it will be commented on and transitioned after apply.")

        return (
            "\n\n**Current run configuration (facts about THIS run — answer questions about it from here, "
            "do not guess or deny):**\n" + "\n".join(lines) + "\n"
        )

    async def close_out_jira(
        self,
        run_id: str,
        jira_ticket_key: str,
        pr_url: str | None = None,
        applied: bool = True,
        push_error: str | None = None,
    ) -> None:
        """Post the closing comment on a ticket and move it on.

        Called from both the automatic post-apply delivery and the manual "Push
        to GitHub", because someone who raised the ticket and never opens this
        app should still see how it ended — the agent asked them questions in
        the comments, so it owes them an answer there.

        Best-effort by design: the infrastructure work is already done by the
        time this runs, so a JIRA outage must not fail the run.
        """
        goal = (self._get_run(run_id) or {}).get("goal") or ""
        summary = github_push.task_summary(goal, jira_ticket_key) or "the requested infrastructure"

        lines = [
            f"✅ Done — {summary}",
            "",
            f"Terraform: {'generated, planned and applied to AWS' if applied else 'generated and planned (not applied yet)'}",
        ]
        if pr_url:
            lines.append(f"Pull request: {pr_url}")
        elif push_error:
            lines.append(f"Pull request: could not be opened ({push_error}) — the code is available in the app.")
        else:
            lines.append("Pull request: none — no GitHub repository is connected for this run.")
        if not applied:
            lines.append("")
            lines.append("Review the plan and approve it in AEGIS to apply these changes.")

        try:
            await self._comment_on_ticket(run_id, jira_ticket_key, "\n".join(lines))
            transitioned = await jira_client.transition_ticket(run_id, jira_ticket_key)
            if not transitioned:
                add_run_log(
                    run_id,
                    f"WARN: Commented on {jira_ticket_key} but could not transition it "
                    "— no transition matching JIRA_REVIEW_TRANSITION_NAME on this board.",
                )
        except Exception as e:
            add_run_log(run_id, f"WARN: JIRA write-back failed for {jira_ticket_key}: {e}")
            self._add_message(
                run_id,
                "agent",
                f"Note: the JIRA ticket {jira_ticket_key} could not be updated automatically ({e}). Please update it manually.",
                "text",
            )

    async def _deliver_to_github_and_jira(self, run_id: str):
        """Deterministic post-apply delivery: push the applied Terraform to GitHub as a
        PR, and if the run originated from a JIRA ticket, comment + transition it.
        Runs after apply has already succeeded, so failures here are surfaced as a
        soft warning rather than failing the run."""
        run = self._get_run(run_id) or {}
        goal = run.get("goal") or "Infrastructure provisioning"
        jira_ticket_key = run.get("jira_ticket_key")
        gh_creds = github_push.resolve_github_creds(run_id)

        # No repo connected is not a reason to leave the ticket hanging — the
        # JIRA close-out used to be skipped entirely in this case.
        if not gh_creds:
            if jira_ticket_key:
                await self.close_out_jira(run_id, jira_ticket_key, applied=True)
            return

        try:
            result = await asyncio.to_thread(
                push_workspace_to_github, run_id, goal, jira_ticket_key, gh_creds, True,
                self._vendored_dirs.get(run_id) or [],
            )
            pr_url = result.get("url", "")
        except GitHubPushError as e:
            add_run_log(run_id, f"WARN: GitHub push failed: {e}")
            self._add_message(
                run_id,
                "agent",
                f"Infrastructure was applied successfully, but pushing the Terraform code to GitHub failed: {e}",
                "text",
            )
            if jira_ticket_key:
                await self.close_out_jira(run_id, jira_ticket_key, applied=True, push_error=str(e))
            return

        add_run_log(run_id, f"INFO: Pushed workspace to GitHub: {pr_url}")
        self._add_message(
            run_id,
            "agent",
            f"Terraform code pushed to GitHub — pull request opened: {pr_url}"
            + (f"\n- **JIRA ticket**: {jira_ticket_key}" if jira_ticket_key else ""),
            "text",
        )

        if jira_ticket_key:
            await self.close_out_jira(run_id, jira_ticket_key, pr_url=pr_url, applied=True)

    def _has_doc_fetch_history(self, run_id: str) -> bool:
        """
        True if doc content exists in tool history (DOC_URL) or an assistant turn requested fetch_terraform_docs.
        """
        if self._get_doc_urls_from_fetch_history(run_id):
            return True
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 1
            FROM messages
            WHERE run_id = ?
              AND tool_calls IS NOT NULL
              AND tool_calls LIKE ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (run_id, '%"name": "fetch_terraform_docs"%')
        )
        row = cursor.fetchone()
        conn.close()
        return row is not None

    def _extract_aws_resources_from_content(self, content: str) -> set[str]:
        if not content:
            return set()
        matches = re.findall(r'\b(?:resource|data)\s+"(aws_[^"]+)"\s+"[^"]+"', content)
        return set(matches)

    def _extract_resource_blocks(self, content: str) -> list[tuple[str, str, str]]:
        """
        Best-effort extraction of Terraform resource blocks.
        Returns tuples of (resource_type, resource_name, block_body).
        """
        if not content:
            return []
        blocks: list[tuple[str, str, str]] = []
        pattern = re.compile(r'resource\s+"(aws_[^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE)
        for match in pattern.finditer(content):
            rtype = match.group(1)
            rname = match.group(2)
            start = match.end()
            depth = 1
            i = start
            while i < len(content) and depth > 0:
                ch = content[i]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                i += 1
            body = content[start:i - 1] if i > start else ""
            blocks.append((rtype, rname, body))
        return blocks

    def _get_doc_urls_from_fetch_history(self, run_id: str) -> set[str]:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT content
            FROM messages
            WHERE run_id = ?
              AND role = 'tool'
              AND content LIKE 'DOC_URL:%'
            ORDER BY id ASC
            """,
            (run_id,),
        )
        rows = cursor.fetchall()
        conn.close()
        urls = set()
        for row in rows:
            content = row[0] or ""
            first_line = content.split("\n", 1)[0].strip()
            if first_line.startswith("DOC_URL:"):
                u = first_line.replace("DOC_URL:", "", 1).strip()
                if u:
                    urls.add(u)
        return urls

    def _get_tool_output_from_history(self, run_id: str, first_line: str) -> str | None:
        """Most recent tool output in this run whose first line is `first_line`
        (e.g. a cached DOC_SEARCH result) — saves a Tavily credit per repeat."""
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT content FROM messages WHERE run_id = ? AND role = 'tool' AND content LIKE ? "
            "ORDER BY id DESC LIMIT 1",
            (run_id, first_line.replace("%", "") + "\n%"),
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def _get_doc_content_from_history(self, run_id: str, url: str) -> str | None:
        """Returns cached tool output for a DOC_URL from fetch_terraform_docs in this run."""
        if not url:
            return None
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT content
            FROM messages
            WHERE run_id = ?
              AND role = 'tool'
              AND content LIKE ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (run_id, f"DOC_URL: {url}%"),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

    def _validate_tf_file_content(self, filename: str, content: str, profile: dict | None = None) -> list[str]:
        """
        Catch common AWS provider mistakes before terraform init/validate.
        Returns a list of human-readable errors (empty if OK).

        When `profile` is given, the STYLISTIC expectations follow the connected
        repository instead of the platform defaults — its AWS provider pin, and
        whether `versions.tf` is the convention. The security checks below
        (credentials, backend/cloud blocks, escaping module sources) are
        unconditional and a profile can never relax them.
        """
        errs: list[str] = []
        if not content:
            return errs
        prof_files = (profile or {}).get("files") or {}
        prof_providers = (profile or {}).get("providers") or {}
        # The repo's own pin, or the platform default when there is no profile.
        want_aws_version = prof_providers.get("aws_version_constraint") or "~> 6.0"
        versions_file_allowed = prof_files.get("versions_file") == "versions.tf"
        # Compare the basename: `environments/dev/providers.tf` must be checked
        # exactly like a root-level `providers.tf`. Normalise backslashes first,
        # since os.path.basename does not treat them as separators on Linux.
        norm_path = (filename or "").strip().replace("\\", "/").lower()
        fn = os.path.basename(norm_path)
        txt = content
        is_child_module = norm_path.startswith("modules/") or "/modules/" in norm_path

        if fn == "versions.tf" and not versions_file_allowed:
            errs.append(
                "Do not create versions.tf. Put required_version/required_providers and provider configuration in providers.tf only."
            )

        if fn == "providers.tf":
            if not re.search(r'\bterraform\s*\{', txt):
                errs.append("providers.tf must contain a top-level `terraform { ... }` block.")
            if not re.search(r'\brequired_providers\s*\{', txt):
                errs.append("providers.tf must contain `required_providers { ... }` nested inside `terraform { ... }`.")
            if re.search(r'^\s*required_providers\s*\{', txt, re.MULTILINE) and not re.search(r'\bterraform\s*\{[\s\S]*?\brequired_providers\s*\{', txt):
                errs.append("`required_providers { ... }` must be nested inside `terraform { ... }`.")
            if re.search(r'\baws\s*\{', txt) and not re.search(r'\baws\s*=\s*\{', txt):
                errs.append("Inside required_providers, aws must be assigned as `aws = { ... }`, not `aws { ... }`.")
            if not re.search(r'\bsource\s*=\s*"hashicorp/aws"', txt):
                errs.append("providers.tf required_providers.aws must set `source = \"hashicorp/aws\"`.")
            # Compare with whitespace collapsed, so "~>6.0" and "~> 6.0" are the
            # same constraint — a literal pattern would reject the tighter form.
            _found = re.search(
                r'\baws\s*=\s*\{[^}]{0,300}?\bversion\s*=\s*"([^"]{1,40})"', txt, re.DOTALL
            ) or re.search(r'\bversion\s*=\s*"([^"]{1,40})"', txt)
            _squash = lambda s: re.sub(r"\s+", "", s or "")
            if _squash(_found.group(1) if _found else "") != _squash(want_aws_version):
                errs.append(
                    f"providers.tf required_providers.aws must set `version = \"{want_aws_version}\"`"
                    + (" (the constraint this repository already uses)."
                       if prof_providers.get("aws_version_constraint") else ".")
                )
            if not is_child_module:
                provider_blocks = len(re.findall(r'^\s*provider\s+"aws"\s*\{', txt, re.MULTILINE))
                if provider_blocks != 1:
                    errs.append("providers.tf must contain exactly one `provider \"aws\" { region = ... }` block.")
                elif not re.search(r'provider\s+"aws"\s*\{[\s\S]*?\bregion\s*=', txt):
                    errs.append("The `provider \"aws\" { ... }` block must set `region`.")

        # Never allow static credentials in generated Terraform — the provider
        # must always use the container's/assumed-role's ambient AWS
        # credentials (env), never hardcoded keys committed into the workspace.
        # Never allow remote state configuration, whatever the repository does.
        # Copying the customer's `backend "s3"` makes `terraform init` adopt their
        # real state, `plan` diff production, and an approved apply mutate it. The
        # platform plans on local state and adds the backend at delivery time.
        if re.search(r'^\s*backend\s+"[a-z0-9]+"\s*\{', txt, re.MULTILINE):
            errs.append(
                "Do not write a `backend { ... }` block. Planning runs against local state; "
                "the platform adds the repository's backend configuration when it delivers the code."
            )
        if re.search(r'^\s*cloud\s*\{', txt, re.MULTILINE):
            errs.append(
                "Do not write a `cloud { ... }` block (Terraform Cloud/HCP remote state). "
                "Planning runs against local state."
            )

        # A module source must resolve inside this run's workspace: an absolute or
        # upward-escaping path cannot, so `terraform init` would fail opaquely.
        for m in re.finditer(r'^\s*source\s*=\s*"([^"]+)"', txt, re.MULTILINE):
            src = m.group(1)
            if src.startswith("/") or re.match(r"^[A-Za-z]:", src) or src.startswith("../../../"):
                errs.append(
                    f"Module `source = \"{src}\"` points outside the workspace. Use a path within "
                    "this run's files (for example `../../modules/<name>`) or a registry module."
                )

        if re.search(r'\b(access_key|secret_key)\s*=\s*"[^"]+"', txt) or re.search(r'\bAKIA[0-9A-Z]{16}\b', txt):
            errs.append(
                "Do not hardcode AWS credentials (access_key/secret_key or an AKIA... value) in any .tf file. "
                "The provider must rely on the ambient environment credentials — omit `access_key`/`secret_key` entirely."
            )

        # High-value required field checks for the most common resources, to
        # reduce invalid plans.
        for rtype, rname, body in self._extract_resource_blocks(txt):
            # A resource's local name is a quoted string in the declaration, but
            # every *reference* to it (`type.name.attr`) is an HCL identifier
            # traversal, which cannot start with a digit — `aws_s3_bucket.360x.id`
            # fails to parse ("Missing newline after argument") because `360` reads
            # as a number literal. Catch this before it ever reaches terraform, since
            # the raw parse error gives the model no signal to connect it back to
            # the resource name itself (observed: repair loop reproduced the same
            # invalid name twice in a row without this check).
            if rname and re.match(r'^[0-9]', rname):
                errs.append(
                    f'Resource local name "{rname}" for {rtype} starts with a digit, which is invalid — '
                    f'names must start with a letter or underscore (digits are fine after the first character). '
                    f'Rename it to something like "{re.sub(r"^[0-9]+", "", rname) or "resource"}" or prefix it '
                    f'(e.g. "s3_{rname}"), and update every `{rtype}.{rname}.*` reference elsewhere (main.tf, outputs.tf, etc.) to match.'
                )
            if rtype == "aws_instance":
                if not re.search(r'\bami\s*=', body):
                    errs.append(f"{rtype}.{rname} must set `ami`.")
                if not re.search(r'\binstance_type\s*=', body):
                    errs.append(f"{rtype}.{rname} must set `instance_type`.")
            if rtype == "aws_db_instance":
                if not re.search(r'\bengine\s*=', body):
                    errs.append(f"{rtype}.{rname} must set `engine`.")
                if not re.search(r'\binstance_class\s*=', body):
                    errs.append(f"{rtype}.{rname} must set `instance_class`.")
                if re.search(r'\bpublicly_accessible\s*=\s*true', body, re.IGNORECASE):
                    errs.append(f"{rtype}.{rname}: `publicly_accessible = true` is not allowed — databases must not be publicly reachable.")
            if rtype == "aws_iam_role":
                # The AegisProvisioner role may only create roles that carry the
                # AegisBoundary permissions boundary: IAM refuses anything else,
                # so catch it here instead of at apply time.
                if not re.search(r'\bpermissions_boundary\s*=', body):
                    errs.append(
                        f"{rtype}.{rname} must set `permissions_boundary = var.aegis_permissions_boundary` "
                        "(declare `variable \"aegis_permissions_boundary\" { type = string }`; AEGIS supplies the value)."
                    )
            if rtype == "aws_s3_bucket":
                if not re.search(r'\bbucket\s*=', body):
                    errs.append(f"{rtype}.{rname} must set `bucket`.")
            if rtype == "aws_security_group":
                # ingress/egress blocks have no further nesting in this provider's
                # schema, so a flat (no-brace) capture safely isolates each rule —
                # field order (from_port before/after cidr_blocks) varies in
                # practice, so check co-occurrence within one rule, not sequence.
                for ingress_body in re.findall(r'ingress\s*\{([^{}]*)\}', body):
                    opens_to_world = '0.0.0.0/0' in ingress_body
                    port_match = re.search(r'from_port\s*=\s*(\d+)', ingress_body)
                    if opens_to_world and port_match and int(port_match.group(1)) in (22, 3389):
                        errs.append(f"{rtype}.{rname}: do not open port 22/3389 to 0.0.0.0/0 — restrict to a specific CIDR.")
                        break

        return errs

    async def _run_plan_and_publish(self, run_id: str, destroy: bool = False):
        """
        Runs terraform init/validate/plan, streams logs, and updates status/message consistently.
        When destroy=True, produces a `terraform plan -destroy` (tears down the resources in
        state; the .tf files are NOT modified). Returns (success, full_log)
        """
        self._update_status(run_id, "PLANNING")
        full_log = []

        # Copy in any repository module the generated code calls, so `init` can
        # resolve its relative source. Must run before fmt/init.
        try:
            await self.vendor_referenced_modules(run_id)
        except Exception as e:
            logger.warning("[%s] module vendoring skipped: %s", run_id, e)

        add_run_log(run_id, "INFO: Formatting Terraform files (terraform fmt -recursive)")
        fmt_res = TF_TOOL.format_workspace(run_id)
        fmt_stdout = (fmt_res.get("stdout") or "").strip()
        fmt_stderr = (fmt_res.get("stderr") or "").strip()
        if fmt_stdout:
            for line in fmt_stdout.splitlines():
                add_run_log(run_id, f"STDOUT: {line}")
                full_log.append(f"STDOUT: {line}")
        if fmt_stderr:
            for line in fmt_stderr.splitlines():
                add_run_log(run_id, f"STDERR: {line}")
                full_log.append(f"STDERR: {line}")
        if fmt_res.get("exit_code", 1) != 0:
            add_run_log(run_id, "DONE")
            return False, "\n".join(full_log + ["terraform fmt failed"])

        add_run_log(run_id, "INFO: Running terraform init")
        self._emit_tf_step(run_id, "init", "running")
        init_exit = None
        async for log in TF_TOOL.run_command_stream(run_id, ["terraform", "init", "-no-color", "-input=false"]):
            if run_id in self.stop_signals and self.stop_signals[run_id].is_set():
                await TF_TOOL.stop_run_process(run_id)
                add_run_log(run_id, "INFO: Operation stopped by user during terraform init")
                add_run_log(run_id, "DONE")
                return False, "\n".join(full_log + ["Operation stopped by user"])
            add_run_log(run_id, log)
            full_log.append(log)
            if isinstance(log, str) and log.startswith("EXIT:"):
                try:
                    init_exit = int(log.split(":", 1)[1].strip())
                except ValueError:
                    init_exit = 1

        if init_exit != 0:
            self._emit_tf_step(run_id, "init", "failed")
            add_run_log(run_id, "DONE")
            return False, "\n".join(full_log)
        self._emit_tf_step(run_id, "init", "succeeded")

        # Mandatory validate before plan (syntax + provider schema). Opt out only for debugging:
        # TF_SKIP_VALIDATE=true in low-RAM Docker if plugin startup flakes (plan still runs checks).
        skip_validate = os.getenv("TF_SKIP_VALIDATE", "").lower() in ("1", "true", "yes", "y")
        if not skip_validate:
            add_run_log(run_id, "INFO: Running terraform validate")
            self._emit_tf_step(run_id, "validate", "running")
            validate_exit = None
            async for log in TF_TOOL.run_command_stream(run_id, ["terraform", "validate", "-no-color"]):
                if run_id in self.stop_signals and self.stop_signals[run_id].is_set():
                    await TF_TOOL.stop_run_process(run_id)
                    add_run_log(run_id, "INFO: Operation stopped by user during terraform validate")
                    add_run_log(run_id, "DONE")
                    return False, "\n".join(full_log + ["Operation stopped by user"])
                add_run_log(run_id, log)
                full_log.append(log)
                if isinstance(log, str) and log.startswith("EXIT:"):
                    try:
                        validate_exit = int(log.split(":", 1)[1].strip())
                    except ValueError:
                        validate_exit = 1

            if validate_exit != 0:
                self._emit_tf_step(run_id, "validate", "failed")
                add_run_log(run_id, "DONE")
                return False, "\n".join(full_log)
            self._emit_tf_step(run_id, "validate", "succeeded")

            # Brief pause so the aws plugin process can exit before plan starts another.
            await asyncio.sleep(2)
        else:
            add_run_log(run_id, "WARN: Skipping terraform validate (TF_SKIP_VALIDATE is set)")

        plan_cmd = ["terraform", "plan", "-no-color", "-input=false", "-out=tfplan"]
        if destroy:
            plan_cmd.insert(2, "-destroy")
        add_run_log(run_id, "INFO: Running terraform plan" + (" -destroy (cleanup — config unchanged)" if destroy else ""))
        self._emit_tf_step(run_id, "plan", "running", "destroy" if destroy else "")
        plan_exit = None
        async for log in TF_TOOL.run_command_stream(run_id, plan_cmd):
            if run_id in self.stop_signals and self.stop_signals[run_id].is_set():
                await TF_TOOL.stop_run_process(run_id)
                add_run_log(run_id, "INFO: Operation stopped by user during terraform plan")
                add_run_log(run_id, "DONE")
                return False, "\n".join(full_log + ["Operation stopped by user"])
            add_run_log(run_id, log)
            full_log.append(log)
            if isinstance(log, str) and log.startswith("EXIT:"):
                try:
                    plan_exit = int(log.split(":", 1)[1].strip())
                except ValueError:
                    plan_exit = 1

        if plan_exit != 0:
            self._emit_tf_step(run_id, "plan", "failed", "destroy" if destroy else "")
            # If we're going to retry, don't send DONE yet
            return False, "\n".join(full_log)
        self._emit_tf_step(run_id, "plan", "succeeded", "destroy" if destroy else "")

        plan_res = TF_TOOL.run_command(run_id, ["terraform", "show", "-no-color", "tfplan"])
        add_run_log(run_id, "DONE")
        self._update_status(run_id, "AWAITING_APPROVAL")
        return True, plan_res.get('stdout', '')

    def _compute_plan_summary(self, run_id: str):
        """Best-effort structured plan summary from `terraform show -json tfplan`.

        Powers the UI's add/change/destroy tiles and the resource-topology diagram.
        Never raises to the caller — returns None on any problem so the existing
        text preview / approve / apply flow is unaffected.
        """
        try:
            res = TF_TOOL.run_command(run_id, ["terraform", "show", "-json", "tfplan"])
            out = (res.get("stdout") or "").strip()
            if not out:
                return None
            data = json.loads(out)
        except Exception as e:
            logger.warning(f"[{run_id}] plan_summary: could not parse `terraform show -json`: {e}")
            return None

        counts = {"add": 0, "change": 0, "destroy": 0, "replace": 0}
        changes = []
        for rc in (data.get("resource_changes") or []):
            actions = ((rc.get("change") or {}).get("actions")) or []
            action_set = set(actions)
            if not action_set or action_set == {"no-op"} or action_set == {"read"}:
                continue
            if {"create", "delete"} <= action_set:
                kind = "replace"; counts["replace"] += 1
            elif action_set == {"create"}:
                kind = "create"; counts["add"] += 1
            elif action_set == {"update"}:
                kind = "update"; counts["change"] += 1
            elif action_set == {"delete"}:
                kind = "delete"; counts["destroy"] += 1
            else:
                kind = "/".join(actions)
            changes.append({
                "address": rc.get("address"),
                "type": rc.get("type"),
                "name": rc.get("name"),
                "actions": actions,
                "kind": kind,
            })
        return {"counts": counts, "changes": changes, "raw": data}

    # Locked workflow: map a run status to a phase {n, name} for the chat step chip.
    _PHASE_MAP = {
        "IDLE": (1, "Understand"),
        "AWAITING_INPUT": (2, "Clarify"),
        "PLANNING": (3, "Plan"),
        "AWAITING_APPROVAL": (4, "Review"),
        "APPLYING": (5, "Apply"),
        "REPAIRING": (6, "Repair"),
        "COMPLETED": (7, "Done"),
        "FAILED": (7, "Done"),
    }

    def _phase_for(self, status):
        n, name = self._PHASE_MAP.get((status or "").upper(), (1, "Understand"))
        return {"n": n, "name": name}

    def _emit_tf_step(self, run_id: str, step: str, status: str, detail: str = ""):
        """UI-only structured terraform step event for the Run Status checklist:
        step ∈ {init, validate, plan, apply}; status ∈ {running, succeeded, failed}.
        Best-effort — never breaks the run."""
        try:
            self._add_message(run_id, "agent", json.dumps({"step": step, "status": status, "detail": detail}), "tf_step")
        except Exception as e:
            logger.warning(f"[{run_id}] tf_step emit failed: {e}")

    async def _answer_pending_plan_question(self, run_id: str, messages: list) -> dict:
        """A plan is awaiting approval and the user sent a (non-approval) message.

        Answer it with a TOOL-LESS model call so the pending plan can never be
        regenerated as a side effect of a question. Returns
        {"replan": bool, "answer": str}; replan=True means the user actually asked
        for an infrastructure change and the caller should fall through to the
        normal generate + re-plan flow.
        """
        if not self.client:
            return {"replan": False, "answer": "The plan is ready to review. Approve to apply, or tell me what to change."}
        qa_system = {
            "role": "system",
            "content": (
                "A Terraform plan has already been generated and is waiting for the user to click Approve to apply it. "
                "The user just sent a message. Decide their intent:\n"
                "- If they are ASKING A QUESTION about the pending plan (what it creates, risks, cost, security, outputs, etc.), "
                "answer concisely in clean Markdown using the plan details already in this conversation, and remind them they can Approve when ready. "
                "Do NOT offer to regenerate and do NOT paste file contents.\n"
                "- If they are asking to CHANGE the infrastructure before applying (add/remove/modify a resource, tag, size, region, name, etc.), "
                "reply with EXACTLY this single token and nothing else: <REPLAN>\n"
                "Reply with EITHER the Markdown answer OR just <REPLAN>."
            ),
        }
        response = await self.mc.complete([qa_system] + messages,
                                          model=self.model_chat, timeout=60.0)
        content = (response.choices[0].message.content or "").strip()
        if "<REPLAN>" in content:
            return {"replan": True, "answer": ""}
        return {"replan": False, "answer": content}

    def _looks_like_destroy_request(self, text: str) -> bool:
        """Cheap prefilter for teardown intent (keeps us from calling the classifier
        on every message). The LLM classifier disambiguates true intent."""
        t = (text or "").lower()
        return bool(re.search(
            r"\b(destroy|tear\s*down|teardown|decommission|deprovision|de-provision|"
            r"nuke|clean\s*up|cleanup|take\s*down|delete|remove|wipe|dismantle|"
            r"spin\s*down|shut\s*down|shutdown)\b",
            t,
        ))

    async def _classify_destroy_intent(self, messages: list) -> bool:
        """True only when the user wants to TEAR DOWN the existing provisioned infra
        (not modify a single attribute and not ask a question)."""
        if not self.client:
            return False
        sys = {
            "role": "system",
            "content": (
                "You classify the user's LATEST message in a Terraform provisioning chat. "
                "Reply with exactly one word: DESTROY or OTHER.\n"
                "- DESTROY: the user wants to tear down / delete / clean up / decommission the EXISTING "
                "provisioned infrastructure managed by this run (e.g. 'destroy the resources', "
                "'clean up everything', 'remove the deployed infra', 'spin it all down').\n"
                "- OTHER: anything else — creating new resources, modifying/removing a single "
                "attribute/tag/name/resource in the config, or asking a question.\n"
                "Decide from the latest user message. Reply DESTROY or OTHER only."
            ),
        }
        response = await self.mc.complete([sys] + messages,
                                          model=self.model_chat, timeout=45.0)
        content = (response.choices[0].message.content or "").strip().upper()
        return content.startswith("DESTROY")

    async def step(self, run_id: str, retry_count: int = 0, doc_guard_rounds: int = 0, tool_iterations: int = 0):
        # Ensure only one step runs at a time for a given run_id
        if run_id not in self.run_locks:
            self.run_locks[run_id] = asyncio.Lock()
        task = asyncio.current_task()
        if task:
            self.active_steps[run_id] = task
        try:
            async with self.run_locks[run_id]:
                return await self._step_locked(run_id, retry_count, doc_guard_rounds, tool_iterations)
        except asyncio.CancelledError:
            # Fast-stop path: keep run interactive instead of surfacing failure.
            self._update_status(run_id, "AWAITING_INPUT")
            self._add_message(run_id, "agent", "Operation stopped by user.", "internal_log")
            if run_id in self.stop_signals:
                self.stop_signals[run_id].clear()
            add_run_log(run_id, "INFO: Stop request acknowledged. Operation cancelled.")
            add_run_log(run_id, "DONE")
            logger.info(f"Run {run_id} cancelled via task cancellation")
            return
        finally:
            self.active_steps.pop(run_id, None)

    async def _step_locked(self, run_id: str, retry_count: int = 0, doc_guard_rounds: int = 0, tool_iterations: int = 0):
        # Check for stop signal
        if run_id in self.stop_signals and self.stop_signals[run_id].is_set():
            self._update_status(run_id, "AWAITING_INPUT")
            self._add_message(run_id, "agent", "Operation stopped by user.", "internal_log")
            self.stop_signals[run_id].clear()
            logger.info(f"Run {run_id} STOPPED")
            return

        # Bound benign multi-step tool use so the model can't read/list/fetch forever.
        if tool_iterations > self.max_tool_iterations:
            self._add_message(
                run_id,
                "agent",
                "I've taken many steps exploring the workspace without finishing generation. "
                "Tell me to proceed (e.g. \"generate the Terraform now\") or give one specific correction, and I'll continue.",
                "text",
            )
            self._update_status(run_id, "AWAITING_INPUT")
            logger.warning(f"Run {run_id} hit max_tool_iterations ({self.max_tool_iterations})")
            return

        if retry_count > self.model_total_max_retries:
            self._add_message(
                run_id,
                "agent",
                "I've exhausted the automatic retry budget (mini -> 4o -> 5.4). "
                "Please provide one targeted correction, and I will continue from there.",
                "error",
            )
            self._update_status(run_id, "AWAITING_INPUT")
            return

        if not self.client:
            # Mock mode
            self._add_message(run_id, "agent", "Mock: I've received your request and I'm ready to help!")
            self._update_status(run_id, "AWAITING_INPUT")
            return

        # Cheap and once per run: a restarted agent must plan and deliver into the
        # same directory the plan-time message promised.
        self._restore_run_module_dir(run_id)
        # Once per run, best-effort: resolve the connected repository's conventions
        # and follow its layout. A webhook-started run benefits without anyone
        # visiting the Integrations page.
        await self.ensure_repo_profile_for_run(run_id)

        messages = self._repair_tool_call_history(self._get_messages(run_id))

        # Robust approval detection:
        # do not rely only on the very last message, because assistant/tool messages
        # may arrive after user approval in close race windows.
        run = self._get_run(run_id) or {}
        run_status = (run.get("status") or "").upper()
        recent_msgs = messages[-10:] if messages else []
        has_recent_approval = any(
            m.get("role") == "user" and (
                (m.get("type") == "approval")
                or ("approved" in (m.get("content", "").lower()))
            )
            for m in recent_msgs
        )

        if run_status == "AWAITING_APPROVAL" and has_recent_approval:
            self._update_status(run_id, "APPLYING")
            self._add_message(run_id, "agent", "Applying infrastructure changes...", "internal_log")
            self._emit_tf_step(run_id, "apply", "running", "destroy" if self._destroy_pending.get(run_id) else "")
            apply_exit = None
            apply_logs = []
            async for log in TF_TOOL.run_command_stream(run_id, ["terraform", "apply", "-no-color", "-input=false", "-auto-approve", "tfplan"]):
                if run_id in self.stop_signals and self.stop_signals[run_id].is_set():
                    await TF_TOOL.stop_run_process(run_id)
                    self._update_status(run_id, "AWAITING_INPUT")
                    self._add_message(run_id, "agent", "Apply stopped by user.", "internal_log")
                    add_run_log(run_id, "INFO: Apply stopped by user")
                    add_run_log(run_id, "DONE")
                    self.stop_signals[run_id].clear()
                    return
                add_run_log(run_id, log)
                apply_logs.append(log)
                if isinstance(log, str) and log.startswith("EXIT:"):
                    try:
                        apply_exit = int(log.split(":", 1)[1].strip())
                    except ValueError:
                        apply_exit = 1

            if apply_exit == 0:
                add_run_log(run_id, "DONE")
                self._update_status(run_id, "COMPLETED")
                was_destroy = self._destroy_pending.pop(run_id, False)
                self._emit_tf_step(run_id, "apply", "succeeded", "destroy" if was_destroy else "")
                self._add_message(run_id, "agent", "Apply completed successfully.", "internal_log")
                self._add_message(
                    run_id,
                    "agent",
                    "Cleanup complete. The resources managed by this run have been destroyed in AWS."
                    if was_destroy
                    else "Infrastructure deployment completed successfully. Resources are now provisioned in AWS.",
                    "text",
                )
                if not was_destroy:
                    await self._deliver_to_github_and_jira(run_id)
            else:
                self._emit_tf_step(run_id, "apply", "failed")
                add_run_log(run_id, "DONE")
                if _is_credentials_error("\n".join(apply_logs[-400:])):
                    # Creds are refreshed right before apply, so this means they
                    # went away mid-run — not something a .tf edit can repair.
                    self._add_message(
                        run_id, "agent",
                        "**Apply stopped: this run lost its AWS credentials.**\n\n"
                        "Terraform reached AWS during planning but not during apply, so the account's "
                        "temporary credentials most likely expired.\n\n"
                        "**Next:** start the work again with the same AWS account selected — the plan "
                        "will be regenerated against fresh credentials.",
                        "error",  # visible in /snapshot ("assistant"-typed rows are filtered out)
                    )
                    self._update_status(run_id, "FAILED")
                    return
                # Self-heal flow for apply failures: hand off as repair task with bounded retries.
                self._update_status(run_id, "REPAIRING")
                self._add_message(
                    run_id,
                    "agent",
                    "Apply failed. Attempting automatic self-heal by analyzing apply errors and updating Terraform...",
                    "internal_log",
                )
                self._add_message(
                    run_id,
                    "user",
                    "SYSTEM_ERROR: Terraform apply failed with errors:\n"
                    + "\n".join(apply_logs[-400:])
                    + "\nAnalyze these apply errors, fix the .tf files, and run plan again.",
                    "system",
                )
                await self._step_locked(run_id, retry_count + 1, doc_guard_rounds, tool_iterations)
            return

        # Destroy / cleanup intent: the user wants to TEAR DOWN the resources this run
        # provisioned — NOT edit the configuration. Detect it and generate a
        # `terraform plan -destroy` for approval, reusing the same approve -> apply(tfplan)
        # gate. The .tf files are never touched.
        last_user_msg = next(
            (m for m in reversed(messages)
             if m.get("role") == "user" and (m.get("type") or "text") not in ("system", "approval", "discard")),
            None,
        )
        if (
            last_user_msg
            and run_status not in ("PLANNING", "APPLYING", "REPAIRING")
            and self._looks_like_destroy_request(last_user_msg.get("content", ""))
        ):
            try:
                is_destroy = await self._classify_destroy_intent(messages)
            except Exception as e:
                logger.warning(f"[{run_id}] destroy-intent classify failed: {e}")
                is_destroy = False
            if is_destroy:
                add_run_log(run_id, "INFO: Destroy/cleanup request detected — generating a terraform destroy plan (config unchanged).")
                self._add_message(
                    run_id, "agent",
                    "Preparing a destroy plan to clean up the provisioned resources. "
                    "Your Terraform files will NOT be changed.",
                    "internal_log",
                )
                success, logs = await self._run_plan_and_publish(run_id, destroy=True)
                if success:
                    self._destroy_pending[run_id] = True
                    self._add_message(run_id, "agent", f"Plan results:\n{logs}", "plan_preview")
                    summary = self._compute_plan_summary(run_id)
                    if summary:
                        self._add_message(run_id, "agent", json.dumps(summary), "plan_summary")
                    self._add_message(
                        run_id, "agent",
                        "This is a **destroy** plan — approving it will delete the listed resources. "
                        "The `.tf` files are unchanged. Approve to tear down, or discard to keep everything.",
                        "text",
                    )
                else:
                    self._update_status(run_id, "AWAITING_INPUT")
                    self._add_message(
                        run_id, "agent",
                        "I couldn't generate a destroy plan (see the **Logs** tab). "
                        "There may be no active state to destroy yet.",
                        "text",
                    )
                return

        # A non-approval message arrived while a plan is pending approval. Answer
        # questions in place (keep the plan + the Approve/Discard gate) instead of
        # blowing the plan away and re-planning. Only fall through to the normal
        # generate + re-plan flow when the user actually asks for a change.
        if run_status == "AWAITING_APPROVAL" and not has_recent_approval:
            last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            last_type = (last_user or {}).get("type") or "text"
            if last_user and last_type not in ("discard", "approval", "system"):
                try:
                    verdict = await self._answer_pending_plan_question(run_id, messages)
                except Exception as e:
                    logger.warning(f"[{run_id}] pending-plan Q&A failed; falling back to replan: {e}")
                    verdict = None
                if verdict is not None and not verdict.get("replan"):
                    answer = verdict.get("answer") or "The plan is ready — approve to apply, or tell me what you'd like to change."
                    self._add_message(run_id, "agent", answer, "text")
                    self._update_status(run_id, "AWAITING_APPROVAL")
                    add_run_log(run_id, "DONE")
                    return
                # replan requested (or Q&A unavailable) → fall through to regenerate + re-plan

        # System prompt: skills/terraform-provisioning/SKILL.md. Docs are discovered
        # per run with search_terraform_docs (Tavily) — there is no URL table to
        # drift out of date.
        system_msg = {
            "role": "system",
            "content": (
                _SKILL_PROMPT
                + "\n\nOfficial Docs: https://registry.terraform.io/providers/hashicorp/aws/latest/docs "
                "(resource pages are .../docs/resources/<type without aws_>).\n"
                + self._run_context_prompt(run_id)
                + self._repo_pattern_prompt(run_id)
            )
        }

        full_messages = [system_msg] + messages

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "write_tf_file",
                    "description": "Writes or updates any file in the workspace (e.g. 'main.tf', 'modules/vpc/main.tf', 'scripts/deploy.sh', 'config.json', 'README.md'). Automatically creates parent directories if needed.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {
                                "type": "string",
                                "description": "Workspace-relative file path, e.g. 'main.tf', 'modules/vpc/main.tf', or 'scripts/deploy.sh'."
                            },
                            "content": {"type": "string"}
                        },
                        "required": ["filename", "content"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "start_terraform_plan",
                    "description": "Initializes and plans the Terraform configuration.",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "read_tf_file",
                    "description": "Reads a Terraform configuration file from the workspace to understand current state.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string"}
                        },
                        "required": ["filename"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "list_workspace_files",
                    "description": "Lists all files in the current Terraform workspace.",
                    "parameters": {"type": "object", "properties": {}}
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "search_terraform_docs",
                    "description": (
                        "Search official Terraform / AWS documentation (registry.terraform.io, "
                        "developer.hashicorp.com, the AWS provider repo, docs.aws.amazon.com). Use it to find "
                        "the right doc page for each resource, argument or pattern, then read the best hit "
                        "with fetch_terraform_docs. Never include account ids, ARNs or names in the query."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "e.g. 'aws_sqs_queue redrive_policy' or 'aws_lb_listener fixed-response action'"}
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_terraform_docs",
                    "description": "Fetches and reads official Terraform documentation for a specific resource or provider. Use this to verify schemas and prevent hallucinations.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "The full URL to the official documentation (e.g., https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/instance)"}
                        },
                        "required": ["url"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "fetch_jira_tickets",
                    "description": "Searches JIRA for tickets. Use for general queries like 'fetch the latest tickets' or 'show open tickets for PROJECT'. Does not start any Terraform work.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string", "description": "JIRA project key to scope the search to, e.g. 'PROJ'. Omit to use the configured default project."},
                            "jql": {"type": "string", "description": "Optional raw JQL query, overrides 'project' if given."}
                        }
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "ask_user_form",
                    "description": (
                        "Ask the user for missing information using an interactive form rendered in the chat "
                        "(dropdowns, checkboxes, text boxes) instead of a plain-text question. Use this ONLY when "
                        "a required detail genuinely has no safe default. The run pauses until the user submits."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Short heading, e.g. 'Choose a region'."},
                            "description": {"type": "string", "description": "One sentence explaining what you need and why."},
                            "fields": {
                                "type": "array",
                                "description": "The inputs to show. Keep it to the few things you genuinely need.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "description": "Machine name, e.g. 'region'."},
                                        "label": {"type": "string", "description": "Human label shown above the input."},
                                        "type": {"type": "string", "enum": ["select", "multiselect", "text", "checkbox"]},
                                        "options": {"type": "array", "items": {"type": "string"}, "description": "Choices for select/multiselect."},
                                        "default": {"type": "string", "description": "Pre-filled value where sensible."},
                                        "help": {"type": "string", "description": "Optional one-line hint under the input."}
                                    },
                                    "required": ["name", "label", "type"]
                                }
                            }
                        },
                        "required": ["title", "fields"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_jira_ticket",
                    "description": "Fetches full details (summary, description, status, priority) for one JIRA ticket by key. Use this before executing/provisioning a ticket.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "The ticket key, e.g. 'PROJ-123'."}
                        },
                        "required": ["key"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "update_jira_ticket_status",
                    "description": "Transitions a JIRA ticket to a new status (e.g. 'Done', 'In Review', 'In Progress', 'Closed'). Use when the user asks to mark a ticket as done or update its status.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string", "description": "The ticket key, e.g. 'KAN-24'."},
                            "status": {"type": "string", "description": "Target status name, e.g. 'Done', 'In Review', 'In Progress'."}
                        },
                        "required": ["key", "status"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "create_folder",
                    "description": "Creates a directory/folder in the workspace (e.g. 'scripts', 'modules/storage'). Use when the user asks to create a folder or directory.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Workspace-relative directory path to create, e.g. 'scripts' or 'modules/storage'."
                            }
                        },
                        "required": ["path"]
                    }
                }
            }
        ]

        try:
            add_run_log(run_id, "DEBUG: Sending request to Bedrock...")

            selected_model, routing_reason = self._select_model_for_step(run_id, messages, retry_count)
            add_run_log(run_id, f"DEBUG: Model routing selected: {selected_model} ({routing_reason})")

            response = await self.mc.complete(
                full_messages, tools=tools, model=selected_model,
                timeout=self.model_request_timeout,
            )
        except asyncio.TimeoutError:
            if retry_count >= self.model_total_max_retries:
                add_run_log(run_id, "ERROR: Model request timed out repeatedly. Giving up for this turn.")
                self._add_message(
                    run_id,
                    "agent",
                    "The model is taking too long to respond right now (repeated timeouts). "
                    "Please try again in a bit, or simplify the request.",
                    "error"
                )
                self._update_status(run_id, "AWAITING_INPUT")
                return
            wait_s = self._retry_backoff_seconds(retry_count)
            add_run_log(run_id, f"ERROR: Model request timed out. Retrying in {wait_s:.0f}s...")
            await asyncio.sleep(wait_s)
            await self._step_locked(run_id, retry_count + 1, doc_guard_rounds, tool_iterations)
            return
        except Exception as e:
            err_text = str(e)
            is_content_filter = "content_filter" in err_text or "ResponsibleAIPolicyViolation" in err_text
            if is_content_filter:
                add_run_log(run_id, "WARN: Model response blocked by content filter. Retrying with reduced context...")
                logger.warning(f"Content filter hit for run {run_id}, retrying with reduced context.")
                try:
                    last_user_content = ""
                    for m in reversed(messages):
                        if m.get("role") == "user" and m.get("content"):
                            last_user_content = m.get("content", "")
                            break

                    fallback_system = {
                        "role": "system",
                        "content": (
                            "You are an AWS Terraform assistant. "
                            "Use only relevant Terraform Registry docs via fetch_terraform_docs, "
                            "then generate valid Terraform using required arguments and safe defaults. "
                            "Keep output concise and avoid policy-like meta instructions."
                        )
                    }
                    fallback_user = {
                        "role": "user",
                        "content": last_user_content[:2000] if last_user_content else "Proceed with the requested infrastructure update using relevant docs."
                    }
                    fallback_messages = [fallback_system, fallback_user]

                    response = await self.mc.complete(
                        fallback_messages, tools=tools, model=selected_model,
                        timeout=self.model_request_timeout,
                    )
                except Exception as retry_err:
                    add_run_log(run_id, "ERROR: Model request blocked after reduced-context retry. Waiting for user rephrase.")
                    logger.error(f"Inference error after content-filter retry: {retry_err}")
                    self._add_message(
                        run_id,
                        "agent",
                        "I could not continue because the model safety filter blocked this request context. "
                        "Please rephrase your last message in plain infrastructure terms (resource type, region, size), and I will continue.",
                        "error"
                    )
                    self._update_status(run_id, "AWAITING_INPUT")
                    return
            else:
                add_run_log(run_id, f"ERROR: Model request failed: {err_text}")
                logger.error(f"Inference error: {e}")
                is_rate_limit = self._is_rate_limit_error(err_text)
                if self._is_transient_model_error(err_text) and retry_count < self.model_total_max_retries:
                    server_wait = self._retry_after_seconds(e)
                    wait_s = (
                        min(server_wait, 60.0)
                        if server_wait is not None
                        else self._retry_backoff_seconds(retry_count, is_rate_limit=is_rate_limit)
                    )
                    reason = "Rate limit hit" if is_rate_limit else "Transient model/network error detected"
                    add_run_log(run_id, f"WARN: {reason}. Retrying in {wait_s:.0f}s...")
                    await asyncio.sleep(wait_s)
                    await self._step_locked(run_id, retry_count + 1, doc_guard_rounds, tool_iterations)
                    return
                if is_rate_limit:
                    self._add_message(
                        run_id,
                        "agent",
                        "The AI model is rate-limited right now and retries were exhausted. "
                        "Please wait a minute and send a follow-up message to continue.",
                        "error"
                    )
                    self._update_status(run_id, "AWAITING_INPUT")
                    return
                self._update_status(run_id, "FAILED")
                return

        try:
            choice = response.choices[0]

            if choice.message.tool_calls:
                # 1. MUST save the assistant message with tool_calls first
                self._add_message(run_id, "assistant", choice.message.content or "", "assistant", tool_calls=choice.message.tool_calls)

                tool_names = [tc.function.name for tc in choice.message.tool_calls if hasattr(tc, "function") and hasattr(tc.function, "name")]
                wants_to_generate_or_plan = any(name in ("write_tf_file", "start_terraform_plan") for name in tool_names)
                has_doc_fetch_in_step = "fetch_terraform_docs" in tool_names

                resources_in_write_calls = set()
                urls_requested_in_step = set()
                for tc in choice.message.tool_calls:
                    fn = tc.function.name if hasattr(tc, "function") and hasattr(tc.function, "name") else None
                    raw_args = tc.function.arguments if hasattr(tc, "function") and hasattr(tc.function, "arguments") else "{}"
                    try:
                        parsed_args = json.loads(raw_args or "{}")
                    except Exception:
                        parsed_args = {}

                    if fn == "write_tf_file":
                        resources_in_write_calls |= self._extract_aws_resources_from_content(parsed_args.get("content", ""))
                    elif fn == "fetch_terraform_docs":
                        url = parsed_args.get("url")
                        if isinstance(url, str) and url.strip():
                            urls_requested_in_step.add(url.strip())

                has_doc_fetch_history = self._has_doc_fetch_history(run_id)

                # Hard guard: before generating/updating terraform or planning, the agent must
                # fetch official docs first to reduce schema mistakes.
                # We allow proceeding when docs were already fetched earlier in this run
                # to avoid forcing redundant/unrelated doc fetch loops.
                if wants_to_generate_or_plan and not has_doc_fetch_in_step and not has_doc_fetch_history:
                    guard_msg = (
                        "MANDATORY_DOC_FETCH_REQUIRED: You must call fetch_terraform_docs with relevant official "
                        "Terraform Registry URLs before writing any .tf file or starting terraform plan. "
                        "First fetch docs, then regenerate/update files."
                    )
                    if self.strict_doc_guard and doc_guard_rounds < 3:
                        # Strict mode: enforce guard with limited automatic retries
                        add_run_log(run_id, f"INFO: {guard_msg}")
                        for tc in choice.message.tool_calls:
                            tc_id = tc.id if hasattr(tc, "id") else None
                            if tc_id:
                                self._add_message(run_id, "tool", guard_msg, "tool_output", tool_call_id=tc_id)
                        self._add_message(
                            run_id,
                            "agent",
                            "I need to fetch the official Terraform Registry docs for your requested resources "
                            "before generating or planning Terraform. Fetching the relevant documentation now...",
                            "internal_log",
                        )
                        await self._step_locked(run_id, retry_count, doc_guard_rounds + 1, tool_iterations + 1)
                        return
                    elif self.strict_doc_guard:
                        # Keep docs mandatory, but avoid noisy error styling in logs.
                        add_run_log(run_id, f"INFO: {guard_msg} (max guard retries reached)")
                        for tc in choice.message.tool_calls:
                            tc_id = tc.id if hasattr(tc, "id") else None
                            if tc_id:
                                self._add_message(run_id, "tool", guard_msg, "tool_output", tool_call_id=tc_id)
                        self._add_message(
                            run_id,
                            "agent",
                            "I tried multiple times to enforce mandatory documentation fetching before generating Terraform, "
                            "but I am still unable to proceed automatically. "
                            "Please confirm the exact AWS resources you want (with links to their Terraform docs if possible), "
                            "and I will continue from there.",
                            "error",
                        )
                        self._update_status(run_id, "AWAITING_INPUT")
                        return
                    else:
                        # Soft mode: warn but continue so pre-write validation still protects correctness.
                        add_run_log(run_id, f"WARN: {guard_msg} (soft mode - continuing without hard block)")

                # Resource-level guard: if the model is writing Terraform resources, ensure
                # the relevant docs for those resources were fetched in this run (or this step).
                required_types = {
                    r for r in resources_in_write_calls if r not in self.doc_optional_resources
                }
                if required_types:
                    fetched_urls = self._get_doc_urls_from_fetch_history(run_id)
                    available_urls = fetched_urls | urls_requested_in_step
                    # Covers EVERY aws_* type, not a fixed list: a type is documented
                    # once any fetched URL is its registry / provider-markdown page.
                    missing_urls = sorted(
                        registry_url_for(r) for r in required_types
                        if not any(doc_url_covers(u, r) for u in available_urls)
                    )
                    if missing_urls:
                        missing_msg = (
                            "MANDATORY_RESOURCE_DOCS_MISSING: Fetch docs for the exact resources in this Terraform update before code generation.\n"
                            + "\n".join(missing_urls)
                        )
                        if self.strict_doc_guard and doc_guard_rounds < 3:
                            add_run_log(run_id, f"INFO: {missing_msg.replace(chr(10), ' | ')}")
                            for tc in choice.message.tool_calls:
                                tc_id = tc.id if hasattr(tc, "id") else None
                                if tc_id:
                                    self._add_message(run_id, "tool", missing_msg, "tool_output", tool_call_id=tc_id)
                            self._add_message(
                                run_id,
                                "agent",
                                "Fetching only the required HashiCorp Terraform docs for the resources in your request before "
                                "regenerating the configuration...",
                                "internal_log",
                            )
                            await self._step_locked(run_id, retry_count, doc_guard_rounds + 1, tool_iterations + 1)
                            return
                        elif self.strict_doc_guard:
                            # Max rounds reached: continue anyway instead of dead-ending with
                            # zero files. Pre-write schema validation still guards correctness.
                            add_run_log(run_id, f"WARN: {missing_msg.replace(chr(10), ' | ')} (max guard retries reached — continuing; pre-write schema validation still applies)")
                        else:
                            # Soft mode: log a warning and continue; pre-write validation still guards schema errors.
                            add_run_log(run_id, f"WARN: {missing_msg.replace(chr(10), ' | ')} (soft mode - continuing without hard block)")

                wrote_tf_file = False
                write_validation_failed = False
                triggered_plan = False
                plan_success = True
                plan_logs = ""
                asked_form = False

                for tool_call in choice.message.tool_calls:
                    name = tool_call.function.name
                    args = json.loads(tool_call.function.arguments)
                    tool_id = tool_call.id

                    if name == "write_tf_file":
                        filename = args.get('filename')
                        content = args.get('content')
                        if not filename or content is None:
                            err_msg = f"Error: Agent tried to write file {filename} but content was missing (likely truncated). You MUST provide the full content of the file."
                            add_run_log(run_id, f"ERROR: {err_msg}")
                            self._add_message(run_id, "tool", err_msg, "tool_output", tool_call_id=tool_id)
                            # Force a retry by the model for this specific failure
                            triggered_plan = False # Ensure we don't plan if a write failed
                            continue

                        _base = (
                            os.path.basename(filename.strip().replace("\\", "/")).lower()
                            if isinstance(filename, str) else ""
                        )
                        _profile = self.profile_for_run(run_id)
                        _versions_ok = ((_profile or {}).get("files") or {}).get("versions_file") == "versions.tf"
                        if _base == "versions.tf" and not _versions_ok:
                            err_msg = "versions.tf is disabled. Put provider version and required_providers in providers.tf."
                            add_run_log(run_id, f"ERROR: {err_msg}")
                            self._add_message(run_id, "tool", err_msg, "tool_output", tool_call_id=tool_id)
                            write_validation_failed = True
                            triggered_plan = False
                            continue

                        validation_errors = self._validate_tf_file_content(filename, content, profile=_profile)
                        if validation_errors:
                            err_msg = "Terraform validation (pre-write): " + " | ".join(validation_errors)
                            add_run_log(run_id, f"ERROR: {err_msg}")
                            self._add_message(run_id, "tool", err_msg, "tool_output", tool_call_id=tool_id)
                            write_validation_failed = True
                            triggered_plan = False
                            continue

                        add_run_log(run_id, f"INFO: Agent is writing file: {filename}")
                        TF_TOOL.write_tf_file(run_id, filename, content)
                        wrote_tf_file = True
                        self._add_message(run_id, "tool", f"File {filename} written.", "tool_output", tool_call_id=tool_id)
                        self._add_message(run_id, "agent", f"Generating file: {filename}", "internal_log")

                    elif name == "start_terraform_plan":
                        if write_validation_failed:
                            msg = "Plan blocked: fix all pre-write Terraform validation errors before planning."
                            add_run_log(run_id, f"ERROR: {msg}")
                            self._add_message(run_id, "tool", msg, "tool_output", tool_call_id=tool_id)
                            triggered_plan = False
                            continue
                        # Never plan an empty workspace — that yields a confusing
                        # "No configuration files" error and a false repair loop. Nudge
                        # the model to write the files first.
                        # Root module only: a workspace holding just
                        # modules/vpc/main.tf has nothing for `plan` to read.
                        existing_tf = TF_TOOL.root_module_tf_files(run_id)
                        if not existing_tf:
                            msg = ("Cannot plan: the workspace has no .tf files yet. Call write_tf_file to create "
                                   "providers.tf, main.tf, variables.tf, outputs.tf (and terraform.tfvars) BEFORE start_terraform_plan.")
                            add_run_log(run_id, f"WARN: {msg}")
                            self._add_message(run_id, "tool", msg, "tool_output", tool_call_id=tool_id)
                            triggered_plan = False
                            continue
                        triggered_plan = True
                        self._update_status(run_id, "PLANNING")
                        self._add_message(run_id, "agent", "Starting Terraform plan...", "internal_log")
                        success, logs = await self._run_plan_and_publish(run_id)
                        plan_success = success
                        plan_logs = logs

                        tool_res = "Terraform plan started." if success else f"Terraform plan failed:\n{logs}"
                        self._add_message(run_id, "tool", tool_res, "tool_output", tool_call_id=tool_id)

                        if success:
                            # A normal create/update plan supersedes any pending destroy plan.
                            self._destroy_pending[run_id] = False
                            # Add the preview AFTER the tool response to maintain sequence
                            self._add_message(run_id, "agent", f"Plan results:\n{logs}", "plan_preview")
                            # UI-only: structured summary (counts + resource changes + raw
                            # show-json) for the plan tiles, colored diff, and topology diagram.
                            summary = self._compute_plan_summary(run_id)
                            if summary:
                                self._add_message(run_id, "agent", json.dumps(summary), "plan_summary")

                    elif name == "read_tf_file":
                        filename = args.get('filename')
                        if not filename:
                            self._add_message(run_id, "tool", "Error: Missing filename in read_tf_file call.", "tool_output", tool_call_id=tool_id)
                            continue
                        add_run_log(run_id, f"INFO: Agent is reading file: {filename}")
                        content = TF_TOOL.read_tf_file(run_id, filename)
                        self._add_message(run_id, "tool", content, "tool_output", tool_call_id=tool_id)

                    elif name == "list_workspace_files":
                        add_run_log(run_id, "INFO: Agent is listing workspace files")
                        files = TF_TOOL.list_workspace_files(run_id)
                        self._add_message(run_id, "tool", f"Files in workspace: {', '.join(files)}", "tool_output", tool_call_id=tool_id)

                    elif name == "search_terraform_docs":
                        query = (args.get("query") or "").strip()
                        cache_key = f"DOC_SEARCH: {query}"
                        cached = self._get_tool_output_from_history(run_id, cache_key)
                        if cached:
                            self._add_message(run_id, "tool", cached, "tool_output", tool_call_id=tool_id)
                            continue
                        add_run_log(run_id, f"INFO: Searching Terraform docs: {query}")
                        hits = await search_terraform_docs(query)
                        if hits:
                            body = "\n".join(
                                f"{n}. {h['title']}\n   {h['url']}\n   {(h.get('snippet') or '')[:300]}"
                                for n, h in enumerate(hits, 1)
                            )
                            body += "\n\nRead the most relevant page with fetch_terraform_docs."
                        else:
                            # Tavily unavailable or no hit: the registry URL is a pure
                            # function of the resource type, so point straight at it.
                            guesses = sorted(set(re.findall(r"\baws_[a-z0-9_]+", query)))
                            body = "No search results (search unavailable or nothing matched)."
                            if guesses:
                                body += " Fetch the registry page directly:\n" + "\n".join(
                                    registry_url_for(g) for g in guesses
                                )
                        self._add_message(run_id, "tool", f"{cache_key}\n\n{body}", "tool_output", tool_call_id=tool_id)

                    elif name == "fetch_terraform_docs":
                        url = (args.get('url') or "").strip()
                        if not url:
                            self._add_message(run_id, "tool", "Error: Missing url in fetch_terraform_docs call.", "tool_output", tool_call_id=tool_id)
                            continue
                        if not is_allowed_doc_url(url):
                            self._add_message(
                                run_id,
                                "tool",
                                "Error: Not an official documentation URL. Use search_terraform_docs to find the "
                                "registry.terraform.io / developer.hashicorp.com / docs.aws.amazon.com page.",
                                "tool_output",
                                tool_call_id=tool_id,
                            )
                            continue
                        cached_doc = self._get_doc_content_from_history(run_id, url)
                        if cached_doc:
                            add_run_log(run_id, f"INFO: Documentation already fetched (cache hit): {url}")
                            self._add_message(run_id, "tool", cached_doc, "tool_output", tool_call_id=tool_id)
                            continue
                        add_run_log(run_id, f"INFO: Agent is fetching documentation: {url}")
                        content = await fetch_terraform_docs(url)
                        self._add_message(run_id, "tool", f"DOC_URL: {url}\n\n{content}", "tool_output", tool_call_id=tool_id)
                        if isinstance(content, str) and content.strip().startswith("Error:"):
                            add_run_log(run_id, f"ERROR: Documentation fetch failed for {url}")
                        else:
                            add_run_log(run_id, f"INFO: Documentation fetched successfully: {url}")

                    elif name == "ask_user_form":
                        raw_fields = args.get("fields") or []
                        fields = []
                        for f in raw_fields:
                            if not isinstance(f, dict) or not f.get("name") or not f.get("label"):
                                continue
                            ftype = f.get("type") if f.get("type") in ("select", "multiselect", "text", "checkbox") else "text"
                            opts = [str(o) for o in (f.get("options") or []) if str(o).strip()]
                            # A select with no options is unanswerable — degrade to free text.
                            if ftype in ("select", "multiselect") and not opts:
                                ftype = "text"
                            fields.append({
                                "name": str(f["name"]),
                                "label": str(f["label"]),
                                "type": ftype,
                                "options": opts,
                                "default": str(f.get("default") or ""),
                                "help": str(f.get("help") or ""),
                            })
                        if not fields:
                            self._add_message(
                                run_id, "tool",
                                "ask_user_form needs at least one valid field (name, label, type). Ask in plain text instead.",
                                "tool_output", tool_call_id=tool_id,
                            )
                            continue
                        payload = {
                            "title": str(args.get("title") or "A few details needed"),
                            "description": str(args.get("description") or ""),
                            "fields": fields,
                        }
                        add_run_log(run_id, f"INFO: Asking the user for input via a form ({len(fields)} field(s))")
                        self._add_message(run_id, "tool", "Form shown to the user; awaiting their answer.", "tool_output", tool_call_id=tool_id)
                        self._add_message(run_id, "agent", json.dumps(payload), "form")
                        # Ticket-driven run: the requester is in JIRA, not watching
                        # this chat — ask there too and pick their reply up on the
                        # next poll.
                        await self._ask_on_jira(run_id, payload)
                        self._update_status(run_id, "AWAITING_INPUT")
                        add_run_log(run_id, "DONE")
                        asked_form = True

                    elif name == "fetch_jira_tickets":
                        project = args.get("project")
                        jql = args.get("jql")
                        add_run_log(run_id, f"INFO: Searching JIRA tickets (project={project or 'default'})")
                        try:
                            tickets = await jira_client.search_tickets(run_id, jql=jql, project=project)
                            self._add_message(run_id, "tool", json.dumps(tickets), "tool_output", tool_call_id=tool_id)
                            # UI-only: render the tickets as a table card rather than
                            # relying on the model to re-type them as markdown.
                            if tickets:
                                self._add_message(
                                    run_id, "agent",
                                    json.dumps({"scope": project or "", "tickets": tickets}),
                                    "jira_tickets",
                                )
                        except Exception as e:
                            add_run_log(run_id, f"ERROR: JIRA search failed: {e}")
                            self._add_message(run_id, "tool", f"Error searching JIRA: {e}", "tool_output", tool_call_id=tool_id)

                    elif name == "get_jira_ticket":
                        key = args.get("key")
                        if not key:
                            self._add_message(run_id, "tool", "Error: Missing key in get_jira_ticket call.", "tool_output", tool_call_id=tool_id)
                            continue
                        add_run_log(run_id, f"INFO: Fetching JIRA ticket: {key}")
                        try:
                            ticket = await jira_client.get_ticket(run_id, key)
                            self._set_run_jira_ticket(run_id, key)
                            self._add_message(run_id, "tool", json.dumps(ticket), "tool_output", tool_call_id=tool_id)
                            self._add_message(
                                run_id, "agent",
                                json.dumps({"scope": key, "tickets": [ticket]}),
                                "jira_tickets",
                            )
                        except Exception as e:
                            add_run_log(run_id, f"ERROR: JIRA fetch failed for {key}: {e}")
                            self._add_message(run_id, "tool", f"Error fetching JIRA ticket {key}: {e}", "tool_output", tool_call_id=tool_id)

                    elif name == "update_jira_ticket_status":
                        key = args.get("key")
                        status = args.get("status")
                        if not key or not status:
                            self._add_message(run_id, "tool", "Error: Both key and status are required for update_jira_ticket_status.", "tool_output", tool_call_id=tool_id)
                            continue
                        add_run_log(run_id, f"INFO: Updating JIRA ticket {key} status to '{status}'")
                        try:
                            success = await jira_client.transition_ticket(run_id, key, target_status_name=status)
                            if success:
                                msg = f"Successfully updated JIRA ticket {key} status to '{status}'."
                            else:
                                msg = f"Could not transition JIRA ticket {key} to '{status}'. Please verify '{status}' is a valid transition on this JIRA project workflow."
                            add_run_log(run_id, f"INFO: {msg}")
                            self._add_message(run_id, "tool", msg, "tool_output", tool_call_id=tool_id)
                        except Exception as e:
                            err_msg = f"Failed to update JIRA ticket {key}: {e}"
                            add_run_log(run_id, f"ERROR: {err_msg}")
                            self._add_message(run_id, "tool", err_msg, "tool_output", tool_call_id=tool_id)

                    elif name == "create_folder":
                        path = args.get("path")
                        if not path:
                            self._add_message(run_id, "tool", "Error: Missing path in create_folder call.", "tool_output", tool_call_id=tool_id)
                            continue
                        add_run_log(run_id, f"INFO: Agent is creating folder: {path}")
                        res_msg = TF_TOOL.create_folder(run_id, path)
                        self._add_message(run_id, "tool", res_msg, "tool_output", tool_call_id=tool_id)

                # A form was shown — the run is deliberately parked on the user's
                # answer. Don't recurse, or the model would talk over its own form.
                if asked_form:
                    return

                # After ALL tools are handled, decide next step
                if triggered_plan and not plan_success and _is_credentials_error(plan_logs):
                    # No .tf edit can fix a missing credential, so don't spend
                    # the repair budget rewriting files. Stop and say what to do.
                    add_run_log(run_id, "ERROR: Terraform has no AWS credentials for this run")
                    self._add_message(
                        run_id, "agent",
                        "**PROVISIONING_ACCOUNT_UNBOUND: this run has no AWS credentials.**\n\n"
                        "The Terraform itself is fine — `init` and `validate` both passed. Only `plan` "
                        "failed, because it is the first step that talks to AWS. AEGIS never falls back "
                        "to its own identity, so every run must be bound to one of your accounts.\n\n"
                        "**To fix it:** onboard an account on the **Accounts** page if you haven't, then "
                        "start this work again with that account selected.",
                        "error",  # visible in /snapshot ("assistant"-typed rows are filtered out)
                    )
                    self._update_status(run_id, "FAILED")
                    add_run_log(run_id, "DONE")
                    return

                if triggered_plan and not plan_success:
                    # Real failure -> spend the repair budget.
                    self._update_status(run_id, "REPAIRING")
                    self._add_message(run_id, "user", f"SYSTEM_ERROR: Terraform operation failed with errors:\n{plan_logs}\nAnalyze these errors and fix the .tf files immediately.", "system")
                    await self._step_locked(run_id, retry_count + 1, doc_guard_rounds, tool_iterations)
                    return
                # If the model called tools that don't terminate the step (like reading), we step again
                if not triggered_plan:
                    if write_validation_failed:
                        # Pre-write validation error -> repair path, spend the repair budget.
                        self._update_status(run_id, "REPAIRING")
                        self._add_message(
                            run_id,
                            "user",
                            "SYSTEM_ERROR: Pre-write Terraform validation failed. Review the specific errors above "
                            "(provider block shape, required arguments, or a hardcoded-credentials guard) and regenerate all .tf files.",
                            "system",
                        )
                        if retry_count >= self.model_codegen_max_retries - 1:
                            self._add_message(
                                run_id,
                                "agent",
                                "I'm still fixing the generated Terraform. If this keeps failing, describe the exact resource "
                                "and its required fields (e.g. 'an aws_instance with ami and instance_type set').",
                                "text",
                            )
                        await self._step_locked(run_id, retry_count + 1, doc_guard_rounds, tool_iterations)
                        return
                    # Benign progress (read/list/fetch_docs/successful write): continue WITHOUT
                    # spending the repair budget — only the tool-iteration budget.
                    await self._step_locked(run_id, retry_count, doc_guard_rounds, tool_iterations + 1)
                    return

            elif choice.message.content:
                self._add_message(run_id, "agent", choice.message.content)
                self._update_status(run_id, "AWAITING_INPUT")

        except Exception as e:
            logger.error(f"Error processing choice: {e}")
            self._add_message(run_id, "agent", f"An error occurred: {str(e)}", "error")
            self._update_status(run_id, "FAILED")
