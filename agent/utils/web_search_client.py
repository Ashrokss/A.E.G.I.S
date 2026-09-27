"""
Live web research for the SRE agent's search_web tool.

Ports the safety mechanisms of an earlier POC's rca/research.py + rca/verify.py
(identifier sanitization on every outbound query, authority scoring, and a
mechanical fetch-and-check quote/claim verification) as plain Python + httpx,
against a real provider (Tavily) rather than the POC's own pluggable mock/
offline/mirror search functions — this codebase has one concrete provider, not
a swappable set.

Previously Serper; switched after its API key started rejecting every
request (both the original key and a freshly-generated replacement got an
identical 403, an account-side issue outside this codebase's control).
Tavily is purpose-built for this exact "agent tool calls search, gets clean
LLM-ready results" pattern, and its free tier (1,000 credits/month) needs no
credit card. search()'s return shape is unchanged, so verify_result(),
authority_of(), and tools/web_research.py needed zero changes.

sanitise_query is the ONLY function permitted to build an outbound query. It
strips every AWS-account-identifying token before anything leaves this
process: ARNs, 12-digit account ids, IPs, subnet/vpc/sg/instance/volume/eni
ids, internal hostnames, this tenant's own incident ids, and email addresses.
This is a hard boundary, not a convention — search() calls it unconditionally
and cannot be called with a raw, unsanitised query.
"""
import logging
import os
import re

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger("uvicorn.error")

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_EXTRACT_URL = "https://api.tavily.com/extract"
DEFAULT_RESULTS = 5
FETCH_TIMEOUT_SECONDS = 10.0
MAX_FETCH_BYTES = 500_000  # a verification fetch is not a scrape job

# ─── Sanitization ───────────────────────────────────────────────────────────

_ARN_RE = re.compile(r"arn:aws:[^\s]+")
_ACCOUNT_RE = re.compile(r"\b\d{12}\b")
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_RESOURCE_ID_RE = re.compile(r"\b(?:subnet|vpc|sg|i|vol|eni)-[0-9a-f]{8,}\b", re.IGNORECASE)
_HOST_RE = re.compile(r"\b[\w.-]+\.(?:amazonaws\.com|internal|local|azurewebsites\.net)\b")
_INCIDENT_ID_RE = re.compile(r"\bINC-\d{4}-\d{4}(?:-[A-Z0-9-]+)?\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")

_REDACTIONS = (_ARN_RE, _HOST_RE, _EMAIL_RE, _ACCOUNT_RE, _IP_RE, _RESOURCE_ID_RE, _INCIDENT_ID_RE)


def sanitise_query(text: str) -> str:
    out = text
    for pattern in _REDACTIONS:
        out = pattern.sub("", out)
    return " ".join(out.split())


# ─── Authority scoring ──────────────────────────────────────────────────────

_AUTHORITY_DOMAINS = {
    "docs.aws.amazon.com": "vendor_doc",
    "docs.microsoft.com": "vendor_doc",
    "learn.microsoft.com": "vendor_doc",
    "cloud.google.com": "vendor_doc",
    "kubernetes.io": "vendor_doc",
    "health.aws.amazon.com": "official_postmortem",
    "aws.amazon.com": "official_postmortem",
    "status.aws.amazon.com": "official_postmortem",
    "azure.status.microsoft": "official_postmortem",
    "blog.cloudflare.com": "official_postmortem",
    "about.gitlab.com": "official_postmortem",
    "engineering.fb.com": "official_postmortem",
}
_AUTHORITY_WEIGHT = {"vendor_doc": 1.0, "official_postmortem": 0.8, "community": 0.4, "unknown": 0.1}


def authority_of(url: str) -> str:
    for domain, tier in _AUTHORITY_DOMAINS.items():
        if domain in url:
            return tier
    return "community" if url.startswith("http") else "unknown"


# ─── Search ─────────────────────────────────────────────────────────────────

