"""STDIO MCP server for reading locally hosted intranet-style API docs."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urldefrag

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

DEFAULT_ALLOWED_PREFIXES: tuple[str, ...] = (
    "http://127.0.0.1:8000/",
    "http://localhost:8000/",
)
DEFAULT_TIMEOUT_SEC = 15
DEFAULT_MAX_CHARS = 20_000
DEFAULT_OUTPUT_FORMAT = "markdown"
SUPPORTED_OUTPUT_FORMATS = {"markdown", "text"}

mcp = FastMCP("intranet-docs")


@dataclass(frozen=True)
class FetchResult:
    """Normalized fetch output before final formatting."""

    requested_url: str
    fetched_url: str
    hash_fragment: str
    status_code: int
    content_type: str
    body_text: str
    hash_note: str


def get_allowed_prefixes() -> tuple[str, ...]:
    """Return sanitized URL allowlist prefixes from env or defaults."""

    raw = os.environ.get("INTRANET_ALLOWED_PREFIXES", "")
    if not raw.strip():
        return DEFAULT_ALLOWED_PREFIXES

    prefixes = tuple(part.strip() for part in raw.split(",") if part.strip())
    return prefixes or DEFAULT_ALLOWED_PREFIXES


def split_url_and_hash(url: str) -> tuple[str, str]:
    """Split a URL into the fetchable base URL and optional hash fragment."""

    base_url, fragment = urldefrag(url.strip())
    hash_fragment = f"#{fragment}" if fragment else ""
    return base_url, hash_fragment


def validate_allowed_url(url: str, allowed_prefixes: Sequence[str]) -> str | None:
    """Return an error message when the URL is not inside the allowlist."""

    if not url:
        return "URL is required."

    if not any(url.startswith(prefix) for prefix in allowed_prefixes):
        prefixes = ", ".join(allowed_prefixes)
        return (
            "URL is not allowed. "
            f"Allowed prefixes: {prefixes}. "
            f"Received: {url}"
        )

    return None


def normalize_content_type(content_type: str) -> str:
    """Strip content-type parameters for simpler downstream checks."""

    return content_type.split(";", 1)[0].strip().lower()


def clean_text(text: str) -> str:
    """Normalize whitespace while preserving paragraph-like breaks."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_to_markdown_like(html: str) -> str:
    """Convert HTML into Markdown-friendly readable text."""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    lines: list[str] = []
    seen = set()
    block_tags = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "pre", "code", "table", "tr")

    for tag in soup.find_all(block_tags):
        text = tag.get_text(" ", strip=True)
        if not text:
            continue

        if tag.name and tag.name.startswith("h") and len(tag.name) == 2 and tag.name[1].isdigit():
            level = int(tag.name[1])
            rendered = f"{'#' * level} {text}"
        elif tag.name == "li":
            rendered = f"- {text}"
        elif tag.name in {"pre", "code"}:
            rendered = f"```\n{text}\n```"
        else:
            rendered = text

        rendered = clean_text(rendered)
        if rendered and rendered not in seen:
            seen.add(rendered)
            lines.append(rendered)

    if not lines:
        fallback_text = soup.get_text("\n", strip=True)
        return clean_text(fallback_text)

    return clean_text("\n\n".join(lines))


