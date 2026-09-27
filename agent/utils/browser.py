import asyncio
import httpx
import html2text
from bs4 import BeautifulSoup
import logging
import re
from urllib.parse import urlparse

from utils import web_search_client

logger = logging.getLogger(__name__)

# Registry docs pages are JS apps; provider docs are plain markdown on GitHub.
_GITHUB_AWS_DOCS = "https://raw.githubusercontent.com/hashicorp/terraform-provider-aws/main/website/docs"
_REGISTRY_RESOURCE_RE = re.compile(
    r"^https://registry\.terraform\.io/providers/hashicorp/aws/latest/docs/resources/([\w]+)/?$"
)
_REGISTRY_DATASOURCE_RE = re.compile(
    r"^https://registry\.terraform\.io/providers/hashicorp/aws/latest/docs/data-sources/([\w]+)/?$"
)


# Official Terraform / AWS documentation. Discovery (search_terraform_docs) is
# restricted to these, and fetch_terraform_docs refuses anything else — the
# model never fetches an arbitrary URL.
DOC_DOMAINS = (
    "registry.terraform.io",
    "developer.hashicorp.com",
    "github.com/hashicorp/terraform-provider-aws",
    "docs.aws.amazon.com",
)
_REGISTRY_BASE = "https://registry.terraform.io/providers/hashicorp/aws/latest/docs"


def is_allowed_doc_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    target = (parsed.netloc + parsed.path).lower()
    return any(target == d or target.startswith(d + "/") for d in DOC_DOMAINS)


def registry_url_for(resource_type: str, kind: str = "resources") -> str:
    """aws_s3_bucket -> the registry page for it. A pure function of the type
    name, so the provider's whole catalogue is covered with no URL table."""
    return f"{_REGISTRY_BASE}/{kind}/{resource_type.removeprefix('aws_')}"


def doc_url_covers(url: str, resource_type: str) -> bool:
    """Does a fetched doc URL document this resource type? Accepts the registry
    page (resources/ or data-sources/) and the provider's GitHub markdown."""
    name = resource_type.removeprefix("aws_")
    path = urlparse(url or "").path.rstrip("/").lower()
    return path.endswith((f"/resources/{name}", f"/data-sources/{name}",
                          f"/r/{name}.html.markdown", f"/d/{name}.html.markdown"))


async def search_terraform_docs(query: str, max_results: int = 5) -> list[dict]:
    """Tavily search restricted to DOC_DOMAINS. [] when Tavily is unavailable —
    the caller then points the model at registry_url_for() directly."""
    q = (query or "").strip()
    if not q:
        return []
    if "terraform" not in q.lower():
        q = f"terraform aws provider {q}"
    results = await asyncio.to_thread(
        web_search_client.search, q, max_results, list(DOC_DOMAINS),
    )
    return [r for r in results if is_allowed_doc_url(r.get("url", ""))]


def _registry_url_to_github_raw(url: str) -> str | None:
    url = (url or "").strip()
    m = _REGISTRY_RESOURCE_RE.match(url)
    if m:
        return f"{_GITHUB_AWS_DOCS}/r/{m.group(1)}.html.markdown"
    m = _REGISTRY_DATASOURCE_RE.match(url)
    if m:
        return f"{_GITHUB_AWS_DOCS}/d/{m.group(1)}.html.markdown"
    return None


def _extract_required_arguments(markdown_text: str) -> list[str]:
    """
    Best-effort extraction of required argument names from Terraform docs.
    Works on HashiCorp website markdown (resource pages).
    """
    required = set()
    patterns = [
        r"`([a-zA-Z0-9_]+)`\s*[-:]*\s*\(Required\)",
        r"^\s*[-*]\s*`([a-zA-Z0-9_]+)`.*\(Required\)",
        r"\*\s*`([a-zA-Z0-9_]+)`\s*-\s*\(Required\)",
    ]
    for line in markdown_text.splitlines():
        for pat in patterns:
            m = re.search(pat, line)
            if m:
                required.add(m.group(1))
    return sorted(required)


def _summarize_doc_body(registry_url: str, body: str) -> str:
    lines = [line.strip() for line in body.split("\n") if line.strip()]
    compact_text = "\n".join(lines[:400])
    required_args = _extract_required_arguments(compact_text)
    summary_lines = [
        "DOC_PARSE_SUMMARY",
        f"DOC_URL: {registry_url}",
    ]
    if required_args:
        summary_lines.append("REQUIRED_ARGUMENTS: " + ", ".join(required_args))
    else:
        summary_lines.append("REQUIRED_ARGUMENTS: not_detected")
    summary_lines.append("----")
    return "\n".join(summary_lines) + "\n" + compact_text


async def _fetch_github_markdown(registry_url: str) -> str | None:
    gh_url = _registry_url_to_github_raw(registry_url)
    if not gh_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(gh_url)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.text
    except Exception as e:
        logger.warning(f"GitHub doc fetch failed for {gh_url}: {e}")
        return None


async def fetch_terraform_docs(url: str) -> str:
    """
    Load Terraform docs for a Registry URL. Prefers GitHub markdown (no JS) for hashicorp/aws,
    then falls back to scraping the Registry HTML (often incomplete without JS).
    """
    if not is_allowed_doc_url(url):
        return ("Error: Only official documentation URLs are supported ("
                + ", ".join(DOC_DOMAINS) + "). Use search_terraform_docs to find one.")

    # 1. Registry resource/data-source pages: the provider's own markdown on
    #    GitHub — free, complete, no JavaScript.
    md = await _fetch_github_markdown(url)
    if md:
        return _summarize_doc_body(url, md)

    # 2. Anything else (guides, developer.hashicorp.com, AWS docs, JS-rendered
    #    registry pages): Tavily Extract, when configured.
    extracted = await asyncio.to_thread(web_search_client.extract, url)
    if extracted:
        return _summarize_doc_body(url, extracted)

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")

            for element in soup(["script", "style", "nav", "footer", "header"]):
                element.decompose()

            main_content = soup.find("div", id="main-content") or soup.find("article") or soup.body

            if not main_content:
                return "Error: Could not identify main content on the page."

            converter = html2text.HTML2Text()
            converter.ignore_links = False
            converter.ignore_images = True
            converter.body_width = 0

            text = converter.handle(str(main_content))
            return _summarize_doc_body(url, text)

    except Exception as e:
        logger.error(f"Error fetching documentation: {e}")
        return f"Error: Failed to fetch documentation from {url}. {str(e)}"
