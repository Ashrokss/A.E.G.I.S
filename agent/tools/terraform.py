import os
import subprocess
import logging
import asyncio
from typing import Dict, Any

from tools.aws_creds import aws_env_for
from utils.workspace_paths import (
    BACKEND_FILE_NAME,
    EXCLUDED_DIR_NAMES,
    EXCLUDED_FILE_NAMES,
    EXCLUDED_FILE_SUFFIXES,
    WorkspacePathError,
    safe_join,
)

logger = logging.getLogger(__name__)

# aws provider is a large binary; default plugin handshake timeout (~30s) is often too low in Docker/WSL.
_DEFAULT_PLUGIN_TIMEOUT = os.getenv("TF_PLUGIN_TIMEOUT", "120")


# Every way the AWS SDK / Terraform provider can find credentials on its own.
# Stripped so Terraform can NEVER act as this container's identity (the ECS task
# role, or a developer's profile locally) — only as the run's assumed role.
_AMBIENT_AWS_CREDENTIAL_VARS = (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_ROLE_ARN", "AWS_ROLE_SESSION_NAME",
)


def _terraform_env(run_id: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT_AWS_CREDENTIAL_VARS}
    # Close the file and metadata-service lanes too: no shared config/credentials
    # file, no IMDS. A run with no bound account fails closed with "No valid
    # credential sources", which agent.py reports as PROVISIONING_ACCOUNT_UNBOUND.
    env["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
    env["AWS_CONFIG_FILE"] = os.devnull
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env.setdefault("TF_PLUGIN_TIMEOUT", _DEFAULT_PLUGIN_TIMEOUT)
    env.setdefault("TF_IN_AUTOMATION", "1")
    # The run's own temporary STS creds for the account it is bound to.
    env.update(aws_env_for(run_id))
    return env


class StateBackendUnavailable(Exception):
    """The run has no state bucket, i.e. no bound account. Never falls back to local state."""


_BACKEND_TF = 'terraform {\n  backend "s3" {}\n}\n'


def state_key(run_id: str) -> str:
    return f"runs/{run_id}/terraform.tfstate"


def backend_config_args(run_id: str) -> list[str]:
    """`terraform init` flags for the run's S3 backend in the target account.

    The bucket arrives with the run's STS creds (api provisioning-creds), and
    Terraform reaches it with those same creds, so state lives in the client's
    account and is written only by the assumed AegisProvisioner role.
    """
    env = aws_env_for(run_id)
    bucket = env.get("AEGIS_STATE_BUCKET")
    region = env.get("AEGIS_STATE_REGION") or env.get("AWS_REGION")
    if not bucket or not region:
        raise StateBackendUnavailable(run_id)
    return [
        # The settings are the same on every init; -reconfigure just never asks
        # to migrate, which -input=false would turn into a failure.
        "-reconfigure",
        f"-backend-config=bucket={bucket}",
        f"-backend-config=key={state_key(run_id)}",
        f"-backend-config=region={region}",
        "-backend-config=use_lockfile=true",
        "-backend-config=encrypt=true",
    ]


# Saved refresh-only plan. Holds a state snapshot, so like tfplan it never
# leaves the platform (EXCLUDED_FILE_NAMES).
DRIFT_PLAN = "driftplan"


def _count_managed(module: dict) -> int:
    n = sum(1 for r in (module.get("resources") or []) if r.get("mode") == "managed")
    return n + sum(_count_managed(c) for c in (module.get("child_modules") or []))


def parse_drift(show_json: dict) -> dict:
    """`terraform show -json <refresh-only plan>` -> {tracked, drifted[]}.

    `resource_drift` lists what changed in the account since the last apply,
    outside Terraform: an edited attribute is "changed", a resource that no
    longer exists is "deleted".
    """
    drifted = []
    for rc in show_json.get("resource_drift") or []:
        change = rc.get("change") or {}
        actions = set(change.get("actions") or [])
        if not actions or actions <= {"no-op", "read"}:
            continue
        before, after = change.get("before") or {}, change.get("after") or {}
        if "delete" in actions:
            kind, attributes = "deleted", []
        else:
            kind = "changed"
            attributes = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))[:8]
        drifted.append({"address": rc.get("address"), "kind": kind, "attributes": attributes})
    prior = ((show_json.get("prior_state") or {}).get("values") or {}).get("root_module") or {}
    return {"tracked": _count_managed(prior), "drifted": drifted}


