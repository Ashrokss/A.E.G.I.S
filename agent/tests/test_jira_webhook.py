"""Signature gate on the JIRA webhook. Run: python test_jira_webhook.py"""
import hashlib
import hmac
import json
import os

os.environ["JIRA_WEBHOOK_SECRET"] = "test-secret"
os.environ["JIRA_AUTO_LABEL"] = "aegis-auto"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

client = TestClient(main.app)
body = json.dumps({"webhookEvent": "jira:issue_created", "issue": {"key": "SAM1-1", "fields": {}}}).encode()


def post(sig: str | None):
    headers = {"content-type": "application/json"}
    if sig is not None:
        headers["X-Hub-Signature"] = sig
    return client.post("/api/jira/webhook", content=body, headers=headers)


good = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()

assert post(None).status_code == 401, "missing signature must be rejected"
assert post("sha256=deadbeef").status_code == 401, "wrong signature must be rejected"
assert post(good.replace("sha256=", "")).status_code == 200, "bare hex signature should pass"

resp = post(good)
assert resp.status_code == 200, resp.text
# No aegis-auto label on the payload -> no run started.
assert resp.json().get("skipped", "").startswith("missing label"), resp.json()

print("ok: signature gate + label filter")
