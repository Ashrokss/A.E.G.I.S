"""
Pushes a completed run's Terraform workspace to GitHub as a pull request.

Called once, after a successful `terraform apply` (see agent.py's post-apply
hook) -- delivery is deterministic platform code, not an LLM tool call, since
it must always happen the same way regardless of what the model decides.
Uses the `git` CLI via subprocess (same per-run workspace layout as
tools/terraform.py) plus a plain httpx call to the GitHub REST API to open
the PR -- no extra dependency beyond what's already in requirements.txt.

Credentials resolve per run: a run that named a GitHub connection in the UI
uses that connection (see tools/integration_creds.py); otherwise it falls
back to the GITHUB_* environment variables. Resolution happens on the async
side and the result is passed in, so the worker thread never reads state a
concurrent refresh could be mutating.
"""
import base64
import os
import re
import shutil
import subprocess
import tempfile
import logging
from dataclasses import dataclass

import httpx

from tools import integration_creds

logger = logging.getLogger("uvicorn.error")

# Paths that must never leave the agent's workspace (state can carry secrets,
# .terraform/ is a large provider-plugin cache). Shared with main.py's
# workspace zip-download endpoint so the two exclusion lists never drift apart.
from tools import repo_scaffold

# One copy, shared with the workspace listing and the zip download. Re-exported
# here because main.py has always imported these two from this module.
from utils.workspace_paths import (  # noqa: E402  (kept beside its former definition)
    EXCLUDED_DIR_NAMES,
    EXCLUDED_FILE_NAMES,
    EXCLUDED_FILE_SUFFIXES,
)


class GitHubPushError(Exception):
    pass


@dataclass
class GitHubCreds:
    token: str
    repo_full_name: str
    base_branch: str = "main"


def resolve_github_creds(run_id: str | None) -> GitHubCreds | None:
    resolved = integration_creds.github_creds_for(run_id)
    if resolved and resolved.get("token") and resolved.get("repoFullName"):
        return GitHubCreds(
            token=str(resolved["token"]),
            repo_full_name=str(resolved["repoFullName"]),
            base_branch=str(resolved.get("baseBranch") or "main"),
        )
    # No environment fallback: a run pushes only with its owner's connection.
    return None


def is_configured(run_id: str | None = None) -> bool:
    return resolve_github_creds(run_id) is not None


def _run(cmd: list[str], cwd: str, env: dict, allow_fail: bool = False) -> str:
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    out = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0 and not allow_fail:
        raise GitHubPushError(f"`{' '.join(cmd[:2])} ...` failed: {out.strip()}")
    return out


def _committable_files(workspace_path: str, exclude_dirs: list[str] | None = None) -> list[str]:
    """Relative (POSIX-style) paths of workspace files that are safe to commit.

    `exclude_dirs` holds repository modules vendored in only so `terraform init`
    could resolve them — re-committing those would duplicate the customer's own
    code back into their repo as if the agent had written it.
    """
    skip = tuple(f"{d.strip('/')}/" for d in (exclude_dirs or []) if d)
    included = []
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
        for file_name in files:
            if file_name in EXCLUDED_FILE_NAMES:
                continue
            if file_name.endswith(EXCLUDED_FILE_SUFFIXES):
                continue
            abs_path = os.path.join(root, file_name)
            rel_path = os.path.relpath(abs_path, workspace_path).replace(os.sep, "/")
            if skip and rel_path.startswith(skip):
                continue
            included.append(rel_path)
    return included


def _repo_state(client, repo_full_name: str, base_branch: str, headers: dict) -> tuple[bool, str]:
    """(is_empty, effective_base_branch) for a repository.

    "Empty" means the repository exists but has no commits, so it has no branch
    to clone or to open a pull request against. `repo.size` is deliberately NOT
    the deciding signal — it is on-disk kilobytes and lags a push, so it reads 0
    for repositories that do have commits. The authoritative test is whether the
    branch actually resolves.
    """
    r = client.get(f"https://api.github.com/repos/{repo_full_name}", headers=headers)
    if r.status_code == 404:
        raise GitHubPushError(
            f"The repository `{repo_full_name}` was not found, or this token can no longer see it."
        )
    if r.status_code >= 400:
        raise GitHubPushError(
            f"GitHub returned {r.status_code} for `{repo_full_name}` and the code could not be delivered."
        )
    info = r.json() or {}
    default_branch = info.get("default_branch") or "main"
    effective = base_branch or default_branch

    if client.get(
        f"https://api.github.com/repos/{repo_full_name}/branches/{effective}", headers=headers
    ).status_code == 200:
        return False, effective

    # The configured branch is missing. If the default branch is missing too, the
    # repository has no commits; otherwise this is a misconfigured base branch,
    # which is a different problem and must not be silently bootstrapped over.
    if effective != default_branch:
        if client.get(
            f"https://api.github.com/repos/{repo_full_name}/branches/{default_branch}",
            headers=headers,
        ).status_code == 200:
            raise GitHubPushError(
                f"The repository `{repo_full_name}` has no `{effective}` branch to merge into "
                f"(its default branch is `{default_branch}`). Update the branch on the "
                f"Integrations page, then push again."
            )
    return True, default_branch


