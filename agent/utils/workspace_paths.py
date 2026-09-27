"""Workspace-relative path safety, plus the single copy of the file-exclusion
sets that the workspace listing, the zip download and the git commit all share.

Every path the model supplies goes through safe_join. Reads and writes use one
implementation so the two directions cannot drift apart.
"""
import os
import re

MAX_DEPTH = 6
MAX_PATH_LEN = 200

# A segment may start with a dot (.gitignore, .tflint.hcl) but must otherwise be
# alphanumerics, dots, dashes and underscores. An allowlist rather than a `..`
# denylist, so it also excludes `~`, glob characters and spaces.
_SEGMENT_RE = re.compile(r"^\.?[A-Za-z0-9][A-Za-z0-9._-]*$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

EXCLUDED_DIR_NAMES = {".terraform", ".git", "__pycache__"}
# Written by the platform before `terraform init` (tools/terraform.py): the S3
# backend in the target account. The model may not write it, and it is never
# listed, downloaded or pushed — a repository keeps its own backend.
BACKEND_FILE_NAME = "_aegis_backend.tf"

EXCLUDED_FILE_NAMES = {
    BACKEND_FILE_NAME,
    ".terraform.lock.hcl",
    "terraform.tfstate",
    "terraform.tfstate.backup",
    "crash.log",
    # A plan file can contain variable values, including secrets. It was absent
    # from every previous exclusion list and was being committed to customer
    # repositories.
    "tfplan",
    "driftplan",  # the refresh-only plan behind a drift check; same reason
}
EXCLUDED_FILE_SUFFIXES = (".tfstate", ".tfstate.backup", ".lock.info")

# What the workspace file browser and the zip treat as Terraform source. Kept
# exactly as main.py had it: widening this would change which files appear in
# the UI, which is not this change's business.
TF_FILE_EXTS = (".tf", ".tfvars", ".hcl")
WRITE_EXT_ALLOWLIST = TF_FILE_EXTS + (
    ".tf.json", ".tfvars.json", ".md", ".gitignore", ".sh", ".json",
    ".yaml", ".yml", ".txt", ".py", ".env.example", ".tflint.hcl", ".xml"
)


class WorkspacePathError(ValueError):
    """A model-supplied path that must not be read or written."""


def safe_join(workspace: str, rel_path: str, *, for_write: bool) -> str:
    """Absolute realpath inside `workspace`, or raise WorkspacePathError."""
    if not isinstance(rel_path, str) or not rel_path.strip():
        raise WorkspacePathError("no filename was given")
    raw = rel_path.strip()
    if "\x00" in raw:
        raise WorkspacePathError("the path contains a null byte")
    if len(raw) > MAX_PATH_LEN:
        raise WorkspacePathError(f"the path is longer than {MAX_PATH_LEN} characters")

    norm = raw.replace("\\", "/")
    if norm.startswith("/"):
        raise WorkspacePathError("the path must be workspace-relative, not absolute")
    # os.path.isabs("C:/x") is False on Linux, so the join would silently succeed
    # here and escape on Windows. Reject the drive letter explicitly.
    if _DRIVE_RE.match(norm):
        raise WorkspacePathError("the path must be workspace-relative, not a drive path")

    segments = norm.split("/")
    if len(segments) > MAX_DEPTH:
        raise WorkspacePathError(f"the path is deeper than {MAX_DEPTH} directories")
    for seg in segments:
        if not seg or seg in (".", ".."):
            raise WorkspacePathError("the path escapes the workspace")
        if seg != seg.rstrip(". "):
            raise WorkspacePathError(f"'{seg}' ends with a dot or space")
        if not _SEGMENT_RE.match(seg):
            raise WorkspacePathError(f"'{seg}' is not a valid file or directory name")
        if seg in EXCLUDED_DIR_NAMES:
            raise WorkspacePathError(f"'{seg}' is a reserved name")

    if for_write and segments[-1].lower() == BACKEND_FILE_NAME:
        raise WorkspacePathError(f"'{BACKEND_FILE_NAME}' is managed by the platform")

    if for_write and not norm.lower().endswith(WRITE_EXT_ALLOWLIST):
        raise WorkspacePathError(
            "only Terraform, .hcl, .md and .gitignore files may be written"
        )

    # Segment validation gives the model a self-correctable message; realpath
    # containment is what stops a symlink already inside the workspace from
    # pointing out of it.
    root = os.path.realpath(workspace)
    resolved = os.path.realpath(os.path.join(root, *segments))
    try:
        contained = os.path.commonpath([root, resolved]) == root
    except ValueError:
        contained = False  # different drives, or mixed absolute/relative
    if not contained:
        raise WorkspacePathError("the path escapes the workspace")
    return resolved
