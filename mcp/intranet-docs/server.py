"""STDIO MCP server for reading locally hosted intranet-style API docs."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Sequence
from urllib.parse import urlparse, urldefrag, urljoin

import httpx
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

DEFAULT_ALLOWED_PREFIXES: tuple[str, ...] = (
    "http://127.0.0.1:8000/",
    "http://localhost:8000/",
)
DEFAULT_DOCS_BASE_URL = DEFAULT_ALLOWED_PREFIXES[0]
DEFAULT_TIMEOUT_SEC = 15
DEFAULT_MAX_CHARS = 20_000
DEFAULT_OUTPUT_FORMAT = "markdown"
DEFAULT_INDEX_TIMEOUT_SEC = 15
SUPPORTED_OUTPUT_FORMATS = {"markdown", "text"}
SUPPORTED_HTTP_METHODS: tuple[str, ...] = (
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
    "trace",
)
NAMESPACED_DOC_SUFFIXES: dict[str, str] = {
    "mapi": "_mapi",
    "xapi": "_xapi",
    "sapi": "_sapi",
}
FALLBACK_FILE_OVERRIDES: tuple[tuple[str, str, str], ...] = (
    ("prefix", "/api/faceverify/", "identityauth.html"),
    ("prefix", "/api/activityoperate/", "activityoperates.html"),
    ("exact", "/open/pagetrace/trace", "tracepage.html"),
    ("exact", "/open/promotioncodestat/trace", "tracepage.html"),
    ("exact", "/open/wx/partnerjsapicallback", "payment.html"),
    ("exact", "/open/wx/transfercallback", "transfer.html"),
    ("exact", "/open/qiwei/support_qrcode", "sysconfig.html"),
    ("prefix", "/xapi/auth/", "merchantauth_xapi.html"),
    ("prefix", "/mapi/wx/", "wxmanage_mapi.html"),
    ("prefix", "/mapi/tagoption/", "tag_mapi.html"),
    ("prefix", "/api/user_member/", "order.html"),
    ("prefix", "/api/user_member_product/", "order.html"),
    ("prefix", "/open/member_type/", "product.html"),
)
PREFERRED_DOC_FILES: tuple[tuple[str, str], ...] = (
    ("/open/discovery/", "discovery.html"),
    ("/sapi/userinfo/", "userinfo_sapi.html"),
)

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


@dataclass(frozen=True)
class DocCandidate:
    """A documentation file candidate for an exact API path."""

    file: str
    methods: tuple[str, ...]


@dataclass(frozen=True)
class DocIndex:
    """Resolved index of API paths to documentation pages."""

    source_url: str
    path_map: dict[str, tuple[DocCandidate, ...]]
    path_lookup: dict[str, str]
    file_map: dict[str, dict[str, tuple[str, ...]]]


@dataclass(frozen=True)
class ResolvedDocLink:
    """Normalized result for API path to documentation URL resolution."""

    input_value: str
    path: str
    file: str
    method: str
    methods: tuple[str, ...]
    doc_url: str
    candidates: tuple[DocCandidate, ...]
    matched_by: str
    warnings: tuple[str, ...]


def get_allowed_prefixes() -> tuple[str, ...]:
    """Return sanitized URL allowlist prefixes from env or defaults."""

    raw = os.environ.get("INTRANET_ALLOWED_PREFIXES", "")
    if not raw.strip():
        return DEFAULT_ALLOWED_PREFIXES

    prefixes = tuple(part.strip() for part in raw.split(",") if part.strip())
    return prefixes or DEFAULT_ALLOWED_PREFIXES


def get_docs_base_url(required: bool = False) -> str:
    """Return the base URL used to construct documentation links."""

    raw = os.environ.get("INTRANET_DOCS_BASE_URL", "").strip()
    if not raw and required:
        raise ValueError(
            "INTRANET_DOCS_BASE_URL is required when resolving an API path or request URL."
        )
    if not raw:
        raw = DEFAULT_DOCS_BASE_URL

    return raw if raw.endswith("/") else f"{raw}/"


def get_index_timeout_sec() -> int:
    """Return the timeout used when crawling the remote docs site for indexing."""

    raw = os.environ.get("INTRANET_DOCS_INDEX_TIMEOUT_SEC", "").strip()
    if not raw:
        return DEFAULT_INDEX_TIMEOUT_SEC

    try:
        timeout_sec = int(raw)
    except ValueError as exc:
        raise ValueError("INTRANET_DOCS_INDEX_TIMEOUT_SEC must be an integer.") from exc

    return max(1, timeout_sec)


def get_docs_page_list() -> tuple[str, ...]:
    """Return an optional explicit list of docs HTML pages to index remotely."""

    raw = os.environ.get("INTRANET_DOCS_HTML_PAGES", "").strip()
    if not raw:
        return ()

    pages: list[str] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        candidate = candidate.split("#", 1)[0].split("?", 1)[0]
        if candidate.endswith(".html"):
            pages.append(candidate.rsplit("/", 1)[-1])

    return tuple(dict.fromkeys(pages))


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


def normalize_input_method(method: str | None) -> str | None:
    """Normalize an optional HTTP method and validate it when provided."""

    if method is None:
        return None

    normalized = method.strip().lower()
    if not normalized:
        return None
    if normalized not in SUPPORTED_HTTP_METHODS:
        supported = ", ".join(SUPPORTED_HTTP_METHODS)
        raise ValueError(f"Invalid method '{method}'. Supported values: {supported}.")

    return normalized


def normalize_api_reference(value: str) -> str:
    """Normalize an API path or request URL to an exact OpenAPI-style path."""

    raw = value.strip()
    if not raw:
        raise ValueError("API path or request URL is required.")

    raw = re.sub(r"^\s*request\s+url\s*:\s*", "", raw, flags=re.IGNORECASE).strip()
    raw = raw.strip("`")
    parsed = urlparse(raw)

    if parsed.scheme and parsed.netloc:
        path = parsed.path
    else:
        path = parsed.path or raw

    path = path.strip()
    if not path:
        raise ValueError(f"Could not extract a pathname from input: {value}")

    if not path.startswith("/"):
        path = f"/{path}"

    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1:
        path = path.rstrip("/")

    return path


def is_local_doc_url(value: str) -> bool:
    """Return whether the input is already an allowed local docs URL."""

    base_url, _ = split_url_and_hash(value.strip().strip("`"))
    return validate_allowed_url(base_url, get_allowed_prefixes()) is None


def get_operation_methods(path_item: Any) -> tuple[str, ...]:
    """Return supported HTTP methods declared for an OpenAPI path item."""

    if not isinstance(path_item, dict):
        return ()

    return tuple(
        method
        for method in SUPPORTED_HTTP_METHODS
        if isinstance(path_item.get(method), dict)
    )


def discover_remote_doc_pages(
    base_url: str,
    timeout: httpx.Timeout,
    allowed_prefixes: Sequence[str],
) -> tuple[str, ...]:
    """Discover remote docs HTML pages from the docs root page."""

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(base_url)
        response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    page_names: list[str] = []
    seen = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href:
            continue

        href = href.split("#", 1)[0].split("?", 1)[0]
        if not href.endswith(".html"):
            continue

        absolute_url = urljoin(base_url, href)
        if validate_allowed_url(absolute_url, allowed_prefixes) is not None:
            continue

        page_name = absolute_url.rsplit("/", 1)[-1]
        if page_name in seen:
            continue
        seen.add(page_name)
        page_names.append(page_name)

    return tuple(page_names)


@lru_cache(maxsize=4)
def build_doc_index(
    base_url: str,
    explicit_pages: tuple[str, ...],
    timeout_sec: int,
) -> DocIndex:
    """Crawl remote HTML docs pages and build an exact path to docs page index."""

    allowed_prefixes = get_allowed_prefixes()
    error = validate_allowed_url(base_url, allowed_prefixes)
    if error:
        raise ValueError(error)

    timeout = httpx.Timeout(timeout_sec)
    page_names = explicit_pages or discover_remote_doc_pages(
        base_url=base_url,
        timeout=timeout,
        allowed_prefixes=allowed_prefixes,
    )
    if not page_names:
        raise ValueError(
            "Could not discover any remote docs HTML pages. "
            "Set INTRANET_DOCS_HTML_PAGES if the docs root does not expose a listing."
        )

    path_map: dict[str, list[DocCandidate]] = {}
    path_lookup: dict[str, str] = {}
    file_map: dict[str, dict[str, tuple[str, ...]]] = {}

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for page_name in page_names:
            page_url = urljoin(base_url, page_name)
            error = validate_allowed_url(page_url, allowed_prefixes)
            if error:
                continue

            response = client.get(page_url)
            response.raise_for_status()

            spec = extract_embedded_openapi_spec(response.text)
            if not spec:
                continue

            paths = spec.get("paths")
            if not isinstance(paths, dict):
                continue

            per_file_paths: dict[str, tuple[str, ...]] = {}
            for api_path, path_item in paths.items():
                if not isinstance(api_path, str):
                    continue

                methods = get_operation_methods(path_item)
                if not methods:
                    continue

                path_lookup.setdefault(api_path.lower(), api_path)
                path_map.setdefault(api_path, []).append(
                    DocCandidate(file=page_name, methods=methods)
                )
                per_file_paths[api_path.lower()] = methods

            if per_file_paths:
                file_map[page_name] = per_file_paths

    frozen_map = {
        api_path: tuple(candidates)
        for api_path, candidates in path_map.items()
    }

    return DocIndex(
        source_url=base_url,
        path_map=frozen_map,
        path_lookup=path_lookup,
        file_map=file_map,
    )


def get_doc_index() -> DocIndex:
    """Return the cached remote docs index."""

    return build_doc_index(
        base_url=get_docs_base_url(required=True),
        explicit_pages=get_docs_page_list(),
        timeout_sec=get_index_timeout_sec(),
    )


def choose_preferred_file(api_path: str, file_name: str) -> int:
    """Score documentation files when multiple pages contain the same API path."""

    score = 0
    lowered_path = api_path.lower()

    for prefix, preferred_file in PREFERRED_DOC_FILES:
        if lowered_path.startswith(prefix) and file_name == preferred_file:
            score += 100

    parts = [part for part in lowered_path.strip("/").split("/") if part]
    namespace = parts[0] if parts else ""
    expected_suffix = NAMESPACED_DOC_SUFFIXES.get(namespace)

    if expected_suffix and file_name.endswith(f"{expected_suffix}.html"):
        score += 20
    elif namespace in {"open", "api"} and not re.search(
        r"_(mapi|xapi|sapi)\.html$",
        file_name,
    ):
        score += 10

    return score


def sort_doc_candidates(api_path: str, candidates: Sequence[DocCandidate]) -> list[DocCandidate]:
    """Sort candidates by explicit preference, namespace affinity, then filename."""

    return sorted(
        candidates,
        key=lambda candidate: (-choose_preferred_file(api_path, candidate.file), candidate.file),
    )


def get_override_file_candidates(api_path: str) -> list[str]:
    """Return heuristic fallback files for known path-to-page exceptions."""

    lowered_path = api_path.lower()
    files: list[str] = []
    for match_type, pattern, file_name in FALLBACK_FILE_OVERRIDES:
        if match_type == "exact" and lowered_path == pattern:
            files.append(file_name)
        if match_type == "prefix" and lowered_path.startswith(pattern):
            files.append(file_name)
    return files


def get_heuristic_file_candidates(api_path: str) -> list[str]:
    """Guess likely documentation files from a normalized API path."""

    parts = [part for part in api_path.lower().strip("/").split("/") if part]
    if len(parts) < 2:
        return []

    namespace, resource = parts[0], parts[1]
    resource_slug = re.sub(r"[^a-z0-9]+", "", resource)
    if not resource_slug:
        return []

    files = get_override_file_candidates(api_path)
    suffix = NAMESPACED_DOC_SUFFIXES.get(namespace, "")

    if namespace in {"open", "api", "mapi", "xapi", "sapi"}:
        files.append(f"{resource_slug}{suffix}.html")

    deduped: list[str] = []
    seen = set()
    for file_name in files:
        if file_name in seen:
            continue
        seen.add(file_name)
        deduped.append(file_name)

    return deduped


def build_doc_url(file_name: str, api_path: str, method: str) -> str:
    """Build a Stoplight operation URL from doc file, path, and method."""

    base = urljoin(get_docs_base_url(required=True), file_name)
    return f"{base}#/paths/{normalize_stoplight_path(api_path)}/{method}"


def pick_method(
    api_path: str,
    requested_method: str | None,
    candidates: Sequence[DocCandidate],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Choose a method from matching candidates and capture any ambiguity notes."""

    warnings: list[str] = []

    if requested_method:
        for candidate in candidates:
            if requested_method in candidate.methods:
                return requested_method, candidate.methods, tuple(warnings)
        available = sorted({method for candidate in candidates for method in candidate.methods})
        raise ValueError(
            "Requested method does not exist for this path. "
            f"path={api_path} "
            f"available_methods={', '.join(available) or '(none)'}"
        )

    method_sets = {candidate.methods for candidate in candidates}
    if len(method_sets) > 1:
        warnings.append(
            "Multiple method sets were found for this path across different docs pages; "
            "selected the first method from the preferred candidate."
        )

    preferred_methods = candidates[0].methods
    return preferred_methods[0], preferred_methods, tuple(warnings)