def _bootstrap_empty_repo(client, creds: "GitHubCreds", headers: dict, base_branch: str) -> str:
    """Create the first commit on an empty repository, and return its branch.

    Uses the Contents API rather than `git init`: a single PUT creates the
    default branch *and* its first commit, needing only the `contents: write`
    the push already requires. Cloning that branch afterwards then guarantees a
    common ancestor, which is what lets GitHub open a pull request at all.
    Idempotent — a 422 means someone got there first.
    """
    for path, content, message in repo_scaffold.BOOTSTRAP_FILES:
        resp = client.put(
            f"https://api.github.com/repos/{creds.repo_full_name}/contents/{path}",
            headers=headers,
            json={
                "message": message,
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": base_branch,
            },
        )
        if resp.status_code in (200, 201, 422):
            continue
        raise GitHubPushError(
            f"The repository `{creds.repo_full_name}` is empty and the first commit could not be "
            f"created ({path}: HTTP {resp.status_code}). The token needs Contents: write."
        )
    logger.info("Bootstrapped empty repo %s on %s", creds.repo_full_name, base_branch)
    return base_branch


def task_summary(goal: str, jira_ticket_key: str | None = None) -> str:
    """The one-line human description of what a run is doing.

    A run's `goal` is the first line plus (for ticket runs) the whole ticket
    description, and ticket runs prefix it with "Execute KEY:" — redundant
    everywhere the key is already shown. Strip both so branch names, commit
    subjects and PR titles read as the task itself.
    """
    summary = (goal or "").strip().splitlines()[0].strip() if (goal or "").strip() else ""
    if jira_ticket_key:
        summary = re.sub(
            rf"^execute\s+{re.escape(jira_ticket_key)}\s*[:\-]\s*", "", summary, flags=re.IGNORECASE
        ).strip()
    return summary