def search(query: str, num_results: int = DEFAULT_RESULTS, include_domains: list[str] | None = None) -> list[dict]:
    """One Tavily call against a SANITIZED query. Returns [] on any failure —
    a research step that can't reach the search API degrades to "no
    results", never raises into the tool-calling loop.

    Deliberately does not request Tavily's own raw_content/answer bundling:
    verify_result() below already does its own independent page fetch and
    mechanical containment check, so trusting Tavily's own extracted content
    instead would defeat that independent check. "basic" search_depth (1
    credit/query) is enough for the SERP-shaped title/url/snippet this
    function returns; the free tier is 1,000 credits/month."""
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key:
        logger.warning("TAVILY_API_KEY not set — search_web will find nothing.")
        return []
    clean_query = sanitise_query(query)
    if not clean_query:
        return []
    try:
        resp = httpx.post(
            TAVILY_URL,
            json={
                "api_key": api_key,
                "query": clean_query,
                "max_results": num_results,
                "search_depth": "basic",
                # Restricts results to these sites (Terraform doc discovery passes the
                # official registry/HashiCorp/AWS doc domains; KB research passes none).
                **({"include_domains": include_domains} if include_domains else {}),
            },
            headers={"Content-Type": "application/json"},
            timeout=FETCH_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as ex:
        logger.warning("[web_research] Tavily search failed: %s", ex)
        return []

    results = []
    for item in (data.get("results") or [])[:num_results]:
        url = item.get("url") or ""
        results.append({
            "title": item.get("title") or url,
            "url": url,
            "snippet": item.get("content") or "",
            "authority": authority_of(url),
        })
    return results


def extract(url: str) -> str | None:
    """Clean page content via Tavily Extract — for JS-rendered doc pages that a
    plain GET cannot read. None on any failure or with no API key; the caller
    decides the fallback. The URL itself is the request, so there is nothing to
    sanitise: callers only pass allow-listed public documentation URLs."""
    api_key = os.getenv("TAVILY_API_KEY", "").strip()
    if not api_key or not url:
        return None
    try:
        resp = httpx.post(
            TAVILY_EXTRACT_URL,
            json={"api_key": api_key, "urls": [url], "extract_depth": "basic"},
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        results = (resp.json() or {}).get("results") or []
    except Exception as ex:
        logger.warning("[web_search] Tavily extract failed for %s: %s", url, ex)
        return None
    content = (results[0].get("raw_content") if results else None) or ""
    return content[:MAX_FETCH_BYTES] or None


# ─── Verification ───────────────────────────────────────────────────────────

def fetch_text(url: str) -> str | None:
    """Best-effort plain text of a page, for a mechanical containment check.
    None on any failure (timeout, non-HTML, 4xx/5xx, JS-rendered content
    BeautifulSoup can't see) — verification then falls back to treating the
    result as unverified rather than raising."""
    try:
        resp = httpx.get(
            url, timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; AEGIS-agent/1.0)"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return None
        soup = BeautifulSoup(resp.content[:MAX_FETCH_BYTES], "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return " ".join(soup.get_text(separator=" ").split())
    except Exception as ex:
        logger.info("[web_research] could not fetch %s: %s", url, ex)
        return None


def verify_result(result: dict, fetch_fn=fetch_text) -> dict:
    """Mechanical, no-LLM check: does the page actually contain what its own
    snippet claims? `fetch_fn` is injectable so tests can exercise this
    without a real network call. An unverified source is marked, not
    dropped — the model/user still sees it, just labeled as lower
    confidence, mirroring how a PERMISSION_MISSING tool result is a finding
    (a gap), not a silent omission, elsewhere in this codebase."""
    page_text = fetch_fn(result["url"])
    snippet = (result.get("snippet") or "").strip().lower()
    verified = bool(page_text and snippet and snippet[:80] in page_text.lower())
    weight = _AUTHORITY_WEIGHT.get(result.get("authority", "unknown"), 0.1)
    confidence = round(weight if verified else weight * 0.3, 3)
    return {**result, "verified": verified, "confidence": confidence}