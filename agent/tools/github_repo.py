"""Read-only access to a connected repository, for deriving its conventions.

Uses the same httpx + Bearer style as tools/github_push.py — no new dependency.
Deliberately narrow: one call for the whole file index, then a bounded set of
blob reads. There is no tool that lets the model browse a repository; what it
sees is the pre-computed profile in tools/repo_profile.py.
"""
import base64
import logging
import os
import re
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

API = "https://api.github.com"

# Guards against a monorepo turning one analysis into a multi-megabyte download.
MAX_FILES = 60
# Separate budget: module interfaces are what make reuse possible at all.
MAX_MODULE_FILES = 60
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 768 * 1024

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

# What is worth reading to learn a repo's conventions, most informative first.
_INTERESTING_BASENAMES = (
    "versions.tf", "providers.tf", "provider.tf", "main.tf", "variables.tf",
    "outputs.tf", "locals.tf", "backend.tf", "terraform.tfvars", "terragrunt.hcl",
)
_SKIP_DIR_SEGMENTS = {
    ".terraform", ".git", "node_modules", "vendor", ".venv", "__pycache__",
    "dist", "build", ".idea", ".vscode",
}


class RepoReadError(RuntimeError):
    """The repository could not be read. Always non-fatal to a run."""


@dataclass
class RepoFile:
    path: str
    size: int
    text: str = ""


@dataclass
class RepoSnapshot:
    repo_full_name: str
    base_branch: str
    head_sha: str = ""
    is_empty: bool = False
    tree_truncated: bool = False
    # Every path in the repo (excluding skipped dirs), for layout detection.
    all_paths: list[str] = field(default_factory=list)
    # The subset actually downloaded.
    files: list[RepoFile] = field(default_factory=list)

    @property
    def tf_paths(self) -> list[str]:
        return [p for p in self.all_paths if p.endswith((".tf", ".tf.json"))]


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _check_repo_name(repo_full_name: str) -> None:
    # Node validates this on the way in, but this module interpolates it into a
    # URL and Python had no guard of its own.
    if not _REPO_RE.match(repo_full_name or ""):
        raise RepoReadError(f"{repo_full_name!r} is not a valid owner/repo name")


def _skip(path: str) -> bool:
    return any(seg in _SKIP_DIR_SEGMENTS for seg in path.split("/")[:-1])


def select_paths(all_paths: list[str], sizes: dict[str, int]) -> list[str]:
    """Which files to download, ranked by how much they reveal about conventions.

    Pure so the ranking can be tested without touching the network.
    """
    def rank(path: str) -> tuple:
        base = path.rsplit("/", 1)[-1].lower()
        depth = path.count("/")
        try:
            named = _INTERESTING_BASENAMES.index(base)
        except ValueError:
            named = len(_INTERESTING_BASENAMES)
        # Shallower first: a root module at environments/dev beats a deep example.
        return (named, depth, path)

    candidates = [
        p for p in all_paths
        if not _skip(p)
        and p.endswith((".tf", ".tfvars", ".hcl"))
        and sizes.get(p, 0) <= MAX_FILE_BYTES
    ]
    # A module's variables.tf is the only way to know how to CALL that module, so
    # module files get their own budget instead of competing with root-module
    # files — otherwise every module reports "required inputs: none".
    def is_module_iface(p: str) -> bool:
        base = p.rsplit("/", 1)[-1].lower()
        return "modules/" in f"{p}/" and base in ("variables.tf", "outputs.tf", "main.tf")

    module_files = sorted([p for p in candidates if is_module_iface(p)], key=rank)
    other_files = sorted([p for p in candidates if not is_module_iface(p)], key=rank)

    chosen: list[str] = []
    total = 0
    for bucket, max_n in ((module_files, MAX_MODULE_FILES), (other_files, MAX_FILES)):
        taken = 0
        for p in bucket:
            size = sizes.get(p, 0)
            if taken >= max_n or len(chosen) >= MAX_FILES + MAX_MODULE_FILES:
                break
            if total + size > MAX_TOTAL_BYTES:
                break
            chosen.append(p)
            total += size
            taken += 1
    return chosen


