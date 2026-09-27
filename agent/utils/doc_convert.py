"""Converts an uploaded knowledge-base file to markdown before storage.

tools/knowledge_base.py's title extraction and chunking (chunk_text,
_extract_title) both assume markdown -- an optional leading "# Title" and
"##" section headers -- so every non-markdown upload funnels through here
once, rather than teaching the KB pipeline five raw formats.
"""
import base64
import csv
import io
import logging
import os
import tempfile

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from openpyxl import load_workbook
from pypdf import PdfReader

logger = logging.getLogger("uvicorn.error")

# CSV event logs are still plain .csv on disk -- "csv event logs" in the
# upload UI is a labeling hint for admins, not a distinct format here.
SUPPORTED_EXTENSIONS = (".md", ".txt", ".pdf", ".docx", ".xlsx", ".csv")

# Model used for the LLM-OCR fallback below -- falls back through the same
# per-role chain model_client.py uses, so a fresh deployment needs no new
# config beyond what the agents already require. No string default: an unset
# model must mean "OCR unavailable", not a model id that 404s on every upload.
def _ocr_model() -> str:
    return (
        os.getenv("BEDROCK_MODEL_OCR")
        or os.getenv("BEDROCK_MODEL_CODEGEN")
        or os.getenv("BEDROCK_MODEL")
        or ""
    ).strip()

_DOCX_HEADING_LEVELS = {f"Heading {n}": n for n in range(1, 7)}