def resolve_doc_link(api_reference: str, method: str | None = None) -> ResolvedDocLink:
    """Resolve an API path or request URL to the best matching local docs page URL."""

    requested_method = normalize_input_method(method)
    normalized_path = normalize_api_reference(api_reference)
    index = get_doc_index()
    actual_path = index.path_lookup.get(normalized_path.lower(), normalized_path)

    warnings: list[str] = []
    matched_by = "exact-index"
    candidates = index.path_map.get(actual_path)

    if not candidates:
        matched_by = "heuristic"
        heuristic_files = get_heuristic_file_candidates(actual_path)
        resolved_candidates: list[DocCandidate] = []
        for file_name in heuristic_files:
            methods = index.file_map.get(file_name, {}).get(actual_path.lower())
            if methods:
                resolved_candidates.append(DocCandidate(file=file_name, methods=methods))
        candidates = tuple(resolved_candidates)

    if not candidates:
        raise ValueError(
            "No documentation page matched the normalized API path. "
            f"path={normalized_path}"
        )

    sorted_candidates = sort_doc_candidates(actual_path, candidates)
    if len(sorted_candidates) > 1:
        warnings.append(
            "Multiple docs pages contain this path; selected the preferred file based on "
            "namespace and known duplicate-page priorities."
        )

    selected_method, selected_methods, method_warnings = pick_method(
        api_path=actual_path,
        requested_method=requested_method,
        candidates=sorted_candidates,
    )
    warnings.extend(method_warnings)
    selected_file = sorted_candidates[0].file

    return ResolvedDocLink(
        input_value=api_reference,
        path=actual_path,
        file=selected_file,
        method=selected_method,
        methods=selected_methods,
        doc_url=build_doc_url(selected_file, actual_path, selected_method),
        candidates=tuple(sorted_candidates),
        matched_by=matched_by,
        warnings=tuple(warnings),
    )


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


