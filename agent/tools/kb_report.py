"""
Knowledge-base RCA report generator: a structured, field-by-field report
compiled from a tenant's uploaded/promoted knowledge, distinct from
sre_agent.py's submit_rca (which publishes a live investigation's own
conclusion). This asks "what does the knowledge base know about X", not
"what did this investigation find" -- the two can disagree, and both are
useful, which is why they are two capabilities, not one.

Ports the shape of an earlier POC's rca/report.py: each field is generated
and citation-checked INDEPENDENTLY, and any field with generated text but no
resolvable citation is moved to unsupported_fields rather than kept -- text
that reads as evidenced but is not is worse than an honest blank, per that
POC's own reasoning.
"""
import asyncio
import logging
import re
from datetime import datetime

from tools import knowledge_base as kb
from utils import web_search_client
from utils.model_client import ModelClient

logger = logging.getLogger("uvicorn.error")

RETRIEVAL_K = 8

SYSTEM_PROMPT = (
    "You are compiling one field of a root-cause-analysis report from a knowledge base of past "
    "incident documents. Answer ONLY the field asked below, using ONLY the retrieved context "
    "provided -- never outside knowledge. Cite every claim by quoting its citation_tag exactly "
    "as shown, e.g. [cite_key · section]. If the context does not support an answer for this "
    "field, reply with exactly the words: NOT SUPPORTED"
)

_SECTIONS = {
    "summary": "A one-paragraph summary of the incident or topic.",
    "impact": "Who or what was affected, and how severely.",
    "root_cause": "What went wrong and why, in as much detail as the context supports.",
    "detection": "How the issue was (or would be) detected.",
}
_LIST_SECTIONS = {
    "timeline": "Key events in chronological order, one per line as '<when> — <what>'.",
    "contributing_factors": "Contributing factors, one per line.",
    "mitigation": "Mitigation steps taken or recommended, one per line.",
    "action_items": "Concrete follow-up action items, one per line.",
}
_NOT_SUPPORTED = "NOT SUPPORTED"

# Used only by generate_report_from_web -- same per-field/citation-check
# mechanism as the knowledge-base version, but citing a web source instead
# of a knowledge-base passage, because this path exists specifically for a
# knowledge gap, where by definition the knowledge base has nothing.
WEB_SYSTEM_PROMPT = (
    "You are compiling one field of a root-cause-analysis-style report from verified public web "
    "search results, because the tenant's knowledge base has nothing on this topic. Answer ONLY "
    "the field asked below, using ONLY the web results provided -- never outside knowledge. Cite "
    "every claim by writing the source's URL exactly as shown, in the form (web: <url>). "
    "A web search for a name or short phrase often returns results about a completely different "
    "person, product, or entity that happens to share the name -- a namesake, not a match. Before "
    "answering, check that a result is actually about the SPECIFIC subject the topic names (e.g. "
    "the same employer, product, or context), not merely the same name in isolation. If the web "
    "results are only about a namesake, or do not support an answer for this field, reply with "
    "exactly the words: NOT SUPPORTED"
)
# A knowledge gap isn't always an incident -- "who reports to X" or "what does
# Y do" land here just as often as an AWS failure mode, and every field above
# (root_cause, mitigation, detection, timeline...) is nonsensical for those,
# so the model correctly answers NOT SUPPORTED for nearly all of them even
# when the web search found real, relevant, citable information. This is the
# fallback used only when EVERY structured field above came back unsupported:
# one plain, unstructured answer to the actual topic, not shoehorned into an
# RCA report's shape.
GENERAL_WEB_SYSTEM_PROMPT = (
    "You are answering a question using verified public web search results, because the tenant's "
    "knowledge base has nothing on this topic. This may or may not be about an AWS incident -- "
    "answer whatever the topic actually asks, directly and factually, using ONLY the web results "
    "provided. Cite every claim by writing the source's URL exactly as shown, in the form "
    "(web: <url>). "
    "A web search for a name or short phrase often returns results about a completely different "
    "person, product, or entity that happens to share the name -- a namesake, not a match. Before "
    "answering, check that a result is actually about the SPECIFIC subject the topic names (e.g. "
    "the same employer, product, or context), not merely the same name in isolation. If the web "
    "results are only about a namesake, or do not support any answer at all, reply with exactly "
    "the words: NOT SUPPORTED"
)
_WEB_CITE_RE = re.compile(r"\(web:\s*(\S+?)\)")