def read_repo(token: str, repo_full_name: str, base_branch: str, *, timeout: float = 20.0) -> RepoSnapshot:
    """One tree call plus a bounded set of blob reads. Never raises for an empty
    repository — it returns a snapshot with is_empty set."""
    _check_repo_name(repo_full_name)
    snap = RepoSnapshot(repo_full_name=repo_full_name, base_branch=base_branch)
    headers = _headers(token)

    with httpx.Client(timeout=timeout) as client:
        ref = client.get(
            f"{API}/repos/{repo_full_name}/git/ref/heads/{base_branch}", headers=headers
        )
        if ref.status_code in (404, 409):
            snap.is_empty = True
            return snap
        if ref.status_code >= 400:
            raise RepoReadError(f"could not read {repo_full_name}@{base_branch}: HTTP {ref.status_code}")
        snap.head_sha = (((ref.json() or {}).get("object") or {}).get("sha")) or ""
        if not snap.head_sha:
            raise RepoReadError(f"{repo_full_name}@{base_branch} returned no commit sha")

        # One request for the entire file index.
        tree = client.get(
            f"{API}/repos/{repo_full_name}/git/trees/{snap.head_sha}",
            params={"recursive": "1"},
            headers=headers,
        )
        if tree.status_code >= 400:
            raise RepoReadError(f"could not list {repo_full_name}: HTTP {tree.status_code}")
        payload = tree.json() or {}
        snap.tree_truncated = bool(payload.get("truncated"))

        sizes: dict[str, int] = {}
        blob_shas: dict[str, str] = {}
        for entry in payload.get("tree") or []:
            if entry.get("type") != "blob":
                continue
            path = entry.get("path") or ""
            if not path:
                continue
            snap.all_paths.append(path)
            sizes[path] = int(entry.get("size") or 0)
            blob_shas[path] = entry.get("sha") or ""

        for path in select_paths(snap.all_paths, sizes):
            sha = blob_shas.get(path)
            if not sha:
                continue
            blob = client.get(f"{API}/repos/{repo_full_name}/git/blobs/{sha}", headers=headers)
            if blob.status_code >= 400:
                logger.warning("skipping %s: HTTP %s", path, blob.status_code)
                continue
            body = blob.json() or {}
            text = ""
            if body.get("encoding") == "base64":
                try:
                    text = base64.b64decode(body.get("content") or "").decode("utf-8", "replace")
                except Exception:
                    text = ""
            else:
                text = body.get("content") or ""
            snap.files.append(RepoFile(path=path, size=sizes.get(path, 0), text=text))

    logger.info(
        "read %s@%s: %d paths, %d tf, %d sampled%s",
        repo_full_name, snap.head_sha[:8], len(snap.all_paths), len(snap.tf_paths),
        len(snap.files), " (tree truncated)" if snap.tree_truncated else "",
    )
    return snap


def fetch_dir(token: str, repo_full_name: str, ref: str, dir_path: str, *, timeout: float = 20.0) -> list[RepoFile]:
    """Every file directly under `dir_path` (one level), with contents.

    Used to vendor a repository module into a run's workspace so
    `terraform init` can resolve a relative `source` that points at it.
    """
    _check_repo_name(repo_full_name)
    if not dir_path or ".." in dir_path or dir_path.startswith("/"):
        raise RepoReadError(f"refusing to fetch {dir_path!r}")
    out: list[RepoFile] = []
    with httpx.Client(timeout=timeout) as client:
        r = client.get(
            f"{API}/repos/{repo_full_name}/contents/{dir_path}",
            params={"ref": ref},
            headers=_headers(token),
        )
        if r.status_code == 404:
            raise RepoReadError(f"{dir_path} does not exist in {repo_full_name}@{ref}")
        if r.status_code >= 400:
            raise RepoReadError(f"could not read {dir_path}: HTTP {r.status_code}")
        entries = r.json() or []
        if isinstance(entries, dict):  # a file path, not a directory
            entries = [entries]
        for e in entries:
            if e.get("type") != "file" or not str(e.get("name", "")).endswith((".tf", ".tf.json")):
                continue
            if int(e.get("size") or 0) > MAX_FILE_BYTES:
                continue
            blob = client.get(
                f"{API}/repos/{repo_full_name}/git/blobs/{e.get('sha')}", headers=_headers(token)
            )
            if blob.status_code >= 400:
                continue
            body = blob.json() or {}
            text = ""
            if body.get("encoding") == "base64":
                try:
                    text = base64.b64decode(body.get("content") or "").decode("utf-8", "replace")
                except Exception:
                    continue
            out.append(RepoFile(path=f"{dir_path}/{e['name']}", size=int(e.get("size") or 0), text=text))
    return out


def head_sha(token: str, repo_full_name: str, base_branch: str, *, timeout: float = 10.0) -> str:
    """Current head of the base branch, or "" for an empty repository. Used as
    the cache key, so it must stay a single cheap request."""
    _check_repo_name(repo_full_name)
    with httpx.Client(timeout=timeout) as client:
        r = client.get(
            f"{API}/repos/{repo_full_name}/git/ref/heads/{base_branch}", headers=_headers(token)
        )
        if r.status_code in (404, 409):
            return ""
        if r.status_code >= 400:
            raise RepoReadError(f"could not read {repo_full_name}@{base_branch}: HTTP {r.status_code}")
        return (((r.json() or {}).get("object") or {}).get("sha")) or ""


ANALYSIS_ENABLED = os.getenv("REPO_ANALYSIS_ENABLED", "true").lower() not in ("0", "false", "no")
