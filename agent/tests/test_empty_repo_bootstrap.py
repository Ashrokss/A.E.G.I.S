"""A brand-new repository can be connected and pushed to: the agent creates the
first commit so a pull request has a base to target.
Run: python test_empty_repo_bootstrap.py"""
import base64

from tools.github_push import (
    GitHubCreds,
    GitHubPushError,
    _bootstrap_empty_repo,
    _repo_state,
)
from tools.repo_scaffold import BOOTSTRAP_FILES

HEADERS = {"Authorization": "Bearer t"}
CREDS = GitHubCreds(token="t", repo_full_name="acme/infra", base_branch="main")


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeClient:
    """Answers GETs from a {url_suffix: FakeResp} map and records PUTs."""

    def __init__(self, routes):
        self.routes = routes
        self.puts = []

    def get(self, url, headers=None):
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(f"unexpected GET {url}")

    def put(self, url, headers=None, json=None):
        self.puts.append((url, json))
        return FakeResp(201, {"commit": {"sha": "abc"}})


# --- _repo_state -----------------------------------------------------------

# A normal repo with an existing base branch is not empty.
c = FakeClient({
    "/repos/acme/infra": FakeResp(200, {"default_branch": "main", "size": 120}),
    "/branches/main": FakeResp(200, {"name": "main"}),
})
assert _repo_state(c, CREDS.repo_full_name, "main", HEADERS) == (False, "main")

# No commits at all: neither the configured nor the default branch exists.
c = FakeClient({
    "/repos/acme/infra": FakeResp(200, {"default_branch": "main", "size": 0}),
    "/branches/main": FakeResp(404),
})
assert _repo_state(c, CREDS.repo_full_name, "main", HEADERS) == (True, "main")

# size is on-disk KB and lags a push, so it must not be the deciding signal: a
# repo reporting size 0 whose branch DOES exist is not empty.
c = FakeClient({
    "/repos/acme/infra": FakeResp(200, {"default_branch": "main", "size": 0}),
    "/branches/main": FakeResp(200, {"name": "main"}),
})
assert _repo_state(c, CREDS.repo_full_name, "main", HEADERS) == (False, "main")

# A repo with commits but a wrong configured base branch is a different error
# from an empty repo, and must say so rather than bootstrapping over it.
c = FakeClient({
    "/repos/acme/infra": FakeResp(200, {"default_branch": "main", "size": 90}),
    "/branches/release": FakeResp(404),
    "/branches/main": FakeResp(200, {"name": "main"}),
})
try:
    _repo_state(c, CREDS.repo_full_name, "release", HEADERS)
except GitHubPushError as e:
    assert "release" in str(e), e
else:
    raise AssertionError("a missing base branch on a non-empty repo must raise")

# A repo the token cannot see is reported plainly.
c = FakeClient({"/repos/acme/infra": FakeResp(404)})
try:
    _repo_state(c, CREDS.repo_full_name, "main", HEADERS)
except GitHubPushError as e:
    assert "acme/infra" in str(e), e
else:
    raise AssertionError("a 404 repo must raise")

# --- _bootstrap_empty_repo -------------------------------------------------

c = FakeClient({})
base = _bootstrap_empty_repo(c, CREDS, HEADERS, "main")
assert base == "main", base
assert len(c.puts) == len(BOOTSTRAP_FILES), c.puts

first_url, first_body = c.puts[0]
assert first_url.endswith("/contents/README.md"), first_url
assert first_body["branch"] == "main", first_body
decoded = base64.b64decode(first_body["content"]).decode()
assert "AEGIS" in decoded and "environments/dev" in decoded, decoded

second_url, second_body = c.puts[1]
assert second_url.endswith("/contents/.gitignore"), second_url
ignored = base64.b64decode(second_body["content"]).decode()
assert "*.tfstate" in ignored and "tfplan" in ignored, ignored
# Must NOT ignore tfvars: the agent commits terraform.tfvars as example values,
# and a repo-level ignore would silently drop it from every commit.
assert "*.tfvars\n" not in ignored, ignored


class ConflictClient(FakeClient):
    def put(self, url, headers=None, json=None):
        self.puts.append((url, json))
        return FakeResp(422, {"message": "already exists"})


# A concurrent bootstrap is success, not a failure.
c = ConflictClient({})
assert _bootstrap_empty_repo(c, CREDS, HEADERS, "main") == "main"


class DeniedClient(FakeClient):
    def put(self, url, headers=None, json=None):
        return FakeResp(403, {"message": "Resource not accessible by personal access token"})


c = DeniedClient({})
try:
    _bootstrap_empty_repo(c, CREDS, HEADERS, "main")
except GitHubPushError as e:
    assert "README.md" in str(e) or "403" in str(e), e
else:
    raise AssertionError("a rejected write must raise")

print("ok: empty repositories are detected and bootstrapped before the PR")