def _slugify(text: str, max_len: int = 40) -> str:
    """Lowercase, hyphen-separated, `[a-z0-9-]` only — safe as a git ref segment
    without needing to reason about git's ref-name rules."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    if len(slug) <= max_len:
        return slug
    # Cut on a word boundary so the name doesn't end mid-word.
    return slug[:max_len].rsplit("-", 1)[0] if "-" in slug[:max_len] else slug[:max_len]


def branch_name_for(run_id: str, jira_ticket_key: str | None = None, goal: str = "") -> str:
    """e.g. provisioning/KAN-10-create-an-ec2-instance-112bffde

    Always suffixed with the run id: two runs against the same JIRA ticket must
    not share a branch, or the second would clobber the first's code. The slug
    in the middle is for humans scanning the branch list — it carries no
    meaning, so an empty one (goal is blank or all punctuation) is fine.
    """
    parts = ["provisioning/"]
    if jira_ticket_key:
        parts.append(f"{jira_ticket_key}-")
    slug = _slugify(task_summary(goal, jira_ticket_key))
    if slug:
        parts.append(f"{slug}-")
    parts.append(run_id[:8])
    return "".join(parts)


def push_workspace_to_github(
    run_id: str,
    goal: str,
    jira_ticket_key: str | None = None,
    creds: GitHubCreds | None = None,
    applied: bool = True,
    exclude_dirs: list[str] | None = None,
) -> dict:
    """
    Commits the run's workspace to a per-run branch and opens a PR against the
    connection's base branch. Safe to call repeatedly for the same run: the
    branch is updated in place and an existing PR is reused rather than
    duplicated. Returns {url, branch, updated, created}.

    `applied` records whether the Terraform has actually been applied yet, so a
    manual pre-apply push is not described as deployed.

    Synchronous/subprocess-based -- call via asyncio.to_thread from the async
    agent loop, passing `creds` resolved on the async side.
    """
    creds = creds or resolve_github_creds(run_id)
    if not creds:
        raise GitHubPushError(
            "No GitHub connection on this run. Connect a repository on the Integrations page "
            "and pick it for the run."
        )

    workspace_path = os.path.join(os.getenv("AGENT_WORKSPACE_DIR", "data/runs"), run_id, "workspace")
    if not os.path.isdir(workspace_path):
        raise GitHubPushError(f"Workspace not found for run {run_id}.")

    files_to_commit = _committable_files(workspace_path, exclude_dirs)
    if not files_to_commit:
        raise GitHubPushError("There are no Terraform files to push yet — generate a configuration first.")

    summary = task_summary(goal, jira_ticket_key) or "provisioning run"
    branch = branch_name_for(run_id, jira_ticket_key, goal)
    remote_url = f"https://github.com/{creds.repo_full_name}.git"
    # Auth via a per-invocation extraheader rather than embedding the token in the
    # remote URL, so it never appears in git's stdout/stderr (which we surface in
    # run logs / chat on failure) or in `git remote -v`.
    #
    # Must be BASIC, not Bearer: `Bearer <token>` is accepted by GitHub's REST API
    # but not by its git-over-HTTPS transport, which fails with "could not read
    # Username". base64("x-access-token:<token>") is the scheme actions/checkout uses.
    basic = base64.b64encode(f"x-access-token:{creds.token}".encode()).decode()
    auth_header = f"AUTHORIZATION: basic {basic}"
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"

    api_headers = {
        "Authorization": f"Bearer {creds.token}",
        "Accept": "application/vnd.github+json",
    }
    # Resolve the base branch BEFORE any git work. This used to be checked after
    # the push, so a repository with no base branch got a pushed branch and then
    # an error saying no pull request could be opened for it.
    with httpx.Client(timeout=15) as client:
        is_empty, base_branch = _repo_state(client, creds.repo_full_name, creds.base_branch, api_headers)
        if is_empty:
            base_branch = _bootstrap_empty_repo(client, creds, api_headers, base_branch)

    # Build the commit in a throwaway clone of the base branch, NOT by git-init'ing
    # the Terraform workspace. A fresh init produces an orphan history with no
    # common ancestor, and GitHub refuses to open a pull request between unrelated
    # histories. Cloning also keeps the run's workspace free of a .git directory
    # and of whatever files already live in the repo.
    with tempfile.TemporaryDirectory(prefix=f"ghpush-{run_id[:8]}-") as tmp:
        _run(
            ["git", "-c", f"http.extraheader={auth_header}", "clone", "--depth", "1",
             "--branch", base_branch, remote_url, "."],
            tmp, env,
        )
        _run(["git", "config", "user.email", "agent@aegis.local"], tmp, env)
        _run(["git", "config", "user.name", "AEGIS Agent"], tmp, env)
        _run(["git", "checkout", "-B", branch], tmp, env)

        for rel in files_to_commit:
            dest = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(workspace_path, rel), dest)

        _run(["git", "add", "-A"], tmp, env)

        # Re-pushing an unchanged workspace is a normal thing to do from the UI
        # button; "nothing to commit" is not an error, we just skip the commit.
        status = _run(["git", "status", "--porcelain"], tmp, env)
        if status.strip():
            # Subject line only: `goal` carries the whole ticket description for
            # ticket runs, which makes an unreadable commit subject.
            commit_msg = f"Provision: {summary}" + (f" ({jira_ticket_key})" if jira_ticket_key else "")
            _run(["git", "commit", "-m", commit_msg], tmp, env)

        # Force: the branch is unique to this run and only ever written by the
        # agent, so a re-push should overwrite rather than fail on divergence.
        _run(
            ["git", "-c", f"http.extraheader={auth_header}", "push", "--force", remote_url, f"HEAD:{branch}"],
            tmp, env,
        )

    pr_title = (f"[{jira_ticket_key}] {summary}" if jira_ticket_key else summary)[:250]
    state = (
        "Generated, planned, and **applied** by the AEGIS provisioning agent."
        if applied
        else "Generated by the AEGIS provisioning agent. **Not applied yet** — "
             "this is the proposed configuration, pushed for review."
    )
    pr_body = (
        f"Automated Terraform provisioning{' for ' + jira_ticket_key if jira_ticket_key else ''}.\n\n"
        f"**Goal:** {goal}\n\n{state}"
    )
    owner = creds.repo_full_name.split("/", 1)[0]
    with httpx.Client(timeout=15) as client:
        # The base branch was resolved (and bootstrapped if the repo was empty)
        # before the git work above, so it is guaranteed to exist by now.
        resp = client.post(
            f"https://api.github.com/repos/{creds.repo_full_name}/pulls",
            headers=api_headers,
            json={"title": pr_title, "head": branch, "base": base_branch, "body": pr_body},
        )
        if resp.status_code < 400:
            return {"url": resp.json().get("html_url", ""), "branch": branch, "created": True, "updated": False}

        # 422 usually means "a pull request already exists for this branch" —
        # expected on a second push, so return the existing one instead of failing.
        if resp.status_code == 422:
            existing = client.get(
                f"https://api.github.com/repos/{creds.repo_full_name}/pulls",
                headers=api_headers,
                params={"head": f"{owner}:{branch}", "state": "open"},
            )
            if existing.status_code < 400 and existing.json():
                return {
                    "url": existing.json()[0].get("html_url", ""),
                    "branch": branch,
                    "created": False,
                    "updated": True,
                }

        raise GitHubPushError(f"GitHub PR creation failed ({resp.status_code}): {resp.text[:300]}")
