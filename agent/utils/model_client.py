"""
Amazon Bedrock plumbing, shared by the agents.

Model routing and retry classification live here so the agents don't each carry
a subtly different copy. Credentials are ambient — the Fargate task role, or the
local default chain — so there is no API key to configure or leak, and
`available` now turns on whether a model id is set rather than on a secret.

ProvisioningAgent is deliberately NOT refactored onto this yet: collapsing its
six methods into delegations would touch the hot path of a working 2300-line
agent in the middle of a migration. That collapse is the next commit, once this
module is proven.
"""
import asyncio
import logging
import os

import boto3
from botocore.config import Config

from utils.bedrock import from_converse, to_converse, tools_to_toolconfig

logger = logging.getLogger("uvicorn.error")

_RATE_LIMIT_SIGNALS = (
    "rate_limit_exceeded", "rate limit", "too many requests", "429",
    "throttlingexception", "toomanyrequestsexception", "servicequotaexceededexception",
)

_TRANSIENT_SIGNALS = (
    "remotedisconnected", "remote end closed connection", "connection aborted",
    "connection reset", "server disconnected", "temporarily unavailable",
    "temporary failure", "timed out", "timeout", "502", "503", "504",
    "429", "rate_limit_exceeded", "rate limit", "too many requests",
    # TLS/socket drops mid-request — without these a single dropped connection
    # ends the turn instead of retrying.
    "unexpected_eof", "eof occurred in violation of protocol", "broken pipe",
    "incompleteread", "connection error",
    # Bedrock/botocore equivalents. A throttle is transient AND a rate limit —
    # it must appear in both tuples, or it is classified as a rate limit and
    # then never retried.
    "throttlingexception", "toomanyrequestsexception", "servicequotaexceededexception",
    "modeltimeoutexception", "serviceunavailableexception", "internalserverexception",
    "modelnotreadyexception", "endpointconnectionerror", "connecttimeouterror",
    "readtimeouterror",
)

# Checked BEFORE the transient list. A ValidationException echoes the offending
# input back in its message, and that input routinely contains "timeout" or
# "503" — so without a fatal-first check a wrong model id gets retried nine
# times per turn and presents as "the agent has hung" rather than as a config
# error. Same for a missing IAM grant.
_FATAL_SIGNALS = (
    "validationexception", "accessdeniedexception", "resourcenotfoundexception",
    "unrecognizedclientexception", "expiredtokenexception", "serializationexception",
)


