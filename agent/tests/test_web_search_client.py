"""sanitise_query's identifier redaction, authority scoring, and the mechanical
verify_result containment check (with an injected fake fetch — no real
network call). Run: python tests/test_web_search_client.py"""
from utils import web_search_client as web

# ─── sanitise_query: every identifier class the POC's own tests targeted ────
CASES = [
    ("account id", "look up 123456789012 outage", "123456789012"),
    ("ARN", "check arn:aws:s3:::my-bucket/key.txt for details", "arn:aws:"),
    ("IPv4", "connect to 10.0.1.25 and see", "10.0.1.25"),
    ("subnet id", "the subnet-0a1b2c3d4e5f67890 is unreachable", "subnet-0a1b2c3d4e5f67890"),
    ("vpc id", "vpc-0a1b2c3d4e5f67890 routing issue", "vpc-0a1b2c3d4e5f67890"),
    ("security group id", "sg-0a1b2c3d4e5f67890 blocks traffic", "sg-0a1b2c3d4e5f67890"),
    ("instance id", "i-0a1b2c3d4e5f67890 is unhealthy", "i-0a1b2c3d4e5f67890"),
    ("internal hostname", "reach db.abc123.us-east-1.rds.amazonaws.com now", "amazonaws.com"),
    ("incident id", "similar to INC-2025-0101-a1 last month", "INC-2025-0101"),
    ("email", "contact ops-team@example.com for context", "@example.com"),
]

for label, raw, must_not_appear in CASES:
    cleaned = web.sanitise_query(raw)
    assert must_not_appear.lower() not in cleaned.lower(), (
        f"{label}: {must_not_appear!r} leaked into sanitized query {cleaned!r}"
    )

# A query with nothing identifying should pass through basically unchanged
# (just whitespace-normalized), not get mangled.
assert web.sanitise_query("why did the ALB return 5xx errors") == "why did the ALB return 5xx errors"

# Whitespace left behind by redaction must collapse, not leave double spaces.
collapsed = web.sanitise_query("account 123456789012 in region us-east-1")
assert "  " not in collapsed, collapsed

# ─── Authority scoring ────────────────────────────────────────────────────────
assert web.authority_of("https://docs.aws.amazon.com/whatever") == "vendor_doc"
assert web.authority_of("https://health.aws.amazon.com/incident/123") == "official_postmortem"
assert web.authority_of("https://blog.cloudflare.com/some-outage") == "official_postmortem"
assert web.authority_of("https://some-random-blog.example.com/post") == "community"
assert web.authority_of("not-a-url") == "unknown"

# ─── verify_result: mechanical containment check, injected fetch (no network) ─
result = {
    "title": "Example outage doc", "url": "https://docs.aws.amazon.com/x",
    "snippet": "Connections drop when the target group deregistration delay is too short",
    "authority": "vendor_doc",
}

verified = web.verify_result(result, fetch_fn=lambda url: (
    "This page explains that connections drop when the target group "
    "deregistration delay is too short for in-flight requests to complete."
))
assert verified["verified"] is True, verified
assert verified["confidence"] == web._AUTHORITY_WEIGHT["vendor_doc"], verified

unverified = web.verify_result(result, fetch_fn=lambda url: "this page says something unrelated entirely")
assert unverified["verified"] is False, unverified
assert unverified["confidence"] < verified["confidence"], (unverified, verified)

# A fetch failure (None) must degrade to unverified, not raise.
failed_fetch = web.verify_result(result, fetch_fn=lambda url: None)
assert failed_fetch["verified"] is False, failed_fetch

# Community/unknown sources get a lower ceiling than vendor docs even when verified.
community_result = {**result, "authority": "community"}
community_verified = web.verify_result(
    community_result,
    fetch_fn=lambda url: "Connections drop when the target group deregistration delay is too short",
)
assert community_verified["verified"] is True
assert community_verified["confidence"] < verified["confidence"], (community_verified, verified)

print("ok: sanitise_query redacts every identifier class, authority scoring, "
      "verify_result containment check (verified/unverified/fetch-failure/authority-weighted)")