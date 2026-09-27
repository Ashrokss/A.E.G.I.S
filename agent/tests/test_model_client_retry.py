"""Retry classification against real botocore errors.

No AWS, no credentials, no network — ClientError objects are constructed
directly. Run:
    python tests/test_model_client_retry.py

The load-bearing case is the fatal list: a ValidationException echoes the
offending request back in its message, so a wrong model id produces error text
that contains transient-looking substrings. Without a fatal-first check that
gets retried nine times a turn and looks like a hang, not a config error.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from botocore.exceptions import ClientError  # noqa: E402

from utils.model_client import ModelClient  # noqa: E402


def _err(code: str, message: str = "something went wrong") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


def test_bedrock_transient_codes_retry():
    for code in ("ThrottlingException", "ModelTimeoutException",
                 "ServiceUnavailableException", "InternalServerException",
                 "ModelNotReadyException"):
        assert ModelClient.is_transient(str(_err(code))), code


def test_bedrock_fatal_codes_do_not_retry():
    for code in ("ValidationException", "AccessDeniedException",
                 "ResourceNotFoundException", "UnrecognizedClientException",
                 "ExpiredTokenException", "SerializationException"):
        assert not ModelClient.is_transient(str(_err(code))), code


def test_fatal_wins_over_transient_substring_in_the_echoed_message():
    """The whole reason _FATAL_SIGNALS is checked first."""
    err = _err("ValidationException",
               "The model returned 503 and timed out: invalid modelId 'gpt-4o'")
    text = str(err)
    assert "503" in text and "timed out" in text      # transient signals present
    assert not ModelClient.is_transient(text)          # and still not retried


def test_access_denied_is_not_retried_even_when_it_mentions_throttling():
    err = _err("AccessDeniedException",
               "You don't have access to the model; rate limit docs at ...")
    assert not ModelClient.is_transient(str(err))


def test_throttling_is_a_rate_limit():
    assert ModelClient.is_rate_limit(str(_err("ThrottlingException")))
    assert ModelClient.is_rate_limit(str(_err("TooManyRequestsException")))
    assert not ModelClient.is_rate_limit(str(_err("ModelTimeoutException")))


def test_pre_existing_signals_still_classify():
    """The Azure-era strings stay: a bare socket drop is still transient."""
    assert ModelClient.is_transient("Connection aborted: RemoteDisconnected")
    assert ModelClient.is_transient("502 Bad Gateway")
    assert not ModelClient.is_transient("something entirely unremarkable")


def test_retry_after_degrades_to_none_for_client_errors():
    """ClientError.response is a dict with no .headers — backoff() takes over."""
    assert ModelClient.retry_after(_err("ThrottlingException")) is None
    assert ModelClient.retry_after(RuntimeError("no response at all")) is None


def test_backoff_gives_rate_limits_more_room():
    assert ModelClient.backoff(0, is_rate_limit=False) == 1
    assert ModelClient.backoff(0, is_rate_limit=True) == 3
    assert ModelClient.backoff(10, is_rate_limit=False) == 8    # transient cap
    assert ModelClient.backoff(10, is_rate_limit=True) == 30    # rate-limit cap


def test_unset_model_means_mock_mode_not_a_broken_id():
    """No string defaults: a stale gpt-4o would ValidationException forever."""
    saved = {k: os.environ.pop(k, None) for k in (
        "BEDROCK_MODEL", "BEDROCK_MODEL_CHAT", "BEDROCK_MODEL_CODEGEN",
        "BEDROCK_MODEL_HEAVY", "BEDROCK_REGION", "AWS_REGION")}
    try:
        mc = ModelClient()
        assert not mc.available
        assert mc.model_chat == ""
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_region_without_model_is_still_mock_mode():
    saved = {k: os.environ.pop(k, None) for k in (
        "BEDROCK_MODEL", "BEDROCK_MODEL_CHAT", "BEDROCK_REGION", "AWS_REGION")}
    os.environ["BEDROCK_REGION"] = "us-east-1"
    try:
        assert not ModelClient().available
    finally:
        os.environ.pop("BEDROCK_REGION", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
            passed += 1
    print(f"\n{passed} checks passed")