class ModelClient:
    """Bedrock Converse, with model routing and retry classification.

    `available` is False when no model id is configured, so callers degrade
    rather than crash — the same "mock mode" the provisioning agent has. Note
    there are deliberately NO string defaults for the model ids: an unset model
    must mean mock mode, because a stale `gpt-4o` would be a valid-looking
    string that ValidationExceptions on every single request instead.
    """

    def __init__(self):
        self.region = (os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "").strip()
        self.model = os.getenv("BEDROCK_MODEL", "").strip()
        self.model_chat = os.getenv("BEDROCK_MODEL_CHAT", "").strip() or self.model
        self.model_codegen = os.getenv("BEDROCK_MODEL_CODEGEN", "").strip() or self.model
        self.model_heavy = os.getenv("BEDROCK_MODEL_HEAVY", "").strip() or self.model_codegen
        self.request_timeout = float(os.getenv("MODEL_REQUEST_TIMEOUT_SECONDS", "180"))
        # Unset, Converse reserves the model's maximum output and silently
        # consumes that much quota, throttling at low real traffic. 8192 rather
        # than 4096 because write_tf_file emits whole Terraform files in one
        # tool call.
        self.max_output_tokens = int(os.getenv("MODEL_MAX_OUTPUT_TOKENS", "8192"))

        if self.model_chat and self.region:
            # max_attempts=1 because callers run their own visible, backoff-aware
            # retry loop. Left at the botocore default, throttles would be retried
            # invisibly underneath the caller's retries and a sustained rate limit
            # would just look like the agent has frozen.
            self.client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                config=Config(
                    retries={"max_attempts": 1, "mode": "standard"},
                    read_timeout=self.request_timeout + 10,
                    connect_timeout=10,
                ),
            )
        else:
            self.client = None
            logger.warning(
                "BEDROCK_MODEL_CHAT/BEDROCK_REGION not set. Agent will run in mock mode."
            )

    @property
    def available(self) -> bool:
        return self.client is not None

    async def complete(self, messages: list[dict], tools: list[dict] | None = None,
                       model: str | None = None, timeout: float | None = None,
                       temperature: float | None = None):
        """One blocking Converse call, off the event loop and time-boxed.

        Returns a `utils.bedrock.Response` — the OpenAI-shaped shim, so callers
        keep reading `.choices[0].message.tool_calls`.
        """
        if not self.client:
            raise RuntimeError("No model configured (BEDROCK_MODEL_CHAT/BEDROCK_REGION).")

        system, converse_messages = to_converse(messages)
        args: dict = {
            "modelId": model or self.model_chat,
            "messages": converse_messages,
            "inferenceConfig": {"maxTokens": self.max_output_tokens},
        }
        if system:
            args["system"] = system
        if temperature is not None:
            args["inferenceConfig"]["temperature"] = temperature
        tool_config = tools_to_toolconfig(tools)
        if tool_config:
            args["toolConfig"] = tool_config

        raw = await asyncio.wait_for(
            asyncio.to_thread(lambda: self.client.converse(**args)),
            timeout=timeout or self.request_timeout,
        )
        return from_converse(raw)

    # ─── Retry classification ─────────────────────────────────────────────

    @staticmethod
    def is_rate_limit(err_text: str) -> bool:
        t = (err_text or "").lower()
        return any(sig in t for sig in _RATE_LIMIT_SIGNALS)

    @staticmethod
    def is_transient(err_text: str) -> bool:
        t = (err_text or "").lower()
        # Fatal first: a ValidationException's message echoes the request back,
        # and that echo routinely contains a transient-looking substring.
        # Retrying a malformed request never helps, it just hides the cause.
        if any(sig in t for sig in _FATAL_SIGNALS):
            return False
        return any(sig in t for sig in _TRANSIENT_SIGNALS)

    @staticmethod
    def retry_after(err: Exception) -> float | None:
        """The server's Retry-After, when it sent one.

        Bedrock does not send one. A botocore ClientError's `response` is a dict
        with no `.headers`, so this returns None and backoff() takes over — the
        correct degrade, kept because a future gateway may set the header.
        """
        headers = getattr(getattr(err, "response", None), "headers", None)
        if not headers:
            return None
        value = headers.get("Retry-After") or headers.get("retry-after")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def backoff(retry_count: int, is_rate_limit: bool = False) -> float:
        # Rate limits are per-minute quota windows, so an 8s ceiling rarely
        # clears one — give those noticeably more room than a flaky socket.
        cap = 30 if is_rate_limit else 8
        base = 3 if is_rate_limit else 1
        return min(base * (2 ** max(retry_count, 0)), cap)


def repair_tool_call_history(messages: list[dict]) -> list[dict]:
    """Ensure every assistant tool_call is answered before another role appears.

    A step interrupted mid-tool-call leaves a dangling call in the transcript,
    and the model API rejects the whole request when it sees one. Copied from
    ProvisioningAgent._repair_tool_call_history — a pure function there too, so
    the two can be collapsed whenever that agent moves onto this module.
    """
    if not messages:
        return messages

    repaired: list[dict] = []
    pending: dict[str, str] = {}

    def flush():
        nonlocal pending
        for call_id in list(pending.keys()):
            repaired.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": "Tool call was interrupted in a previous step. "
                           "Continue with the latest user intent.",
            })
        pending = {}

    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            flush()
            repaired.append(msg)
            for tc in msg["tool_calls"]:
                call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if call_id:
                    pending[call_id] = ""
            continue

        if role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id in pending:
                pending.pop(call_id, None)
                repaired.append(msg)
            # A tool result with no matching call is dropped: replaying it would
            # be rejected just as a dangling call would.
            continue

        flush()
        repaired.append(msg)

    flush()
    return repaired