def convert_to_markdown(filename: str, raw: bytes) -> str:
    """Raises ValueError with a user-facing message on unsupported/corrupt input."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("md", "txt"):
        return _decode_text(raw)
    if ext == "pdf":
        return _pdf_to_markdown(raw)
    if ext == "docx":
        return _docx_to_markdown(raw)
    if ext == "xlsx":
        return _xlsx_to_markdown(raw)
    if ext == "csv":
        return _csv_to_markdown(_decode_text(raw))
    raise ValueError(f"Unsupported file type: .{ext or '?'}")


def _decode_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("File must be UTF-8 text.")


def _pdf_to_markdown(raw: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as ex:
        raise ValueError(f"Could not read PDF: {ex}")
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            pages.append(f"## Page {i}\n\n{text}")
    if pages:
        return "\n\n".join(pages)
    # pypdf found no text layer at all -- likely scanned/image-only. Fall back
    # to LLM OCR rather than failing outright.
    return _pdf_ocr_fallback(raw)


def _ocr_llm_client():
    """A minimal Bedrock-backed stand-in for the OpenAI SDK client markitdown
    expects. markitdown only ever calls `.chat.completions.create(model=,
    messages=)` and reads `.choices[0].message.content`, so duck-typing that one
    method is the whole integration -- and the response shape already exists in
    utils.bedrock, built for the agents.

    Returns None when no model or region is configured, so an unconfigured
    environment degrades to the same "unsupported" error as before rather than
    crashing."""
    region = (os.getenv("BEDROCK_REGION") or os.getenv("AWS_REGION") or "").strip()
    if not region or not _ocr_model():
        return None
    return _BedrockChatShim(region)


class _BedrockChatShim:
    """`client.chat.completions.create(...)` over Converse."""

    def __init__(self, region: str):
        import boto3
        self._client = boto3.client("bedrock-runtime", region_name=region)
        self.chat = _BedrockChatNamespace(self)

    def create(self, model: str, messages: list, **kwargs):
        from utils.bedrock import from_converse

        system, turns = [], []
        for msg in messages:
            content = msg.get("content")
            if msg.get("role") == "system":
                system.append({"text": content if isinstance(content, str) else str(content)})
                continue
            turns.append({
                "role": "assistant" if msg.get("role") == "assistant" else "user",
                "content": _to_converse_content(content),
            })

        args = {"modelId": model, "messages": turns,
                "inferenceConfig": {"maxTokens": int(kwargs.get("max_tokens") or 4096)}}
        if system:
            args["system"] = system
        return from_converse(self._client.converse(**args))


class _BedrockChatNamespace:
    """Just enough nesting for `.chat.completions.create`."""

    def __init__(self, shim):
        self.completions = shim


# Converse names the format explicitly rather than reading the data URI's MIME
# type, and accepts only these four.
_IMAGE_FORMATS = {"png": "png", "jpeg": "jpeg", "jpg": "jpeg",
                  "gif": "gif", "webp": "webp"}


def _to_converse_content(content) -> list:
    """OpenAI content parts -> Converse blocks. markitdown sends a text prompt
    plus one base64 data-URI image per page."""
    if isinstance(content, str):
        return [{"text": content}]

    blocks = []
    for part in content or []:
        if not isinstance(part, dict):
            blocks.append({"text": str(part)})
            continue
        if part.get("type") == "text":
            text = part.get("text") or ""
            if text.strip():
                blocks.append({"text": text})
            continue
        if part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url") or ""
            if not url.startswith("data:"):
                # Converse takes raw bytes, never a remote URL -- skipping is
                # better than sending a block the API will reject outright.
                logger.warning("[doc_convert] skipping non-data image URL in OCR request")
                continue
            header, _, payload = url.partition(",")
            subtype = header.split(";")[0].split("/")[-1].lower()
            fmt = _IMAGE_FORMATS.get(subtype)
            if not fmt:
                logger.warning("[doc_convert] unsupported OCR image format %r", subtype)
                continue
            blocks.append({"image": {"format": fmt,
                                     "source": {"bytes": base64.b64decode(payload)}}})
    return blocks or [{"text": "Transcribe this document."}]


def _pdf_ocr_fallback(raw: bytes) -> str:
    client = _ocr_llm_client()
    if client is None:
        raise ValueError(
            "No extractable text found in that PDF (scanned/image-only), and "
            "LLM OCR isn't available (BEDROCK_MODEL_OCR/BEDROCK_REGION not configured)."
        )
    from markitdown import MarkItDown
    md = MarkItDown(enable_plugins=True, llm_client=client, llm_model=_ocr_model())

    # markitdown converts from a file path, not raw bytes -- write to a temp
    # file for the duration of the call and clean up unconditionally after.
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        result = md.convert(tmp_path)
    except Exception as ex:
        logger.warning("[doc_convert] LLM OCR failed: %s", ex)
        raise ValueError(f"OCR could not read that PDF: {ex}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    text = (result.text_content or "").strip()
    if not text:
        raise ValueError("OCR found no readable content in that PDF.")
    return text


def _iter_docx_blocks(document):
    """Paragraphs and tables in true document order -- python-docx's own
    .paragraphs/.tables split them into two separate lists."""
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield DocxParagraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield DocxTable(child, document)


def _docx_to_markdown(raw: bytes) -> str:
    try:
        document = Document(io.BytesIO(raw))
    except Exception as ex:
        raise ValueError(f"Could not read Word document: {ex}")
    blocks = []
    for block in _iter_docx_blocks(document):
        if isinstance(block, DocxParagraph):
            text = block.text.strip()
            if not text:
                continue
            style_name = block.style.name if block.style else ""
            level = _DOCX_HEADING_LEVELS.get(style_name)
            blocks.append(f"{'#' * level} {text}" if level else text)
        else:
            rows = [[cell.text.strip() for cell in row.cells] for row in block.rows]
            table_md = _rows_to_markdown_table(rows)
            if table_md:
                blocks.append(table_md)
    text = "\n\n".join(blocks)
    if not text.strip():
        raise ValueError("No extractable text found in that Word document.")
    return text


def _xlsx_to_markdown(raw: bytes) -> str:
    try:
        workbook = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
    except Exception as ex:
        raise ValueError(f"Could not read Excel workbook: {ex}")
    sections = []
    for sheet in workbook.worksheets:
        rows = [
            ["" if cell is None else str(cell) for cell in row]
            for row in sheet.iter_rows(values_only=True)
            if any(cell is not None for cell in row)
        ]
        table_md = _rows_to_markdown_table(rows)
        if table_md:
            sections.append(f"## {sheet.title}\n\n{table_md}")
    if not sections:
        raise ValueError("No data found in that Excel workbook.")
    return "\n\n".join(sections)


def _csv_to_markdown(text: str) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    table_md = _rows_to_markdown_table(rows)
    if not table_md:
        raise ValueError("No data found in that CSV file.")
    return table_md


def _rows_to_markdown_table(rows: list) -> str:
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    padded = [list(r) + [""] * (width - len(r)) for r in rows]
    escaped = [[str(c).replace("|", "\\|").replace("\n", " ").strip() for c in r] for r in padded]
    header, *body = escaped
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n".join(lines)
