"""Terraform's working directory is per-run, so a workspace can mirror a repo's
nested layout. Default is the workspace root — identical to previous behaviour.
Run: python test_module_dir.py"""
import os
import tempfile

from tools.terraform import TerraformTool
from utils.workspace_paths import WorkspacePathError

TMP = tempfile.mkdtemp()
tool = TerraformTool(base_workspace_dir=os.path.join(TMP, "runs"))
RUN = "run-mod"
WS = os.path.realpath(tool._get_workspace_path(RUN))


def cwd_of(**kw):
    return tool.run_command(RUN, ["pwd"], **kw)["stdout"].strip()


# Default: commands run at the workspace root, exactly as before this change.
assert cwd_of() == WS, (cwd_of(), WS)
assert tool.module_dir_for(RUN) == "", tool.module_dir_for(RUN)

# With a module dir set, terraform runs there and the directory is created.
tool.set_run_module_dir(RUN, "environments/dev")
assert tool.module_dir_for(RUN) == "environments/dev"
expected = os.path.realpath(os.path.join(WS, "environments", "dev"))
assert cwd_of() == expected, (cwd_of(), expected)

# `terraform fmt -recursive` must stay at the workspace root, or it would not
# reach modules/ sitting outside the root module.
assert cwd_of(at_root=True) == WS, cwd_of(at_root=True)

# Writes stay workspace-root-relative whatever the module dir is: the model
# writes the mirrored tree, it does not write "into" the module dir implicitly.
tool.write_tf_file(RUN, "environments/dev/main.tf", 'resource "aws_s3_bucket" "b" {}\n')
tool.write_tf_file(RUN, "modules/vpc/main.tf", 'variable "cidr" {}\n')
listed = tool.list_workspace_files(RUN)
assert "environments/dev/main.tf" in listed and "modules/vpc/main.tf" in listed, listed

# The pre-plan guard follows the module dir: these are the files `plan` reads.
assert tool.root_module_tf_files(RUN) == ["main.tf"], tool.root_module_tf_files(RUN)

# A module dir is a path like any other and gets the same validation.
for bad in ["../../etc", "/etc", "a/../../b", ".git", "C:/Windows"]:
    try:
        tool.set_run_module_dir(RUN, bad)
    except WorkspacePathError:
        pass
    else:
        raise AssertionError(f"accepted unsafe module dir {bad!r}")
# A refused value must not have disturbed the one already set.
assert tool.module_dir_for(RUN) == "environments/dev", tool.module_dir_for(RUN)

# Clearing returns to the workspace root.
tool.set_run_module_dir(RUN, "")
assert tool.module_dir_for(RUN) == ""
assert cwd_of() == WS, cwd_of()
assert tool.root_module_tf_files(RUN) == [], tool.root_module_tf_files(RUN)

# An unrelated run is unaffected — the setting is per-run, not global.
assert tool.module_dir_for("some-other-run") == ""

# --- persistence: a restarted agent recovers the directory from the run row ---
import agent as agent_mod

a = agent_mod.ProvisioningAgent()
agent_mod.TF_TOOL = tool          # exercise the same temp workspace
a_run = "run-persist"
stored: dict[str, object] = {}
a._set_run_field = lambda run_id, col, val: stored.__setitem__(col, val)
a._get_run = lambda run_id: {"github_target_subdir": stored.get("github_target_subdir")}

assert a.set_run_module_dir(a_run, "environments/prod") is True
assert stored["github_target_subdir"] == "environments/prod", stored
assert tool.module_dir_for(a_run) == "environments/prod"

# An unsafe value is refused and reported, not persisted, and the run falls back
# to the workspace root rather than failing.
assert a.set_run_module_dir(a_run, "../../etc") is False
assert stored["github_target_subdir"] == "environments/prod", stored

# Simulate a restart: in-memory state gone, the row still has the directory.
tool.run_module_dirs.clear()
a._module_dir_restored.clear()
a._restore_run_module_dir(a_run)
assert tool.module_dir_for(a_run) == "environments/prod", tool.module_dir_for(a_run)

# Restoring is once per run, and a run with no stored value stays at the root.
stored.clear()
tool.run_module_dirs.clear()
a._module_dir_restored.clear()
a._restore_run_module_dir(a_run)
assert tool.module_dir_for(a_run) == "", tool.module_dir_for(a_run)

print("ok: terraform's working directory is per-run, defaulting to the workspace root")
