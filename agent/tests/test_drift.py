"""Drift detection: `terraform plan -refresh-only` against the run's state, read-only.
Terraform itself is stubbed; the parser, the report and the status handling are real.
Run: python -m tests.test_drift"""
import asyncio
import json
import os
import sqlite3
import tempfile

os.environ["AGENT_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test-runs.db")

import agent as agent_mod  # noqa: E402
import main  # noqa: E402
from tools import aws_creds  # noqa: E402
from tools.terraform import parse_drift  # noqa: E402

# ── The parser ────────────────────────────────────────────────────────────
SHOW = {
    "prior_state": {"values": {"root_module": {
        "resources": [
            {"mode": "managed", "address": "aws_s3_bucket.a"},
            {"mode": "data", "address": "data.aws_caller_identity.me"},
        ],
        "child_modules": [{"resources": [{"mode": "managed", "address": "module.q.aws_sqs_queue.q"}]}],
    }}},
    "resource_drift": [
        {"address": "aws_s3_bucket.a", "change": {"actions": ["update"],
         "before": {"tags": {"Owner": "infra"}, "bucket": "a"}, "after": {"tags": {}, "bucket": "a"}}},
        {"address": "module.q.aws_sqs_queue.q", "change": {"actions": ["delete"], "before": {"name": "q"}, "after": None}},
        {"address": "aws_s3_bucket.noise", "change": {"actions": ["no-op"]}},
    ],
}
got = parse_drift(SHOW)
assert got["tracked"] == 2, got  # managed resources only, child modules included
assert got["drifted"] == [
    {"address": "aws_s3_bucket.a", "kind": "changed", "attributes": ["tags"]},
    {"address": "module.q.aws_sqs_queue.q", "kind": "deleted", "attributes": []},
], got["drifted"]
assert parse_drift({}) == {"tracked": 0, "drifted": []}

# ── check_drift end to end, with Terraform stubbed ────────────────────────
main.init_db()
RUN = "run-drift"
conn = sqlite3.connect(os.environ["AGENT_DB_PATH"])
conn.execute("INSERT INTO runs (id, status, created_at, updated_at, goal) VALUES (?, 'COMPLETED', '', '', 'g')", (RUN,))
conn.commit()
conn.close()

tf = agent_mod.TF_TOOL
tf.base_workspace_dir = tempfile.mkdtemp()
agent = main.agent
commands = []


async def fake_stream(run_id, command, at_root=False):
    commands.append(command)
    yield "STDOUT: ok"
    yield "EXIT: 0"


tf.run_command_stream = fake_stream
tf.run_command = lambda run_id, command, at_root=False: {"stdout": json.dumps(SHOW), "exit_code": 0}


def last(msg_type):
    conn = sqlite3.connect(os.environ["AGENT_DB_PATH"])
    row = conn.execute("SELECT content FROM messages WHERE run_id = ? AND type = ? ORDER BY id DESC LIMIT 1", (RUN, msg_type)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


# No bound account: refused, never run against local state or AEGIS's identity.
asyncio.run(agent.check_drift(RUN))
report = last("drift_report")
assert report["ok"] is False and report["error"].startswith("PROVISIONING_ACCOUNT_UNBOUND"), report
assert commands == [], "terraform must not run without the run's account"
assert agent._get_run(RUN)["status"] == "COMPLETED", "status is restored"

aws_creds._run_aws[RUN] = {
    "AWS_ACCESS_KEY_ID": "ASIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "x", "AWS_SESSION_TOKEN": "y",
    "AWS_REGION": "us-east-1", "AEGIS_STATE_BUCKET": "aegis-tfstate-123456789012-default",
    "AEGIS_STATE_REGION": "us-east-1",
}
asyncio.run(agent.check_drift(RUN))
report = last("drift_report")
assert report["ok"] is True and report["tracked"] == 2 and len(report["drifted"]) == 2, report
assert report["checked_at"]
init_cmd, plan_cmd = commands
assert init_cmd[:2] == ["terraform", "init"] and any(a.startswith("-backend-config=key=runs/run-drift/") for a in init_cmd)
# Read-only: a refresh-only plan to its own file, never an apply, never tfplan.
assert plan_cmd[:3] == ["terraform", "plan", "-refresh-only"] and "-out=driftplan" in plan_cmd, plan_cmd
assert not any("apply" in c for cmd in commands for c in cmd)
assert agent._get_run(RUN)["status"] == "COMPLETED"


# A failed refresh is reported, and a credential failure says so.
async def failing_stream(run_id, command, at_root=False):
    yield "STDERR: Error: No valid credential sources found"
    yield "EXIT: 1"

tf.run_command_stream = failing_stream
asyncio.run(agent.check_drift(RUN))
report = last("drift_report")
assert report["ok"] is False and "PROVISIONING_ACCOUNT_UNBOUND" in report["error"], report
assert agent._get_run(RUN)["status"] == "COMPLETED"

# The endpoints exist and refuse while the agent is working.
assert {"/api/runs/{run_id}/drift-check", "/api/runs/{run_id}/drift-fix"} <= {r.path for r in main.app.routes}
assert "CHECKING_DRIFT" in main._BUSY_STATUSES

print("✅ test_drift passed")