class TerraformTool:
    def __init__(self, base_workspace_dir: str = os.getenv("AGENT_WORKSPACE_DIR", "data/runs")):
        self.base_workspace_dir = base_workspace_dir
        self.active_processes: dict[str, asyncio.subprocess.Process] = {}
        # run_id -> workspace-relative directory holding the root module. Empty
        # means the workspace root, which is the behaviour for every run that
        # does not mirror a repo's nested layout.
        self.run_module_dirs: dict[str, str] = {}

    def _get_workspace_path(self, run_id: str) -> str:
        path = os.path.join(self.base_workspace_dir, run_id, "workspace")
        os.makedirs(path, exist_ok=True)
        return path

    def module_dir_for(self, run_id: str) -> str:
        return self.run_module_dirs.get(run_id, "")

    def set_run_module_dir(self, run_id: str, rel_dir: str) -> None:
        """Point terraform at a subdirectory of this run's workspace.

        Raises WorkspacePathError for an unsafe value, leaving any previous
        setting untouched — the caller decides whether that is fatal.
        """
        if not rel_dir or not str(rel_dir).strip():
            self.run_module_dirs.pop(run_id, None)
            return
        workspace = self._get_workspace_path(run_id)
        # Validated exactly like a file path (no extension rule for a directory),
        # so `..`, absolute paths and reserved names are refused here too.
        safe_join(workspace, rel_dir, for_write=False)
        self.run_module_dirs[run_id] = str(rel_dir).strip().replace("\\", "/").strip("/")

    def _module_path(self, run_id: str) -> str:
        """Where terraform actually runs: the workspace root, or the run's root
        module directory beneath it."""
        workspace = self._get_workspace_path(run_id)
        rel = self.module_dir_for(run_id)
        if not rel:
            return workspace
        path = os.path.join(workspace, *rel.split("/"))
        os.makedirs(path, exist_ok=True)
        return path

    def write_tf_file(self, run_id: str, filename: str, content: str) -> str:
        workspace = self._get_workspace_path(run_id)
        try:
            file_path = safe_join(workspace, filename, for_write=True)
        except WorkspacePathError as e:
            logger.warning("[%s] refused write to %r: %s", run_id, filename, e)
            return (
                f"Error: refused to write '{filename}': {e}. Use a workspace-relative "
                "path such as 'main.tf' or 'modules/vpc/main.tf'."
            )
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Explicit utf-8 + LF: the bare open(..., "w") this replaces used the
        # locale encoding and wrote CRLF on a Windows host, which `terraform fmt`
        # then rewrote on the next run.
        with open(file_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return f"File {filename} written to workspace."

    def create_folder(self, run_id: str, path: str) -> str:
        workspace = self._get_workspace_path(run_id)
        try:
            dir_path = safe_join(workspace, path, for_write=False)
        except WorkspacePathError as e:
            logger.warning("[%s] refused create_folder of %r: %s", run_id, path, e)
            return f"Error: refused to create folder '{path}': {e}."
        os.makedirs(dir_path, exist_ok=True)
        return f"Folder '{path}' created in workspace."

    def read_tf_file(self, run_id: str, filename: str) -> str:
        workspace = self._get_workspace_path(run_id)
        try:
            file_path = safe_join(workspace, filename, for_write=False)
        except WorkspacePathError as e:
            logger.warning("[%s] refused read of %r: %s", run_id, filename, e)
            return (
                f"Error: refused to read '{filename}': {e}. Use a workspace-relative "
                "path such as 'main.tf' or 'modules/vpc/main.tf'."
            )
        if not os.path.exists(file_path):
            return f"Error: File {filename} does not exist."
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()

    def list_workspace_files(self, run_id: str, *, recursive: bool = True, limit: int = 300) -> list:
        """Workspace files as sorted POSIX-relative paths.

        Excludes the provider cache and state: a naive walk would enumerate
        thousands of .terraform/providers/... entries straight into a tool
        result and the model's context. Capped for the same reason.
        """
        workspace = self._get_workspace_path(run_id)
        if not recursive:
            return sorted(os.listdir(workspace))
        found: list[str] = []
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
            for file_name in files:
                if file_name in EXCLUDED_FILE_NAMES:
                    continue
                if file_name.endswith(EXCLUDED_FILE_SUFFIXES):
                    continue
                rel = os.path.relpath(os.path.join(root, file_name), workspace)
                found.append(rel.replace(os.sep, "/"))
        found.sort()
        if len(found) > limit:
            return found[:limit] + [f"… {len(found) - limit} more"]
        return found

    def root_module_tf_files(self, run_id: str) -> list:
        """Top-level *.tf only — the files `terraform plan` will actually read.

        Distinct from list_workspace_files so the pre-plan guard keeps its
        meaning once nested layouts exist: a workspace holding only
        modules/vpc/main.tf has no root module and must not be planned.
        """
        # The run's module directory, not the workspace root: with a nested
        # layout the root module lives at e.g. environments/dev.
        module_path = self._module_path(run_id)
        return sorted(
            name for name in os.listdir(module_path)
            if name.endswith((".tf", ".tf.json"))
            and os.path.isfile(os.path.join(module_path, name))
        )

    async def run_command_stream(self, run_id: str, command: list, *, at_root: bool = False):
        # Terraform runs in the run's module directory (the workspace root unless
        # a nested layout is mirrored). `tfplan` is always referenced relative to
        # cwd, so it lands and is read in the same place with no path changes.
        workspace = self._get_workspace_path(run_id) if at_root else self._module_path(run_id)
        queue = asyncio.Queue()

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_terraform_env(run_id),
            )
            self.active_processes[run_id] = process

            async def read_stream(stream, prefix):
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    await queue.put(f"{prefix}: {line.decode().strip()}")

            # Start reading both streams in background
            stdout_task = asyncio.create_task(read_stream(process.stdout, "STDOUT"))
            stderr_task = asyncio.create_task(read_stream(process.stderr, "STDERR"))

            # Yield logs as they arrive in the queue
            while not (stdout_task.done() and stderr_task.done() and queue.empty()):
                try:
                    # Wait for a log with a short timeout to check task status
                    log = await asyncio.wait_for(queue.get(), timeout=0.1)
                    yield log
                except asyncio.TimeoutError:
                    # Keep loop responsive for cancellation
                    if process.returncode is not None:
                        break
                    continue

            return_code = await process.wait()
            yield f"EXIT: {return_code}"
        except Exception as e:
            yield f"ERROR: {str(e)}"
        finally:
            self.active_processes.pop(run_id, None)

    async def stop_run_process(self, run_id: str):
        process = self.active_processes.get(run_id)
        if not process or process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except Exception:
            try:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except Exception:
                pass
        finally:
            self.active_processes.pop(run_id, None)

    def run_command(self, run_id: str, command: list, *, at_root: bool = False) -> Dict[str, Any]:
        workspace = self._get_workspace_path(run_id) if at_root else self._module_path(run_id)
        try:
            result = subprocess.run(
                command,
                cwd=workspace,
                env=_terraform_env(run_id),
                capture_output=True,
                text=True,
                check=False
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode
            }
        except Exception as e:
            return {"error": str(e)}

    def format_workspace(self, run_id: str) -> Dict[str, Any]:
        """
        Format Terraform files in the workspace.
        This keeps generated .tf/.tfvars files clean and consistent.

        Deliberately at the workspace root: -recursive must also reach a
        modules/ tree sitting outside the root module directory.
        """
        return self.run_command(run_id, ["terraform", "fmt", "-recursive"], at_root=True)

    def init_command(self, run_id: str) -> list[str]:
        """Write the platform's backend file into the root module and return the
        init command. Raises StateBackendUnavailable when the run has no bucket."""
        args = backend_config_args(run_id)
        with open(os.path.join(self._module_path(run_id), BACKEND_FILE_NAME), "w", encoding="utf-8", newline="\n") as f:
            f.write(_BACKEND_TF)
        return ["terraform", "init", "-no-color", "-input=false", *args]

    def init(self, run_id: str) -> Dict[str, Any]:
        return self.run_command(run_id, self.init_command(run_id))

    def validate(self, run_id: str) -> Dict[str, Any]:
        """Syntax and provider schema check; run after init and before plan."""
        return self.run_command(run_id, ["terraform", "validate", "-no-color"])

    def plan(self, run_id: str) -> Dict[str, Any]:
        return self.run_command(run_id, ["terraform", "plan", "-no-color", "-out=tfplan"])

    def apply(self, run_id: str) -> Dict[str, Any]:
        return self.run_command(run_id, ["terraform", "apply", "-no-color", "-auto-approve", "tfplan"])

    def get_plan_json(self, run_id: str) -> Dict[str, Any]:
        # Optional: convert plan to JSON for better UI rendering
        return self.run_command(run_id, ["terraform", "show", "-json", "tfplan"])