def resolve_schema_ref(schema: Any, spec: dict[str, Any] | None) -> Any:
    """Resolve local OpenAPI schema refs when possible."""

    if not isinstance(schema, dict):
        return schema

    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/") or not isinstance(spec, dict):
        return schema

    target: Any = spec
    for part in ref.removeprefix("#/").split("/"):
        if not isinstance(target, dict):
            return schema
        target = target.get(part)
        if target is None:
            return schema

    return target if isinstance(target, dict) else schema


def format_schema_details(
    schema: Any,
    spec: dict[str, Any] | None,
    indent: int = 0,
    seen_refs: set[str] | None = None,
) -> list[str]:
    """Render schema fields recursively for object-like request/response bodies."""

    if seen_refs is None:
        seen_refs = set()

    if not isinstance(schema, dict):
        return []

    ref = schema.get("$ref")
    if isinstance(ref, str) and ref:
        if ref in seen_refs:
            return []
        seen_refs = seen_refs | {ref}
        schema = resolve_schema_ref(schema, spec)

    merged_properties: dict[str, Any] = {}
    required: set[str] = set()

    schema_type = schema.get("type")
    if schema_type == "array":
        item_schema = schema.get("items", {})
        prefix = "  " * indent
        lines = [f"{prefix}- items `{format_schema_type(item_schema)}`"]
        lines.extend(format_schema_details(item_schema, spec, indent + 1, seen_refs))
        return lines

    if isinstance(schema.get("properties"), dict):
        merged_properties.update(schema["properties"])
    if isinstance(schema.get("required"), list):
        required.update(str(item) for item in schema["required"])

    for key in ("allOf", "oneOf", "anyOf"):
        variants = schema.get(key)
        if not isinstance(variants, list):
            continue
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            resolved_variant = resolve_schema_ref(variant, spec)
            if isinstance(resolved_variant.get("properties"), dict):
                merged_properties.update(resolved_variant["properties"])
            if isinstance(resolved_variant.get("required"), list):
                required.update(str(item) for item in resolved_variant["required"])

    if not merged_properties:
        return []

    lines: list[str] = []
    prefix = "  " * indent
    for name, prop_schema in merged_properties.items():
        prop_schema = resolve_schema_ref(prop_schema, spec)
        prop_type = format_schema_type(prop_schema)
        is_required = " required" if name in required else ""
        description = clean_text(str(prop_schema.get("description", "")))
        lines.append(f"{prefix}- `{name}` `{prop_type}`{is_required}")
        if description:
            lines.append(f"{prefix}  {description}")
        lines.extend(format_schema_details(prop_schema, spec, indent + 1, seen_refs))

    return lines


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


