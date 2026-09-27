"""The Bedrock stand-in markitdown talks to for scanned-PDF OCR.

No AWS, no credentials, no network. Run:
    PYTHONPATH=. python tests/test_ocr_shim.py

markitdown only calls `.chat.completions.create(model=, messages=)` and reads
`.choices[0].message.content`, so that surface plus the content translation is
the whole integration. The image translation is the part worth a test: Converse
names the format explicitly and takes raw bytes, where OpenAI takes a base64
data URI.
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.doc_convert import (  # noqa: E402
    _BedrockChatShim, _ocr_llm_client, _ocr_model, _to_converse_content,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"fake pixels"
DATA_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()


def test_plain_string_becomes_one_text_block():
    assert _to_converse_content("read this") == [{"text": "read this"}]


def test_data_uri_image_becomes_raw_bytes():
    """Converse takes bytes and an explicit format, not a data URI."""
    blocks = _to_converse_content([
        {"type": "text", "text": "Transcribe."},
        {"type": "image_url", "image_url": {"url": DATA_URI}},
    ])
    assert blocks[0] == {"text": "Transcribe."}
    assert blocks[1]["image"]["format"] == "png"
    assert blocks[1]["image"]["source"]["bytes"] == PNG


def test_jpg_normalises_to_jpeg():
    uri = "data:image/jpg;base64," + base64.b64encode(b"x").decode()
    blocks = _to_converse_content([{"type": "image_url", "image_url": {"url": uri}}])
    assert blocks[0]["image"]["format"] == "jpeg"   # Converse has no "jpg"


def test_unsupported_format_is_skipped_not_sent():
    uri = "data:image/tiff;base64," + base64.b64encode(b"x").decode()
    blocks = _to_converse_content([
        {"type": "text", "text": "kept"},
        {"type": "image_url", "image_url": {"url": uri}},
    ])
    assert blocks == [{"text": "kept"}]


def test_remote_url_is_skipped():
    """Converse cannot fetch a URL; sending the block would be rejected."""
    blocks = _to_converse_content([
        {"type": "text", "text": "kept"},
        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
    ])
    assert blocks == [{"text": "kept"}]


def test_empty_content_never_yields_an_empty_block_list():
    """Converse rejects an empty content array."""
    assert _to_converse_content([]) == [{"text": "Transcribe this document."}]
    assert _to_converse_content([{"type": "text", "text": "  "}])[0]["text"]


def test_create_builds_a_converse_request_and_unwraps_the_reply():
    captured = {}

    class FakeBedrock:
        def converse(self, **kwargs):
            captured.update(kwargs)
            return {"output": {"message": {"content": [{"text": "# Transcribed"}]}},
                    "stopReason": "end_turn"}

    shim = _BedrockChatShim.__new__(_BedrockChatShim)   # skip boto3 construction
    shim._client = FakeBedrock()

    resp = shim.create(model="a-model", messages=[
        {"role": "system", "content": "You transcribe documents."},
        {"role": "user", "content": [
            {"type": "text", "text": "Transcribe."},
            {"type": "image_url", "image_url": {"url": DATA_URI}},
        ]},
    ])

    assert captured["modelId"] == "a-model"
    assert captured["system"] == [{"text": "You transcribe documents."}]
    assert [m["role"] for m in captured["messages"]] == ["user"]   # system hoisted
    assert captured["inferenceConfig"]["maxTokens"] > 0
    # The one attribute markitdown reads.
    assert resp.choices[0].message.content == "# Transcribed"


def test_client_is_none_without_configuration():
    """Unconfigured degrades to the existing 'unsupported' error, not a crash."""
    saved = {k: os.environ.pop(k, None) for k in (
        "BEDROCK_MODEL_OCR", "BEDROCK_MODEL_CODEGEN", "BEDROCK_MODEL",
        "BEDROCK_REGION", "AWS_REGION")}
    try:
        assert _ocr_model() == ""
        assert _ocr_llm_client() is None
        # A region alone is not enough — no model means no OCR.
        os.environ["BEDROCK_REGION"] = "us-east-1"
        assert _ocr_llm_client() is None
    finally:
        os.environ.pop("BEDROCK_REGION", None)
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_ocr_model_falls_back_through_the_role_chain():
    saved = {k: os.environ.pop(k, None) for k in (
        "BEDROCK_MODEL_OCR", "BEDROCK_MODEL_CODEGEN", "BEDROCK_MODEL")}
    try:
        os.environ["BEDROCK_MODEL"] = "base"
        assert _ocr_model() == "base"
        os.environ["BEDROCK_MODEL_CODEGEN"] = "codegen"
        assert _ocr_model() == "codegen"
        os.environ["BEDROCK_MODEL_OCR"] = "ocr"
        assert _ocr_model() == "ocr"
    finally:
        for k in ("BEDROCK_MODEL_OCR", "BEDROCK_MODEL_CODEGEN", "BEDROCK_MODEL"):
            os.environ.pop(k, None)
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
