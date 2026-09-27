"""Nothing outside a run's own workspace is readable or writable through the
agent's tools. Run: python test_workspace_paths.py"""
import os
import tempfile

from utils.workspace_paths import (
    EXCLUDED_FILE_NAMES,
    MAX_PATH_LEN,
    WorkspacePathError,
    safe_join,
)

TMP = tempfile.mkdtemp()
WS = os.path.join(TMP, "workspace")
os.makedirs(WS, exist_ok=True)

# Refused for both reads and writes.
REJECT = [
    "../../../.env",              # the deployment's AWS keys and GitHub PAT
    "../../runs.db",
    "/etc/passwd",
    "C:/Windows/System32/x.tf",   # isabs() is False for this on Linux
    "a/../../b.tf",
    ".git/config",
    ".terraform/providers/x.tf",
    "a/b/c/d/e/f/g.tf",           # deeper than MAX_DEPTH
    "main.tf\x00.txt",
    "~/x.tf",
    "evil.tf.",                   # Windows strips the trailing dot
    "sub dir/main.tf",            # spaces
    "",
    "   ",
    "modules/",                   # trailing slash -> empty segment
    "x" * (MAX_PATH_LEN + 10),
]
for p in REJECT:
    for for_write in (True, False):
        try:
            safe_join(WS, p, for_write=for_write)
        except WorkspacePathError:
            pass
        else:
            raise AssertionError(f"accepted {p!r} (for_write={for_write})")

# Accepted for write, and always resolves inside the workspace.
ROOT = os.path.realpath(WS)
for p in [
    "main.tf", "providers.tf", "terraform.tfvars",
    "modules/vpc/main.tf", "environments/dev/providers.tf",
    "README.md", ".gitignore", "notes.txt", "run.sh", "config.json", "script.py", "deploy.yaml"
]:
    got = safe_join(WS, p, for_write=True)
    assert got.startswith(ROOT + os.sep), (p, got)

# Writes to executable/dangerous extensions like .exe or .dll are refused for write; reads are not.
for p in ["binary.exe", "library.dll", "kernel.so"]:
    try:
        safe_join(WS, p, for_write=True)
    except WorkspacePathError:
        pass
    else:
        raise AssertionError(f"write accepted {p!r}")
    safe_join(WS, p, for_write=False)

# tfplan is excluded from commits and listings (a plan file can carry secrets).
assert "tfplan" in EXCLUDED_FILE_NAMES

from tools.terraform import TerraformTool

tool = TerraformTool(base_workspace_dir=os.path.join(TMP, "runs"))
RUN = "run-aaaa"

# A nested write creates its parent directories and round-trips.
msg = tool.write_tf_file(RUN, "modules/vpc/main.tf", 'resource "aws_vpc" "main" {}\n')
assert "written" in msg, msg
assert tool.read_tf_file(RUN, "modules/vpc/main.tf").startswith("resource "), "round trip failed"

# Explicit folder creation works.
folder_msg = tool.create_folder(RUN, "scripts")
assert "created" in folder_msg, folder_msg
assert os.path.isdir(os.path.join(tool._get_workspace_path(RUN), "scripts")), "folder creation failed"

# Traversal is refused as a message, not an exception, so the model self-corrects.
out = tool.read_tf_file(RUN, "../../../.env")
assert out.startswith("Error:") and "escapes the workspace" in out, out
wrote = tool.write_tf_file(RUN, "../../../evil.tf", "x")
assert wrote.startswith("Error:"), wrote
assert not os.path.exists(os.path.join(TMP, "evil.tf")), "wrote outside the workspace"

# A missing file still reports the pre-existing message, not a path error.
missing = tool.read_tf_file(RUN, "nope.tf")
assert "does not exist" in missing, missing

# Content is written UTF-8 with LF endings regardless of host platform.
tool.write_tf_file(RUN, "crlf.tf", "a\nb\n")
with open(os.path.join(tool._get_workspace_path(RUN), "crlf.tf"), "rb") as fh:
    assert fh.read() == b"a\nb\n", "line endings were translated"

from tools.github_push import _committable_files

ws = tool._get_workspace_path(RUN)
open(os.path.join(ws, "tfplan"), "w").close()
open(os.path.join(ws, "terraform.tfstate"), "w").close()
open(os.path.join(ws, "main.tf"), "w").close()

committable = _committable_files(ws)
assert "main.tf" in committable, committable
assert "modules/vpc/main.tf" in committable, committable
assert "tfplan" not in committable, "tfplan would be pushed to the customer's repo"
assert "terraform.tfstate" not in committable, committable

# A recursive listing must not drag the provider cache into the model's context.
os.makedirs(os.path.join(ws, ".terraform", "providers"), exist_ok=True)
open(os.path.join(ws, ".terraform", "providers", "aws.bin"), "w").close()

listed = tool.list_workspace_files(RUN)
assert "main.tf" in listed and "modules/vpc/main.tf" in listed, listed
assert "tfplan" not in listed and "terraform.tfstate" not in listed, listed
assert not any(p.startswith(".terraform/") for p in listed), listed
assert listed == sorted(listed), listed
assert all("\\" not in p for p in listed), listed

# The pre-plan guard must see only the root module, or a nested-only workspace
# passes the guard and then fails `plan` with "No configuration files".
assert tool.root_module_tf_files(RUN) == ["crlf.tf", "main.tf"], tool.root_module_tf_files(RUN)

# Listing is capped so a large workspace cannot flood a tool result.
many = os.path.join(ws, "many")
os.makedirs(many, exist_ok=True)
for i in range(310):
    open(os.path.join(many, f"f{i:04d}.tf"), "w").close()
capped = tool.list_workspace_files(RUN, limit=300)
assert len(capped) == 301, len(capped)
assert capped[-1].startswith("…") and "more" in capped[-1], capped[-1]

print("ok: workspace paths are contained")