_TOPIC_STOPWORDS = {
    "the", "a", "an", "at", "of", "in", "on", "for", "to", "and", "or", "is", "are",
    "was", "were", "with", "by", "from", "this", "that", "what", "who", "how", "why",
    "did", "does", "do", "our", "we", "us", "it", "its", "be", "as", "any", "all",
}


def _topic_terms(topic: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", topic.lower())
    return [w for w in words if w not in _TOPIC_STOPWORDS and len(w) > 2]


def _topic_anchor_terms(topic: str) -> list[str]:
    """The topic's own named entities/proper nouns -- capitalized words other
    than the sentence-initial one (capitalized by ordinary English sentence
    casing, not because it names anything), e.g. "Harsh"/"Goyal"/"Xebia" out
    of "Harsh Goyal at Xebia", or "Secrets"/"Manager" out of "...Secrets
    Manager rotated them...". These are the words actually doing the
    disambiguating work. Requiring every word of a topic to appear verbatim
    (the previous version of this check) correctly rejected a namesake
    match, but also rejected genuinely on-topic official AWS documentation
    for a real Step Functions question -- it never used the word "silently"
    or the plural "executions", ordinary phrasing variation a strict
    all-words check can't tell apart from being about something else
    entirely. A topic's descriptive wording (verbs, adverbs, connectors)
    legitimately varies between how a question is asked and how a real
    source describes the same thing; a genuine named entity does not."""
    words = topic.split()
    anchors = []
    for i, w in enumerate(words):
        cleaned = re.sub(r"[^A-Za-z0-9]", "", w)
        if i == 0 or not cleaned or not cleaned[0].isupper():
            continue
        token = cleaned.lower()
        if token in _TOPIC_STOPWORDS or len(token) <= 2:
            continue
        anchors.append(token)
    return anchors


def _context_covers_topic(context: str, topic: str) -> bool:
    """Mechanical guard against a namesake/unrelated match: a web search for a
    short topic like a person's name routinely returns results about a
    completely different person or thing that happens to share a word with
    it -- verified empirically that telling the model to check for this in
    the prompt is not enough; it still confidently reports on the wrong
    entity as if the name match were the real one. Rather than trust the
    model's own judgment, require the topic's own named entities to actually
    appear somewhere in the fetched web content before generating anything
    from it at all -- the same "does the source's own text actually contain
    what we're about to claim" principle web_search_client.py's
    verify_result() already applies to individual claims, applied here to
    the topic as a whole before any field-filling is attempted."""
    lowered = context.lower()
    anchors = _topic_anchor_terms(topic)
    if anchors:
        return all(a in lowered for a in anchors)
    # No named entity in the topic at all -- an unnamed internal question
    # ("what team owns X") has no single specific anchor to verify against.
    # Fall back to the loosest possible floor: at least one real content
    # word actually appears, rather than requiring nothing at all.
    terms = _topic_terms(topic)
    if not terms:
        return True
    return any(t in lowered for t in terms)


async def _ask_field(
    mc: ModelClient, context: str, topic: str, field_name: str, field_prompt: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"{context}\n\n### TOPIC ###\n{topic}\n\n### FIELD: {field_name} ###\n{field_prompt}",
        },
    ]
    try:
        response = await mc.complete(messages, model=mc.model_chat)
        return (response.choices[0].message.content or "").strip()
    except Exception as ex:
        logger.warning("[kb_report] field %s failed: %s", field_name, ex)
        return _NOT_SUPPORTED


