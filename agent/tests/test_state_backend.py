"""Terraform state lives in the target account's S3 bucket, never on local disk.
Run: python -m tests.test_state_backend"""
import os
import tempfile

from tools import aws_creds
from tools.terraform import StateBackendUnavailable, TerraformTool, backend_config_args, state_key
from tools.github_push import _committable_files, state_handoff
from utils.workspace_paths import BACKEND_FILE_NAME, WorkspacePathError, safe_join

RUN = "run-1234"
tf = TerraformTool(base_workspace_dir=tempfile.mkdtemp())

# No bound account -> no bucket -> refuse. Local state is never a fallback.
try:
    tf.init_command(RUN)
    raise AssertionError("init without a state bucket must be refused")
except StateBackendUnavailable:
    pass
assert state_handoff(RUN) == ""

aws_creds._run_aws[RUN] = {
    "AWS_ACCESS_KEY_ID": "ASIAEXAMPLE",
    "AWS_SECRET_ACCESS_KEY": "x",
    "AWS_SESSION_TOKEN": "y",
    "AWS_REGION": "us-east-1",
    "AEGIS_STATE_BUCKET": "aegis-tfstate-123456789012-default",
    "AEGIS_STATE_REGION": "us-east-1",
}

# Every run has its own key in that bucket, locked with the S3 lockfile.
cmd = tf.init_command(RUN)
assert cmd[:4] == ["terraform", "init", "-no-color", "-input=false"], cmd
assert "-backend-config=bucket=aegis-tfstate-123456789012-default" in cmd
assert f"-backend-config=key=runs/{RUN}/terraform.tfstate" in cmd
assert "-backend-config=region=us-east-1" in cmd
assert "-backend-config=use_lockfile=true" in cmd
assert "-reconfigure" in cmd
assert state_key("other") != state_key(RUN)

# The platform writes the backend block into the root module...
ws = tf._get_workspace_path(RUN)
with open(os.path.join(ws, BACKEND_FILE_NAME), encoding="utf-8") as f:
    assert f.read() == 'terraform {\n  backend "s3" {}\n}\n'

# ...and it follows the root module when the run mirrors a nested repo layout.
tf.set_run_module_dir(RUN, "envs/prod")
tf.init_command(RUN)
assert os.path.exists(os.path.join(ws, "envs", "prod", BACKEND_FILE_NAME))

# The model can never write (or overwrite) that file, at any depth or case.
for path in (BACKEND_FILE_NAME, f"envs/prod/{BACKEND_FILE_NAME}", BACKEND_FILE_NAME.upper()):
    try:
        safe_join(ws, path, for_write=True)
        raise AssertionError(f"{path} must not be writable by the model")
    except WorkspacePathError:
        pass
assert tf.write_tf_file(RUN, BACKEND_FILE_NAME, "terraform {}").startswith("Error: refused")

# It never leaves the platform: not listed, not downloaded, not pushed. A
# repository keeps its own backend (two would fail with "Duplicate backend").
with open(os.path.join(ws, "main.tf"), "w", encoding="utf-8") as f:
    f.write("# x\n")
listed = [str(f) for f in tf.list_workspace_files(RUN)]
assert "main.tf" in listed and not any(n.endswith(BACKEND_FILE_NAME) for n in listed), listed
pushed = _committable_files(ws)
assert any(p.endswith("main.tf") for p in pushed) and not any(p.endswith(BACKEND_FILE_NAME) for p in pushed), pushed

# The PR says where the state is, so nobody applies the merge from empty state.
handoff = state_handoff(RUN)
assert f"s3://aegis-tfstate-123456789012-default/runs/{RUN}/terraform.tfstate" in handoff
assert 'backend "s3"' in handoff and "use_lockfile = true" in handoff
assert "ASIAEXAMPLE" not in handoff, "credentials must never reach a PR body"

# The backend args depend only on the bucket; the STS creds reach Terraform via env.
assert not any("ASIAEXAMPLE" in a for a in backend_config_args(RUN))

print("✅ test_state_backend passed")
