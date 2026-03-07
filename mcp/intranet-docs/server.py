"""STDIO MCP server for reading locally hosted intranet-style API docs."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Sequence
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


def extract_embedded_openapi_spec(html: str) -> dict[str, Any] | None:
    """Extract an inline OpenAPI JSON object from the docs page when present."""

    match = re.search(
        r"window\.apiDocs\s*=\s*(\{.*?\})\s*(?:;)?\s*</script>",
        html,
        flags=re.DOTALL,
    )
    if not match:
        return None

    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    return payload if isinstance(payload, dict) else None


def normalize_stoplight_path(path: str) -> str:
    """Normalize an OpenAPI path to the hash-route form used by Stoplight Elements."""

    return path.strip().lstrip("/").replace("/", "-").lower()


def resolve_operation_from_hash(
    spec: dict[str, Any],
    hash_fragment: str,
) -> tuple[str, str, dict[str, Any]] | None:
    """Resolve a Stoplight hash route like #/paths/foo-bar/get to an OpenAPI operation."""

    route = hash_fragment.removeprefix("#").strip("/")
    parts = [part for part in route.split("/") if part]
    if len(parts) < 3 or parts[0] != "paths":
        return None

    method = parts[-1].lower()
    route_path = "/".join(parts[1:-1]).lower()
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return None

    for candidate_path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        if normalize_stoplight_path(candidate_path) != route_path:
            continue

        operation = path_item.get(method)
        if isinstance(operation, dict):
            return candidate_path, method, operation

    return None


def format_schema_type(schema: Any) -> str:
    """Render a compact schema type label for parameters, bodies, and headers."""

    if not isinstance(schema, dict):
        return "object"

    schema_type = schema.get("type")
    schema_format = schema.get("format")
    ref = schema.get("$ref")
    enum_values = schema.get("enum")

    if isinstance(ref, str) and ref:
        return ref.rsplit("/", 1)[-1]

    if schema_type == "array":
        item_type = format_schema_type(schema.get("items", {}))
        return f"array<{item_type}>"

    if enum_values and isinstance(enum_values, list):
        return "enum"

    if schema_type and schema_format:
        return f"{schema_type}<{schema_format}>"
    if schema_type:
        return str(schema_type)

    return "object"


def format_parameter_lines(parameters: list[dict[str, Any]]) -> list[str]:
    """Render OpenAPI parameters grouped by their location."""

    groups = (
        ("query", "Query Parameters"),
        ("path", "Path Parameters"),
        ("header", "Header Parameters"),
        ("cookie", "Cookie Parameters"),
    )
    lines: list[str] = []

    for location, title in groups:
        current = [param for param in parameters if param.get("in") == location]
        if not current:
            continue

        lines.extend([f"### {title}", ""])
        for parameter in current:
            name = parameter.get("name", "(unnamed)")
            schema = parameter.get("schema", {})
            required = " required" if parameter.get("required") else ""
            description = clean_text(str(parameter.get("description", "")))
            lines.append(f"- `{name}` `{format_schema_type(schema)}`{required}")
            if description:
                lines.append(f"  {description}")
        lines.append("")

    return lines


def format_request_body_lines(request_body: Any) -> list[str]:
    """Render a compact OpenAPI request body section."""

    if not isinstance(request_body, dict):
        return []

    content = request_body.get("content")
    if not isinstance(content, dict) or not content:
        return []

    lines = ["### Request Body", ""]
    for media_type, media_spec in content.items():
        schema = media_spec.get("schema", {}) if isinstance(media_spec, dict) else {}
        lines.append(f"- `{media_type}` `{format_schema_type(schema)}`")
    lines.append("")
    return lines


def format_response_lines(responses: Any) -> list[str]:
    """Render OpenAPI responses including headers."""

    if not isinstance(responses, dict) or not responses:
        return []

    lines = ["## Responses", ""]
    for status_code, response in responses.items():
        if not isinstance(response, dict):
            continue

        description = clean_text(str(response.get("description", "")))
        lines.append(f"### {status_code}")
        if description:
            lines.extend([description, ""])

        headers = response.get("headers")
        if isinstance(headers, dict) and headers:
            lines.extend(["#### Headers", ""])
            for header_name, header in headers.items():
                if not isinstance(header, dict):
                    continue
                schema = header.get("schema", {})
                header_description = clean_text(str(header.get("description", "")))
                lines.append(f"- `{header_name}` `{format_schema_type(schema)}`")
                if header_description:
                    lines.append(f"  {header_description}")
            lines.append("")

    return lines


def render_openapi_operation_markdown(
    path: str,
    method: str,
    operation: dict[str, Any],
) -> str:
    """Render a single OpenAPI operation as readable Markdown."""

    summary = clean_text(str(operation.get("summary", ""))) or f"{method.upper()} {path}"
    description = clean_text(str(operation.get("description", "")))
    parameters = operation.get("parameters")
    responses = operation.get("responses")
    request_body = operation.get("requestBody")

    lines = [f"# {summary}", "", method.lower(), path, ""]
    if description:
        lines.extend([description, ""])

    request_sections = []
    if isinstance(parameters, list):
        request_sections.extend(format_parameter_lines(parameters))
    request_sections.extend(format_request_body_lines(request_body))
    if request_sections:
        lines.extend(["## Request", ""])
        lines.extend(request_sections)

    lines.extend(format_response_lines(responses))
    return clean_text("\n".join(lines))


def render_openapi_spec_markdown(spec: dict[str, Any]) -> str:
    """Render all operations from an embedded OpenAPI spec as readable Markdown."""

    info = spec.get("info") if isinstance(spec.get("info"), dict) else {}
    title = clean_text(str(info.get("title", ""))) or "API Docs"
    description = clean_text(str(info.get("description", "")))
    version = clean_text(str(info.get("version", "")))
    paths = spec.get("paths")

    lines = [f"# {title}", ""]
    if description:
        lines.extend([description, ""])
    if version:
        lines.extend([f"- version: `{version}`", ""])

    rendered_operations: list[str] = []
    if isinstance(paths, dict):
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for method in ("get", "post", "put", "patch", "delete", "head", "options", "trace"):
                operation = path_item.get(method)
                if not isinstance(operation, dict):
                    continue
                rendered_operations.append(
                    render_openapi_operation_markdown(path=path, method=method, operation=operation)
                )

    if rendered_operations:
        lines.extend(["## Endpoints", ""])
        lines.append("\n\n".join(rendered_operations))

    return clean_text("\n".join(lines))


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


def extract_html_content(html: str, hash_fragment: str) -> tuple[str, str]:
    """Extract the most useful readable content from an HTML docs page."""

    spec = extract_embedded_openapi_spec(html)
    if spec:
        resolved = resolve_operation_from_hash(spec, hash_fragment)
        if resolved:
            path, method, operation = resolved
            return (
                render_openapi_operation_markdown(path=path, method=method, operation=operation),
                "Resolved the requested hash route from the embedded OpenAPI spec.",
            )
        if not hash_fragment:
            return (
                render_openapi_spec_markdown(spec),
                "Rendered all operations from the embedded OpenAPI spec.",
            )

    readable = html_to_markdown_like(html)
    return extract_hash_focused_text(readable, hash_fragment)


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
        response.raise_for_status()

    content_type = normalize_content_type(response.headers.get("content-type", ""))
    if content_type == "text/html":
        readable, hash_note = extract_html_content(response.text, hash_fragment)
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
        hash_note = ""
    else:
        readable = clean_text(response.text)
        hash_note = ""

    focused_text = readable
    if content_type != "text/html":
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