def _dedupe_citations(citations: list[dict]) -> list[dict]:
    seen = set()
    deduped = []
    for c in citations:
        key = (c["cite_key"], c["section_title"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped


def _resolve_web_citations(text: str, available_urls: set[str]) -> list[str]:
    """Same "a model can't cite something it wasn't shown" guard
    kb.resolve_citations applies to [cite_key · section] tags, applied to
    (web: <url>) tags instead -- a URL only counts if it's one this call's
    own verified web search actually returned."""
    found = []
    for raw in _WEB_CITE_RE.findall(text):
        url = raw.rstrip(").,;")
        if url in available_urls and url not in found:
            found.append(url)
    return found


async def generate_report(db_path: str, tenant_id: str, topic: str) -> dict:
    report = {
        "topic": topic,
        "generated_at": datetime.utcnow().isoformat(),
        **{field: "" for field in _SECTIONS},
        **{field: [] for field in _LIST_SECTIONS},
        "five_whys": [],
        "similar_documents": [],
        "citations": [],
        "document_index": None,
        "affected_services": {"direct": [], "also_potentially_affected": []},
        "unsupported_fields": list(_SECTIONS) + list(_LIST_SECTIONS),
    }

    # A "how many / list every" question about the knowledge base itself is not
    # a report to compile field-by-field -- it is the document index, exactly
    # like search_knowledge_base's own aggregate route. Answering it from
    # retrieved chunks instead (the bug this fixes) means whatever document
    # happens to score highest gets asked to explain a question it has
    # nothing to do with.
    if kb.classify_question(topic) == "aggregate":
        report["document_index"] = await asyncio.to_thread(kb.render_document_index, db_path, tenant_id)
        report["unsupported_fields"] = []
        return report

    search_payload = await asyncio.to_thread(kb.search, db_path, tenant_id, topic, RETRIEVAL_K)
    results = search_payload["results"]

    # The same relevance gate search_knowledge_base uses to flag a knowledge
    # gap: nothing retrieved is actually about this topic. Without this gate,
    # top-K still returns the least-bad chunks in the whole knowledge base
    # even when none of them are relevant, and per-field prompting on
    # unrelated content is exactly what produced a report about an RDS
    # connection-pool incident for a question about an ALB 5xx spike.
    if not results or not search_payload["relevant"]:
        return report

    # Deterministic, not model-generated: the services a cited document itself
    # declared (its own frontmatter, or extracted at ingestion time for a doc
    # that had none -- see knowledge_base.learn_service_dependencies), plus
    # what the auto-learned dependency graph says would ALSO be affected.
    # Runs even with no model configured, since it needs neither.
    direct_services = sorted({
        str(s).strip().lower()
        for r in results for s in (r.get("document_metadata") or {}).get("services") or []
        if str(s).strip()
    })
    also_affected: set[str] = set()
    for service in direct_services:
        also_affected.update(await asyncio.to_thread(kb.downstream_of, db_path, tenant_id, service))
    report["affected_services"] = {
        "direct": direct_services,
        "also_potentially_affected": sorted(also_affected - set(direct_services)),
    }

    context = "\n\n".join(f"{r['citation_tag']}\n{r['text']}" for r in results)
    mc = ModelClient()
    if not mc.available:
        return report

    unsupported: list[str] = []
    all_citations: list[dict] = []

    async def fill_single(field: str, prompt: str) -> None:
        text = await _ask_field(mc, context, topic, field, prompt)
        if _NOT_SUPPORTED in text.upper():
            unsupported.append(field)
            return
        citations = kb.resolve_citations(text, results)
        if not citations:
            unsupported.append(field)
            return
        report[field] = text
        all_citations.extend(citations)

    async def fill_list(field: str, prompt: str) -> None:
        text = await _ask_field(mc, context, topic, field, prompt)
        if _NOT_SUPPORTED in text.upper():
            unsupported.append(field)
            return
        citations = kb.resolve_citations(text, results)
        if not citations:
            unsupported.append(field)
            return
        report[field] = [line.strip(" -*") for line in text.splitlines() if line.strip()]
        all_citations.extend(citations)

    await asyncio.gather(
        *(fill_single(field, prompt) for field, prompt in _SECTIONS.items()),
        *(fill_list(field, prompt) for field, prompt in _LIST_SECTIONS.items()),
    )

    if report.get("root_cause"):
        why_text = await _ask_field(
            mc, context, topic, "five_whys",
            "Up to 5 'why -> because' pairs drilling into the root cause, one per line, "
            "each formatted exactly as 'Why: <question> Because: <answer>'.",
        )
        if _NOT_SUPPORTED not in why_text.upper():
            for line in why_text.splitlines():
                if "Because:" not in line:
                    continue
                why_part, _, because_part = line.partition("Because:")
                because = because_part.strip()
                citations = kb.resolve_citations(line, results)
                if not because or not citations:
                    continue
                report["five_whys"].append({
                    "why": why_part.replace("Why:", "").strip(),
                    "because": because,
                    "citations": citations,
                })
                all_citations.extend(citations)

    all_citations = _dedupe_citations(all_citations)
    report["citations"] = all_citations
    report["similar_documents"] = sorted({c["document_title"] for c in all_citations})
    report["unsupported_fields"] = unsupported
    return report


# Web-search result count for one "Perform Research" run -- generous enough
# to cover a topic from a couple of angles, small enough to stay a handful
# of seconds of Tavily + verification, not a scrape job.
GAP_RESEARCH_RESULTS = 6


async def generate_report_from_web(topic: str) -> dict:
    """Same report shape as generate_report, synthesized from verified web
    search results instead of the knowledge base. Exists specifically for a
    knowledge gap: by definition the knowledge base has nothing on this
    topic (that is what made it a gap), so generate_report's own relevance
    gate would just return an empty report -- there is nothing here for
    that function to retrieve. Never touches the knowledge base or creates
    anything; the caller (run_gap_research) decides what to do with the
    result."""
    report = {
        "topic": topic,
        "generated_at": datetime.utcnow().isoformat(),
        **{field: "" for field in _SECTIONS},
        **{field: [] for field in _LIST_SECTIONS},
        "five_whys": [],
        "similar_documents": [],
        "citations": [],
        "document_index": None,
        "affected_services": {"direct": [], "also_potentially_affected": []},
        "unsupported_fields": list(_SECTIONS) + list(_LIST_SECTIONS),
        "general_summary": "",
    }

    results = await asyncio.to_thread(web_search_client.search, topic, GAP_RESEARCH_RESULTS)
    if not results:
        return report
    verified = [web_search_client.verify_result(r) for r in results]
    available_urls = {r["url"] for r in verified}
    context = "\n\n".join(
        f"(web: {r['url']}) [{'verified' if r['verified'] else 'unverified'}, "
        f"authority: {r['authority']}]\n{r['snippet']}"
        for r in verified
    )

    # Mechanical namesake guard -- see _context_covers_topic. If nothing
    # fetched even mentions every distinguishing word of the topic, this is
    # search noise (a namesake), not an answer, regardless of how plausible
    # the model might otherwise make it sound. unsupported_fields is already
    # every field from the report dict above, so returning here reports
    # honestly that nothing relevant was found instead of risking a
    # confidently-wrong answer about the wrong person/entity.
    if not _context_covers_topic(context, topic):
        return report

    mc = ModelClient()
    if not mc.available:
        return report

    unsupported: list[str] = []
    all_citations: list[str] = []

    async def fill_single(field: str, prompt: str) -> None:
        text = await _ask_field(mc, context, topic, field, prompt, system_prompt=WEB_SYSTEM_PROMPT)
        if _NOT_SUPPORTED in text.upper():
            unsupported.append(field)
            return
        citations = _resolve_web_citations(text, available_urls)
        if not citations:
            unsupported.append(field)
            return
        report[field] = text
        all_citations.extend(citations)

    async def fill_list(field: str, prompt: str) -> None:
        text = await _ask_field(mc, context, topic, field, prompt, system_prompt=WEB_SYSTEM_PROMPT)
        if _NOT_SUPPORTED in text.upper():
            unsupported.append(field)
            return
        citations = _resolve_web_citations(text, available_urls)
        if not citations:
            unsupported.append(field)
            return
        report[field] = [line.strip(" -*") for line in text.splitlines() if line.strip()]
        all_citations.extend(citations)

    await asyncio.gather(
        *(fill_single(field, prompt) for field, prompt in _SECTIONS.items()),
        *(fill_list(field, prompt) for field, prompt in _LIST_SECTIONS.items()),
    )

    if report.get("root_cause"):
        why_text = await _ask_field(
            mc, context, topic, "five_whys",
            "Up to 5 'why -> because' pairs drilling into the root cause, one per line, "
            "each formatted exactly as 'Why: <question> Because: <answer>'.",
            system_prompt=WEB_SYSTEM_PROMPT,
        )
        if _NOT_SUPPORTED not in why_text.upper():
            for line in why_text.splitlines():
                if "Because:" not in line:
                    continue
                why_part, _, because_part = line.partition("Because:")
                because = because_part.strip()
                citations = _resolve_web_citations(line, available_urls)
                if not because or not citations:
                    continue
                report["five_whys"].append({
                    "why": why_part.replace("Why:", "").strip(),
                    "because": because,
                    "citations": citations,
                })
                all_citations.extend(citations)

    # Every RCA-shaped field came back unsupported -- try once more with a
    # plain, unstructured question instead of assuming the topic itself has
    # no answer. See GENERAL_WEB_SYSTEM_PROMPT for why this is needed at all.
    if len(unsupported) == len(_SECTIONS) + len(_LIST_SECTIONS):
        summary_text = await _ask_field(
            mc, context, topic, "answer",
            "Answer the topic directly, in a short paragraph or two.",
            system_prompt=GENERAL_WEB_SYSTEM_PROMPT,
        )
        if _NOT_SUPPORTED not in summary_text.upper():
            summary_citations = _resolve_web_citations(summary_text, available_urls)
            if summary_citations:
                report["general_summary"] = summary_text
                all_citations.extend(summary_citations)
                # Don't also list all 8 RCA fields as "not supported" right next
                # to a real answer -- that section exists to explain a partial
                # report's gaps, not to shadow a complete answer of a different shape.
                unsupported = []

    report["citations"] = sorted(set(all_citations))
    report["unsupported_fields"] = unsupported
    return report


async def run_gap_research(db_path: str, tenant_id: str, gap_id: str, question: str, created_by: str) -> None:
    """Background-task body for the "Perform Research" action: synthesizes a
    report from web search (see generate_report_from_web) and stores it as a
    pending_approval document the RCA docs tab can show -- even when
    research found nothing usable, since render_markdown's own "nothing
    relevant" text is exactly what an admin needs to see to reject it
    cleanly. On an actual exception (model unavailable, unexpected error),
    reverts the gap to 'open' instead of leaving it stuck in 'researching'
    with nothing to show for it -- the user can just try again."""
    try:
        report = await generate_report_from_web(question)
        markdown = render_markdown(report, heading=f"Research: {question}")
        kb.create_document(
            db_path, tenant_id, f"research-{gap_id[:8]}.md", markdown,
            uploaded_by=f"web research, requested by {created_by}",
            status="pending_approval", gap_id=gap_id,
        )
    except Exception as ex:
        logger.exception("[kb_report] gap research failed for gap %s", gap_id)
        try:
            kb.set_gap_status(db_path, gap_id, "open")
        except Exception:
            pass


def render_markdown(report: dict, heading: str | None = None) -> str:
    heading = heading or f"Knowledge base report: {report['topic']}"
    lines = [f"# {heading}", ""]

    if report.get("document_index"):
        lines += ["## Document index", "", report["document_index"]]
        return "\n".join(lines).strip()

    affected = report.get("affected_services") or {}
    has_affected = bool(affected.get("direct") or affected.get("also_potentially_affected"))
    has_any_field = (
        any(report.get(f) for f in _SECTIONS) or any(report.get(f) for f in _LIST_SECTIONS)
        or has_affected or report.get("general_summary")
    )
    if not has_any_field:
        lines.append(
            "_Nothing relevant enough to this topic was found to report on. "
            "Try a different phrasing, or check what has actually been uploaded._"
        )
        return "\n".join(lines).strip()

    # The topic didn't fit the RCA-report shape below (not every knowledge
    # gap is an incident) -- see GENERAL_WEB_SYSTEM_PROMPT. This is the whole
    # answer in that case, so it leads the document rather than sitting under
    # a field heading that wouldn't apply to it.
    if report.get("general_summary"):
        lines += [report["general_summary"], ""]

    if has_affected:
        lines.append("## Affected services")
        if affected.get("direct"):
            lines.append(f"**Directly involved:** {', '.join(affected['direct'])}")
        if affected.get("also_potentially_affected"):
            lines.append(
                f"**Also potentially affected (learned from past incidents):** "
                f"{', '.join(affected['also_potentially_affected'])}"
            )
        lines.append("")

    for field in _SECTIONS:
        if report.get(field):
            lines += [f"## {field.replace('_', ' ').title()}", report[field], ""]
    for field in _LIST_SECTIONS:
        items = report.get(field) or []
        if items:
            lines += [f"## {field.replace('_', ' ').title()}"]
            lines += [f"- {item}" for item in items]
            lines.append("")
    if report.get("five_whys"):
        lines.append("## Five Whys")
        for w in report["five_whys"]:
            lines.append(f"- **Why**: {w['why']} — **Because**: {w['because']}")
        lines.append("")
    if report.get("similar_documents"):
        lines.append("## Similar documents")
        lines += [f"- {d}" for d in report["similar_documents"]]
        lines.append("")
    if report.get("unsupported_fields"):
        lines.append("## Not supported by the knowledge base")
        lines += [f"- {f}" for f in report["unsupported_fields"]]
    return "\n".join(lines).strip()