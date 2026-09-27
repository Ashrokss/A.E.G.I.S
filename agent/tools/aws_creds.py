"""
Per-run AWS credentials for provisioning.

A run is bound to one of its owner's onboarded AWS accounts. The api assumes
that account's AegisProvisioner role (ownership-checked, ExternalId from its
own store) and returns short-lived STS credentials, kept here per run_id and
injected into that run's Terraform subprocess (tools/terraform.py), which has
every ambient credential source stripped. There is no fallback identity.

STS credentials expire (~1h), so main.py re-resolves them from the run's
persisted aws_account_id before every approve and follow-up message.
"""
import os
import logging

import httpx

logger = logging.getLogger("uvicorn.error")

BACKEND_URL = os.getenv("BACKEND_INTERNAL_URL", "http://aws-backend:3005").rstrip("/")

# run_id -> {AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN, AWS_REGION}
_run_aws: dict[str, dict[str, str]] = {}
# run_id -> aws_account_id (remembered so apply-time can re-fetch fresh creds)
_run_account: dict[str, str] = {}


class AwsAccountForbidden(Exception):
    """The caller may not have credentials for the account they asked for.

    Distinct from "no credentials available": this is an authorization answer.
    Credentials are issued only for the caller's OWN onboarded accounts, and a
    refusal is never turned into some other identity.
    """


async def _request_aws_creds(authorization: str | None, account_id: str | None):
    """(credentials | None, http status | None). Status is None when no request
    was made or the backend could not be reached."""
    if not authorization or not account_id:
        return None, None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{BACKEND_URL}/api/aws-accounts/internal/provisioning-creds",
                params={"accountId": account_id},
                headers={"Authorization": authorization},
            )
    except Exception as ex:  # backend unreachable / not configured
        logger.info(f"[aws_creds] could not fetch per-account creds: {ex}")
        return None, None

    if resp.status_code == 200:
        data = resp.json()
        if data.get("AWS_SECRET_ACCESS_KEY"):
            return {k: str(v) for k, v in data.items() if v}, 200
        return None, 200

    logger.info(f"[aws_creds] backend returned {resp.status_code} for provisioning-creds")
    return None, resp.status_code


async def fetch_aws_creds(authorization: str | None, account_id: str | None) -> dict[str, str] | None:
    """Resolve the named account's temporary AWS creds, or None.

    LENIENT: any failure, including a refusal, is None so the caller can fall
    back. There is no fallback identity for Terraform either way.
    Read paths want this. A path that would otherwise act on the WRONG account
    wants require_aws_creds below instead.
    """
    creds, _status = await _request_aws_creds(authorization, account_id)
    return creds


async def require_aws_creds(authorization: str | None, account_id: str | None) -> dict[str, str] | None:
    """Same, but a REFUSAL RAISES instead of silently returning None.

    Used where the credentials decide which AWS account the work lands in.
    Falling back there would run the operator's provisioning against this
    deployment's account while the UI showed the one they chose.

    Returns None when no account was named or the role could not be assumed;
    main._bind_run turns that into PROVISIONING_ACCOUNT_UNKNOWN.
    """
    creds, status = await _request_aws_creds(authorization, account_id)
    if status in (401, 403):
        raise AwsAccountForbidden(
            f"You are not permitted to use AWS account {account_id}: it is not one of "
            "your onboarded accounts."
        )
    return creds


def set_run_aws(run_id: str, creds: dict[str, str] | None, account_id: str | None = None) -> None:
    if creds:
        _run_aws[run_id] = creds
    if account_id:
        _run_account[run_id] = account_id


def clear_run_aws(run_id: str) -> None:
    _run_aws.pop(run_id, None)
    _run_account.pop(run_id, None)


def aws_env_for(run_id: str | None) -> dict[str, str]:
    """Per-run AWS_* overrides (empty -> Terraform uses the agent's own env)."""
    if not run_id:
        return {}
    return _run_aws.get(run_id, {})