def format_request_body_lines(request_body: Any, spec: dict[str, Any] | None = None) -> list[str]:
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
        schema_lines = format_schema_details(schema, spec)
        if schema_lines:
            lines.extend(schema_lines)
    lines.append("")
    return lines


def format_response_lines(responses: Any, spec: dict[str, Any] | None = None) -> list[str]:
    """Render OpenAPI responses including headers and body schemas."""

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

        content = response.get("content")
        if isinstance(content, dict) and content:
            lines.extend(["#### Body", ""])
            for media_type, media_spec in content.items():
                if not isinstance(media_spec, dict):
                    continue
                schema = media_spec.get("schema", {})
                lines.extend([media_type, "", f"`{media_type}`", ""])
                schema_lines = format_schema_details(schema, spec)
                if schema_lines:
                    lines.extend(schema_lines)
                    lines.append("")

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
    spec: dict[str, Any] | None = None,
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
    request_sections.extend(format_request_body_lines(request_body, spec))
    if request_sections:
        lines.extend(["## Request", ""])
        lines.extend(request_sections)

    lines.extend(format_response_lines(responses, spec))
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
                    render_openapi_operation_markdown(
                        path=path,
                        method=method,
                        operation=operation,
                        spec=spec,
                    )
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
                render_openapi_operation_markdown(
                    path=path,
                    method=method,
                    operation=operation,
                    spec=spec,
                ),
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


