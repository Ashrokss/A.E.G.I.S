"""Tavily-backed Terraform doc discovery (utils/browser.py + web_search_client).

Replaces a hardcoded resource->URL table. Checks:
  * the doc allow-list is by domain, not by a URL table
  * the registry URL / coverage helpers work for ANY aws_* type
  * search sanitises the query and restricts Tavily to the doc domains
  * no API key -> [] (degrade, never raise)
No real network: httpx.post is stubbed.

Run: python -m tests.test_doc_search
"""
import asyncio
import os

from utils import browser
from utils import web_search_client as web

# ─── allow-list is domain-based ─────────────────────────────────────────────
allowed = [
    "https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue",
    "https://developer.hashicorp.com/terraform/language/meta-arguments/for_each",
    "https://github.com/hashicorp/terraform-provider-aws/blob/main/website/docs/r/sns_topic.html.markdown",
    "https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/welcome.html",
]
refused = [
    "http://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue",  # not https
    "https://registry.terraform.io.evil.example/providers/x",                                  # suffix trick
    "https://github.com/someone-else/terraform-provider-aws/blob/main/x.md",                  # other repo
    "https://example.com/terraform",
    "",
]
for u in allowed:
    assert browser.is_allowed_doc_url(u), u
for u in refused:
    assert not browser.is_allowed_doc_url(u), u

# ─── any aws_* type, no table ───────────────────────────────────────────────
assert browser.registry_url_for("aws_sqs_queue") == (
    "https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/sqs_queue"
)
assert browser.doc_url_covers(browser.registry_url_for("aws_sqs_queue"), "aws_sqs_queue")
assert browser.doc_url_covers(allowed[2], "aws_sns_topic"), "provider markdown counts as coverage"
assert not browser.doc_url_covers(browser.registry_url_for("aws_sqs_queue"), "aws_sqs_queue_policy"), \
    "a prefix of another type must not count"

# ─── search: sanitised query, domain-restricted, graceful without a key ─────
sent = {}


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"results": [
            {"title": "aws_sqs_queue", "url": allowed[0], "content": "Provides an SQS queue."},
            {"title": "off-list", "url": "https://example.com/x", "content": "spam"},
        ]}


def _fake_post(url, json=None, headers=None, timeout=None):
    sent["url"], sent["json"] = url, json
    return _Resp()


real_post = web.httpx.post
web.httpx.post = _fake_post
os.environ["TAVILY_API_KEY"] = "test-key"
try:
    hits = asyncio.run(browser.search_terraform_docs("aws_sqs_queue in account 123456789012"))
    assert sent["url"] == web.TAVILY_URL
    assert "123456789012" not in sent["json"]["query"], "account ids must never leave the process"
    assert sent["json"]["include_domains"] == list(browser.DOC_DOMAINS)
    assert [h["url"] for h in hits] == [allowed[0]], "off-list results are dropped"

    os.environ["TAVILY_API_KEY"] = ""
    assert asyncio.run(browser.search_terraform_docs("aws_sqs_queue")) == [], "no key -> no results, no raise"
    assert web.extract(allowed[3]) is None, "no key -> no extract"
finally:
    web.httpx.post = real_post
    os.environ.pop("TAVILY_API_KEY", None)

# ─── fetch refuses anything off the allow-list before any network call ─────
out = asyncio.run(browser.fetch_terraform_docs("https://example.com/terraform"))
assert out.startswith("Error:") and "search_terraform_docs" in out

print("✅ test_doc_search passed")
