import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path("/Users/leechen/projects/agents/mcp/intranet-docs/server.py")
SPEC = importlib.util.spec_from_file_location("intranet_docs_server_under_test", MODULE_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SERVER
SPEC.loader.exec_module(SERVER)


def make_html_with_spec(paths: dict[str, dict[str, dict]]) -> str:
    spec = {
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0.0"},
        "paths": paths,
    }
    return f"<html><body><script>window.apiDocs = {json.dumps(spec)}</script></body></html>"


class FakeResponse:
    def __init__(
        self,
        url: str,
        text: str,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise SERVER.httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=None,
                response=None,
            )


class FakeClient:
    def __init__(self, responses: dict[str, FakeResponse], requested_urls: list[str]) -> None:
        self._responses = responses
        self._requested_urls = requested_urls

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(self, url: str) -> FakeResponse:
        self._requested_urls.append(url)
        try:
            return self._responses[url]
        except KeyError as exc:
            raise AssertionError(f"Unexpected URL requested in test: {url}") from exc


class IntranetDocsServerTests(unittest.TestCase):
    def setUp(self) -> None:
        SERVER.build_doc_index.cache_clear()

    def tearDown(self) -> None:
        SERVER.build_doc_index.cache_clear()

    def test_normalize_api_reference_supports_request_url_and_cleanup(self) -> None:
        self.assertEqual(
            SERVER.normalize_api_reference(
                "Request URL: https://hkp-api.shangshangji.cn/open/activityreward/lastmessages?code=lingong_2nd#foo"
            ),
            "/open/activityreward/lastmessages",
        )
        self.assertEqual(
            SERVER.normalize_api_reference("open/radiovipaccount/get/"),
            "/open/radiovipaccount/get",
        )

    def test_resolve_doc_link_prefers_duplicate_priority_file(self) -> None:
        index = SERVER.DocIndex(
            source_url="http://docs.example.internal:9500/",
            path_map={
                "/open/discovery/slides": (
                    SERVER.DocCandidate(file="indexsearch.html", methods=("get",)),
                    SERVER.DocCandidate(file="discovery.html", methods=("get",)),
                )
            },
            path_lookup={"/open/discovery/slides": "/open/discovery/slides"},
            file_map={},
        )

        with patch.object(SERVER, "get_doc_index", return_value=index):
            with patch.dict(
                os.environ,
                {"INTRANET_DOCS_BASE_URL": "http://docs.example.internal:9500/"},
                clear=False,
            ):
                resolved = SERVER.resolve_doc_link("/open/discovery/slides")

        self.assertEqual(resolved.file, "discovery.html")
        self.assertEqual(resolved.method, "get")
        self.assertEqual(
            resolved.doc_url,
            "http://docs.example.internal:9500/discovery.html#/paths/open-discovery-slides/get",
        )
        self.assertTrue(resolved.warnings)

    def test_resolve_doc_link_uses_override_heuristic_when_exact_index_misses(self) -> None:
        index = SERVER.DocIndex(
            source_url="http://docs.example.internal:9500/",
            path_map={},
            path_lookup={},
            file_map={
                "payment.html": {
                    "/open/wx/partnerjsapicallback": ("post",),
                }
            },
        )

        with patch.object(SERVER, "get_doc_index", return_value=index):
            with patch.dict(
                os.environ,
                {"INTRANET_DOCS_BASE_URL": "http://docs.example.internal:9500/"},
                clear=False,
            ):
                resolved = SERVER.resolve_doc_link(
                    "/open/wx/partnerjsapicallback",
                    method="post",
                )

        self.assertEqual(resolved.file, "payment.html")
        self.assertEqual(resolved.method, "post")
        self.assertEqual(resolved.matched_by, "heuristic")

    def test_build_doc_index_discovers_remote_pages_from_root_listing(self) -> None:
        base_url = "http://docs.example.internal:9500/"
        page_url = f"{base_url}radiovipaccount.html"
        requested_urls: list[str] = []
        responses = {
            base_url: FakeResponse(
                base_url,
                '<html><body><a href="radiovipaccount.html">radiovipaccount</a></body></html>',
            ),
            page_url: FakeResponse(
                page_url,
                make_html_with_spec(
                    {
                        "/open/radiovipaccount/get": {
                            "get": {"summary": "Get VIP account"}
                        }
                    }
                ),
            ),
        }

        with patch.dict(
            os.environ,
            {"INTRANET_ALLOWED_PREFIXES": base_url},
            clear=False,
        ):
            with patch.object(
                SERVER.httpx,
                "Client",
                return_value=FakeClient(responses, requested_urls),
            ):
                index = SERVER.build_doc_index(base_url, (), 5)

        self.assertIn(base_url, requested_urls)
        self.assertIn(page_url, requested_urls)
        self.assertIn("/open/radiovipaccount/get", index.path_map)
        self.assertEqual(index.path_map["/open/radiovipaccount/get"][0].file, "radiovipaccount.html")

    def test_build_doc_index_explicit_pages_skips_root_discovery(self) -> None:
        base_url = "http://docs.example.internal:9500/"
        page_url = f"{base_url}activityreward.html"
        requested_urls: list[str] = []
        responses = {
            page_url: FakeResponse(
                page_url,
                make_html_with_spec(
                    {
                        "/open/activityreward/lastmessages": {
                            "get": {"summary": "Last messages"}
                        }
                    }
                ),
            ),
        }

        with patch.dict(
            os.environ,
            {"INTRANET_ALLOWED_PREFIXES": base_url},
            clear=False,
        ):
            with patch.object(
                SERVER.httpx,
                "Client",
                return_value=FakeClient(responses, requested_urls),
            ):
                index = SERVER.build_doc_index(base_url, ("activityreward.html",), 5)

        self.assertEqual(requested_urls, [page_url])
        self.assertIn("/open/activityreward/lastmessages", index.path_map)

    def test_fetch_intranet_doc_accepts_api_path_and_returns_operation_text(self) -> None:
        base_url = "http://docs.example.internal:9500/"
        page_url = f"{base_url}radiovipaccount.html"
        requested_urls: list[str] = []
        responses = {
            base_url: FakeResponse(
                base_url,
                '<html><body><a href="radiovipaccount.html">radiovipaccount</a></body></html>',
            ),
            page_url: FakeResponse(
                page_url,
                make_html_with_spec(
                    {
                        "/open/radiovipaccount/get": {
                            "get": {
                                "summary": "Get VIP account",
                                "responses": {"200": {"description": "OK"}},
                            }
                        }
                    }
                ),
            ),
        }

        with patch.dict(
            os.environ,
            {
                "INTRANET_ALLOWED_PREFIXES": base_url,
                "INTRANET_DOCS_BASE_URL": base_url,
            },
            clear=False,
        ):
            with patch.object(
                SERVER.httpx,
                "Client",
                return_value=FakeClient(responses, requested_urls),
            ):
                output = SERVER.fetch_intranet_doc(
                    "/open/radiovipaccount/get",
                    max_chars=1000,
                    output_format="text",
                )

        self.assertIn("requested_url: /open/radiovipaccount/get", output)
        self.assertIn("hash_fragment: #/paths/open-radiovipaccount-get/get", output)
        self.assertIn("/open/radiovipaccount/get", output)
        self.assertIn("Resolved the requested hash route from the embedded OpenAPI spec.", output)
        self.assertGreaterEqual(requested_urls.count(page_url), 1)

    def test_resolve_intranet_doc_url_returns_clear_error_when_base_url_missing(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            result = SERVER.resolve_intranet_doc_url("/open/radiovipaccount/get")

        self.assertEqual(
            result["error"],
            "INTRANET_DOCS_BASE_URL is required when resolving an API path or request URL.",
        )

    def test_fetch_intranet_doc_returns_clear_error_for_unknown_path(self) -> None:
        index = SERVER.DocIndex(
            source_url="http://docs.example.internal:9500/",
            path_map={},
            path_lookup={},
            file_map={},
        )

        with patch.object(SERVER, "get_doc_index", return_value=index):
            with patch.dict(
                os.environ,
                {
                    "INTRANET_ALLOWED_PREFIXES": "http://docs.example.internal:9500/",
                    "INTRANET_DOCS_BASE_URL": "http://docs.example.internal:9500/",
                },
                clear=False,
            ):
                output = SERVER.fetch_intranet_doc(
                    "/api/not-exists/demo",
                    max_chars=200,
                    output_format="text",
                )

        self.assertIn("No documentation page matched the normalized API path.", output)


if __name__ == "__main__":
    unittest.main()