def normalize_fetch_target(url: str, method: str | None = None) -> str:
    """Accept either a docs URL or an API request reference and return a docs URL."""

    requested_method = normalize_input_method(method)
    if is_local_doc_url(url):
        return url

    return resolve_doc_link(api_reference=url, method=requested_method).doc_url


def fetch_document(
    url: str,
    timeout_sec: int,
    method: str | None = None,
) -> FetchResult:
    """Fetch and normalize a supported local documentation page."""

    target_url = normalize_fetch_target(url=url, method=method)
    allowed_prefixes = get_allowed_prefixes()
    base_url, hash_fragment = split_url_and_hash(target_url)

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
def resolve_intranet_doc_url(
    api_reference: str,
    method: str | None = None,
) -> dict[str, Any]:
    """Resolve an API path or Request URL to a local Stoplight documentation URL."""

    try:
        resolved = resolve_doc_link(api_reference=api_reference, method=method)
        return {
            "input": resolved.input_value,
            "path": resolved.path,
            "file": resolved.file,
            "method": resolved.method,
            "methods": list(resolved.methods),
            "docUrl": resolved.doc_url,
            "matchedBy": resolved.matched_by,
            "candidates": [
                {
                    "file": candidate.file,
                    "methods": list(candidate.methods),
                    "docUrl": build_doc_url(
                        candidate.file,
                        resolved.path,
                        candidate.methods[0],
                    ),
                }
                for candidate in resolved.candidates
            ],
            "warnings": list(resolved.warnings),
        }
    except ValueError as exc:
        return {
            "input": api_reference,
            "error": str(exc),
        }


@mcp.tool()
def fetch_intranet_doc(
    url: str,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    max_chars: int = DEFAULT_MAX_CHARS,
    output_format: str = DEFAULT_OUTPUT_FORMAT,
    method: str | None = None,
) -> str:
    """Fetch local API documentation content and return readable text for Codex."""

    normalized_format = output_format.strip().lower() or DEFAULT_OUTPUT_FORMAT
    if normalized_format not in SUPPORTED_OUTPUT_FORMATS:
        return format_error(
            "Invalid output_format. Supported values: markdown, text.",
            requested_url=url,
        )

    try:
        result = fetch_document(url=url, timeout_sec=timeout_sec, method=method)
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