def extract_hash_focused_text(body_text: str, hash_fragment: str) -> tuple[str, str]:
    """Try to narrow the output to the requested hash route when possible."""

    if not hash_fragment:
        return body_text, ""

    route_hint = hash_fragment.removeprefix("#").strip("/")
    if not route_hint:
        return body_text, ""

    route_parts = [part for part in route_hint.split("/") if part]
    lowered_lines = body_text.splitlines()

    best_index = -1
    best_score = 0
    for index, line in enumerate(lowered_lines):
        haystack = line.lower()
        score = sum(1 for part in route_parts if part.lower() in haystack)
        if score > best_score:
            best_score = score
            best_index = index

    if best_index >= 0 and best_score >= max(2, min(len(route_parts), 3)):
        start = max(0, best_index - 8)
        end = min(len(lowered_lines), best_index + 20)
        snippet = clean_text("\n".join(lowered_lines[start:end]))
        note = (
            "Located content related to the requested hash route and returned a focused snippet."
        )
        return snippet or body_text, note

    note = (
        "Requested hash route was preserved, but the HTML content could not be mapped precisely "
        "to that fragment. Returning readable content from the base page."
    )
    return body_text, note


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate text to the given size with a clear marker."""

    if max_chars <= 0:
        return "", bool(text)

    if len(text) <= max_chars:
        return text, False

    marker = "\n\n[TRUNCATED]"
    keep = max(0, max_chars - len(marker))
    return f"{text[:keep].rstrip()}{marker}", True


def format_output(result: FetchResult, truncated: bool, output_format: str) -> str:
    """Render MCP tool output as readable Markdown or plain text."""

    hash_value = result.hash_fragment or "(none)"
    note_value = result.hash_note or "(none)"

    if output_format == "text":
        header = "\n".join(
            [
                f"requested_url: {result.requested_url}",
                f"fetched_url: {result.fetched_url}",
                f"hash_fragment: {hash_value}",
                f"status_code: {result.status_code}",
                f"content_type: {result.content_type}",
                f"truncated: {str(truncated).lower()}",
                f"hash_note: {note_value}",
                "",
            ]
        )
        return f"{header}{result.body_text}".strip()

    metadata = [
        "# Intranet Doc Fetch Result",
        "",
        f"- requested_url: `{result.requested_url}`",
        f"- fetched_url: `{result.fetched_url}`",
        f"- hash_fragment: `{hash_value}`",
        f"- status_code: `{result.status_code}`",
        f"- content_type: `{result.content_type}`",
        f"- truncated: `{str(truncated).lower()}`",
        f"- hash_note: {note_value}",
        "",
        "## Content",
        "",
    ]
    return "\n".join(metadata) + result.body_text


def format_error(message: str, requested_url: str = "") -> str:
    """Return a stable human-readable error block instead of a traceback."""

    lines = [
        "# Intranet Doc Fetch Error",
        "",
        f"- requested_url: `{requested_url or '(empty)'}`",
        f"- error: {message}",
    ]
    return "\n".join(lines)


def fetch_document(
    url: str,
    timeout_sec: int,
) -> FetchResult:
    """Fetch and normalize a supported local documentation page."""

    allowed_prefixes = get_allowed_prefixes()
    base_url, hash_fragment = split_url_and_hash(url)

    error = validate_allowed_url(base_url, allowed_prefixes)
    if error:
        raise ValueError(error)

    timeout = httpx.Timeout(timeout_sec)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(base_url)

    content_type = normalize_content_type(response.headers.get("content-type", ""))
    if content_type == "text/html":
        readable = html_to_markdown_like(response.text)
    elif content_type in {"application/json", "text/plain"}:
        raw_text = response.text
        if content_type == "application/json":
            try:
                readable = json.dumps(response.json(), ensure_ascii=False, indent=2)
            except json.JSONDecodeError:
                readable = raw_text
        else:
            readable = raw_text
        readable = clean_text(readable)
    else:
        readable = clean_text(response.text)

    focused_text, hash_note = extract_hash_focused_text(readable, hash_fragment)
    if hash_fragment and not hash_note:
        hash_note = "Requested hash route detected."

    return FetchResult(
        requested_url=url,
        fetched_url=str(response.url),
        hash_fragment=hash_fragment,
        status_code=response.status_code,
        content_type=content_type or "(unknown)",
        body_text=focused_text,
        hash_note=hash_note,
    )


@mcp.tool()
def fetch_intranet_doc(
    url: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    max_chars: int = DEFAULT_MAX_CHARS,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
) -> str:
    """Fetch local API documentation content and return readable text for Codex."""

    normalized_format = output_format.strip().lower() or DEFAULT_OUTPUT_FORMAT
    if normalized_format not in SUPPORTED_OUTPUT_FORMATS:
        return format_error(
            "Invalid output_format. Supported values: markdown, text.",
            requested_url=url,
        )

    try:
        result = fetch_document(url=url, timeout_sec=timeout_sec)
        truncated_body, truncated = truncate_text(result.body_text, max_chars)
        rendered = format_output(
            FetchResult(
                requested_url=result.requested_url,
                fetched_url=result.fetched_url,
                hash_fragment=result.hash_fragment,
                status_code=result.status_code,
                content_type=result.content_type,
                body_text=truncated_body,
                hash_note=result.hash_note,
            ),
            truncated=truncated,
            output_format=normalized_format,
        )
        return rendered
    except ValueError as exc:
        return format_error(str(exc), requested_url=url)
    except httpx.TimeoutException:
        return format_error(
            f"Request timed out after {timeout_sec} seconds.",
            requested_url=url,
        )
    except httpx.HTTPError as exc:
        return format_error(f"HTTP request failed: {exc}", requested_url=url)
    except Exception as exc:  # pragma: no cover - defensive fallback
        return format_error(f"Unexpected error: {exc}", requested_url=url)


if __name__ == "__main__":
    try:
        mcp.run(transport="stdio")
    except TypeError:
        mcp.run()
