import itertools
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from permetheus import acquisition  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes for the transport seams (_resolve_host / _open_connection) so
# fetch_public_url tests never touch a real socket.
# ---------------------------------------------------------------------------


class _Headers:
    def __init__(self, items):
        self._items = items

    def get_all(self, name, default=None):
        values = [v for k, v in self._items if k.lower() == name.lower()]
        if values:
            return values
        return default if default is not None else []


class _FakeResponse:
    def __init__(self, status, header_items, body):
        self.status = status
        self.headers = _Headers(header_items)
        self._body = body

    def getheader(self, name, default=None):
        values = self.headers.get_all(name)
        return values[0] if values else default

    def read(self, n=None):
        if n is None or n >= len(self._body):
            data, self._body = self._body, b""
            return data
        data, self._body = self._body[:n], self._body[n:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self, response):
        self._response = response
        self.requested = None
        self.closed = False

    def request(self, method, target, headers=None):
        self.requested = (method, target, headers)

    def getresponse(self):
        return self._response

    def close(self):
        self.closed = True


class _FakeConnectionFactory:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, host, port, address, tls, timeout):
        self.calls.append((host, port, address, tls, timeout))
        return _FakeConnection(self._responses.pop(0))


def _html_response(body: str, status: int = 200, extra_headers=()):
    encoded = body.encode("utf-8")
    headers = [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(encoded)))]
    headers.extend(extra_headers)
    return _FakeResponse(status, headers, encoded)


PUBLIC_IP = "93.184.216.34"  # a real, public, non-reserved address; never dialed in tests


class FetchPublicUrlSSRFTests(unittest.TestCase):
    def test_rejects_private_resolved_address(self):
        with mock.patch.object(acquisition, "_resolve_host", return_value=["10.0.0.5"]):
            with mock.patch.object(acquisition, "_open_connection") as open_conn:
                with self.assertRaises(acquisition.FetchError) as ctx:
                    acquisition.fetch_public_url("http://example.com/")
                self.assertEqual(str(ctx.exception), "non_public_address")
                open_conn.assert_not_called()

    def test_rejects_loopback_ipv4_literal_host(self):
        with self.assertRaises(acquisition.FetchError) as ctx:
            acquisition.fetch_public_url("http://127.0.0.1/")
        self.assertEqual(str(ctx.exception), "non_public_address")

    def test_rejects_loopback_ipv6_literal_host(self):
        with self.assertRaises(acquisition.FetchError) as ctx:
            acquisition.fetch_public_url("http://[::1]/")
        self.assertEqual(str(ctx.exception), "non_public_address")

    def test_rejects_link_local_address(self):
        with self.assertRaises(acquisition.FetchError):
            acquisition.fetch_public_url("http://169.254.1.1/")

    def test_rejects_multicast_address(self):
        with self.assertRaises(acquisition.FetchError):
            acquisition.fetch_public_url("http://224.0.0.5/")

    def test_rejects_unspecified_address(self):
        with self.assertRaises(acquisition.FetchError):
            acquisition.fetch_public_url("http://0.0.0.0/")

    def test_rejects_ipv4_mapped_private_address(self):
        with self.assertRaises(acquisition.FetchError):
            acquisition.fetch_public_url("http://[::ffff:10.1.2.3]/")

    def test_rejects_unique_local_ipv6(self):
        with self.assertRaises(acquisition.FetchError):
            acquisition.fetch_public_url("http://[fc00::1]/")

    def test_rejects_unsupported_port(self):
        with mock.patch.object(acquisition, "_resolve_host") as resolver:
            with self.assertRaises(acquisition.FetchError) as ctx:
                acquisition.fetch_public_url("http://example.com:8080/")
            self.assertEqual(str(ctx.exception), "unsupported_port")
            resolver.assert_not_called()

    def test_rejects_unsupported_scheme(self):
        with self.assertRaises(acquisition.FetchError) as ctx:
            acquisition.fetch_public_url("ftp://example.com/file")
        self.assertEqual(str(ctx.exception), "unsupported_url")

    def test_rejects_credentials_in_url(self):
        with self.assertRaises(acquisition.FetchError) as ctx:
            acquisition.fetch_public_url("http://user:pass@example.com/")
        self.assertEqual(str(ctx.exception), "unsupported_url")

    def test_dns_rebinding_safe_connects_to_validated_address(self):
        factory = _FakeConnectionFactory([_html_response("<html><body>ok</body></html>")])
        with mock.patch.object(acquisition, "_resolve_host", return_value=[PUBLIC_IP]) as resolver:
            with mock.patch.object(acquisition, "_open_connection", side_effect=factory):
                result = acquisition.fetch_public_url("http://example.com/")
        resolver.assert_called_once_with("example.com", 80)
        self.assertEqual(factory.calls[0][2], PUBLIC_IP)  # connected to the validated address
        self.assertEqual(result.status, 200)

    def test_literal_ip_host_skips_resolver(self):
        factory = _FakeConnectionFactory([_html_response("<html></html>")])
        with mock.patch.object(acquisition, "_resolve_host") as resolver:
            with mock.patch.object(acquisition, "_open_connection", side_effect=factory):
                acquisition.fetch_public_url(f"http://{PUBLIC_IP}/")
        resolver.assert_not_called()


class FetchPublicUrlBoundsTests(unittest.TestCase):
    def _patched(self, responses):
        factory = _FakeConnectionFactory(responses)
        return (
            mock.patch.object(acquisition, "_resolve_host", return_value=[PUBLIC_IP]),
            mock.patch.object(acquisition, "_open_connection", side_effect=factory),
            factory,
        )

    def test_follows_redirect_and_revalidates_target(self):
        redirect = _FakeResponse(302, [("Location", "http://example.com/next")], b"")
        final = _html_response("<html><body>done</body></html>")
        p1, p2, factory = self._patched([redirect, final])
        with p1, p2:
            result = acquisition.fetch_public_url("http://example.com/start")
        self.assertEqual(result.final_url, "http://example.com/next")
        self.assertEqual(result.redirect_chain, ["http://example.com/next"])
        self.assertEqual(len(factory.calls), 2)

    def test_bounds_redirect_count(self):
        redirects = [_FakeResponse(302, [("Location", f"http://example.com/hop{i}")], b"") for i in range(10)]
        p1, p2, _ = self._patched(redirects)
        with p1, p2:
            with self.assertRaises(acquisition.FetchError) as ctx:
                acquisition.fetch_public_url("http://example.com/start", max_redirects=3)
        self.assertEqual(str(ctx.exception), "too_many_redirects")

    def test_cross_host_redirect_blocked_when_disallowed(self):
        redirect = _FakeResponse(302, [("Location", "https://evil.example/landing")], b"")
        p1, p2, factory = self._patched([redirect])
        with p1, p2:
            with self.assertRaises(acquisition.FetchError) as ctx:
                acquisition.fetch_public_url("http://example.com/start", allow_cross_host_redirects=False)
        self.assertEqual(str(ctx.exception), "cross_host_redirect_blocked")
        self.assertEqual(len(factory.calls), 1)  # the other host is never dialed

    def test_cross_host_redirect_still_followed_by_default(self):
        redirect = _FakeResponse(302, [("Location", "http://other.example/next")], b"")
        p1, p2, _ = self._patched([redirect, _html_response("<html>ok</html>")])
        with p1, p2:
            result = acquisition.fetch_public_url("http://example.com/start")
        self.assertEqual(result.final_url, "http://other.example/next")

    def test_redirect_to_private_address_is_rejected(self):
        redirect = _FakeResponse(302, [("Location", "http://10.0.0.9/internal")], b"")
        factory = _FakeConnectionFactory([redirect])
        with mock.patch.object(acquisition, "_resolve_host", return_value=[PUBLIC_IP, "10.0.0.9"]):
            with mock.patch.object(acquisition, "_open_connection", side_effect=factory):
                with self.assertRaises(acquisition.FetchError):
                    acquisition.fetch_public_url("http://example.com/start")

    def test_declared_content_length_over_budget_rejected(self):
        response = _FakeResponse(
            200,
            [("Content-Type", "text/html"), ("Content-Length", str(10_000_000))],
            b"irrelevant",
        )
        p1, p2, _ = self._patched([response])
        with p1, p2:
            with self.assertRaises(acquisition.FetchError) as ctx:
                acquisition.fetch_public_url("http://example.com/", max_bytes=1000)
        self.assertEqual(str(ctx.exception), "response_too_large")

    def test_oversized_undeclared_body_is_truncated_not_silently_accepted_whole(self):
        big_body = b"a" * 5000
        response = _FakeResponse(200, [("Content-Type", "text/plain")], big_body)
        p1, p2, _ = self._patched([response])
        with p1, p2:
            result = acquisition.fetch_public_url("http://example.com/", max_bytes=1000)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.body), 1000)

    def test_unsupported_content_type_rejected(self):
        response = _FakeResponse(200, [("Content-Type", "application/octet-stream")], b"\x00\x01")
        p1, p2, _ = self._patched([response])
        with p1, p2:
            with self.assertRaises(acquisition.FetchError) as ctx:
                acquisition.fetch_public_url("http://example.com/")
        self.assertEqual(str(ctx.exception), "unsupported_content_type")

    def test_compressed_response_rejected(self):
        response = _FakeResponse(200, [("Content-Type", "text/html"), ("Content-Encoding", "gzip")], b"\x1f\x8b")
        p1, p2, _ = self._patched([response])
        with p1, p2:
            with self.assertRaises(acquisition.FetchError) as ctx:
                acquisition.fetch_public_url("http://example.com/")
        self.assertEqual(str(ctx.exception), "compressed_response_rejected")

    def test_overall_timeout_bounds_the_whole_fetch(self):
        with mock.patch.object(acquisition.time, "monotonic", side_effect=[0, 100]):
            with self.assertRaises(acquisition.FetchError) as ctx:
                acquisition.fetch_public_url("http://example.com/", overall_timeout=10)
        self.assertEqual(str(ctx.exception), "overall_timeout_exceeded")

    def test_slow_trickle_body_is_bounded_by_overall_timeout(self):
        # A server that returns one byte per read() call never trips any
        # single socket-level read timeout (slowloris); only a budget on the
        # whole streamed read -- not just the pre-connect hops -- catches it.
        class _Trickle:
            status = 200
            headers = _Headers([("Content-Type", "text/plain")])

            def getheader(self, name, default=None):
                values = self.headers.get_all(name)
                return values[0] if values else default

            def read(self, n=None):
                return b"x"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        factory = _FakeConnectionFactory([_Trickle()])
        with mock.patch.object(acquisition, "_resolve_host", return_value=[PUBLIC_IP]):
            with mock.patch.object(acquisition, "_open_connection", side_effect=factory):
                with mock.patch.object(acquisition.time, "monotonic", side_effect=itertools.count(0, 5)):
                    with self.assertRaises(acquisition.FetchError) as ctx:
                        acquisition.fetch_public_url("http://example.com/", overall_timeout=10)
        self.assertEqual(str(ctx.exception), "overall_timeout_exceeded")


# ---------------------------------------------------------------------------
# PRH v3 normalization, pagination and error handling
# ---------------------------------------------------------------------------


def _urlopen_success(payload: dict):
    body = json.dumps(payload).encode("utf-8")

    class _Resp:
        status = 200

        def read(self, n=None):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp()


SAMPLE_COMPANY = {
    "businessId": {"value": "0116297-6", "registrationDate": "1978-03-15", "source": "3"},
    "names": [
        {"name": "Valio Oy", "type": "1", "registrationDate": "1992-09-30", "version": 1, "source": "1"},
        {
            "name": "Valio Meijerien Keskusosuusliike",
            "type": "1",
            "registrationDate": "1955-07-01",
            "endDate": "1992-09-29",
            "version": 2,
            "source": "1",
        },
    ],
    "website": {"url": "https://www.valio.fi", "source": "1"},
    "companyForms": [{"type": "OYJ", "version": 1, "source": "1"}],
    "addresses": [
        {
            "type": 1,
            "street": "Meijeritie 6",
            "postCode": "00370",
            "postOffices": [{"city": "Helsinki", "languageCode": "1"}],
            "country": "FI",
            "source": "1",
        }
    ],
    "mainBusinessLine": {"type": "10510", "source": "1"},
    "tradeRegisterStatus": "2",
    "status": "1",
    "registrationDate": "1978-03-15",
    "endDate": None,
    "lastModified": "2024-01-01T00:00:00",
}


class PRHSearchTests(unittest.TestCase):
    def test_normalizes_company_record_and_preserves_provenance(self):
        payload = {"totalResults": 1, "companies": [SAMPLE_COMPANY]}
        with mock.patch.object(acquisition.urllib.request, "urlopen", return_value=_urlopen_success(payload)):
            result = acquisition.prh_search(business_id="0116297-6")
        self.assertEqual(result.total_results, 1)
        company = result.companies[0]
        self.assertEqual(company.business_id, "0116297-6")
        self.assertEqual(company.name, "Valio Oy")
        self.assertEqual(company.website, "https://www.valio.fi")
        self.assertEqual(company.company_form, "OYJ")
        self.assertEqual(company.addresses[0]["city"], "Helsinki")
        self.assertEqual(company.raw, SAMPLE_COMPANY)
        self.assertIn("businessId=0116297-6", company.source_url)
        self.assertTrue(company.fetched_at)  # provenance timestamp present

    def test_pagination_hint_reflects_documented_page_size_assumption(self):
        payload = {"totalResults": 164, "companies": [SAMPLE_COMPANY] * 100}
        with mock.patch.object(acquisition.urllib.request, "urlopen", return_value=_urlopen_success(payload)):
            page1 = acquisition.prh_search(registration_start="2024-01-01", registration_end="2024-01-03", page=1)
        self.assertEqual(page1.next_page_hint, 2)

        payload2 = {"totalResults": 164, "companies": [SAMPLE_COMPANY] * 64}
        with mock.patch.object(acquisition.urllib.request, "urlopen", return_value=_urlopen_success(payload2)):
            page2 = acquisition.prh_search(registration_start="2024-01-01", registration_end="2024-01-03", page=2)
        self.assertIsNone(page2.next_page_hint)

    def test_invalid_date_format_rejected_without_network_call(self):
        with mock.patch.object(acquisition.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(ValueError):
                acquisition.prh_search(registration_start="2024-1-1")  # not zero-padded: shape-invalid
            urlopen.assert_not_called()

    def test_semantically_invalid_date_reaches_api_and_surfaces_its_400(self):
        # "2024-13-40" matches the YYYY-MM-DD shape (regex can't know month 13 is invalid),
        # so this must reach PRH -- which is what live probing on 2026-09-27 confirmed:
        # {"message":"Field registrationDateStart has not correct value: 2024-13-40 - Correct
        # format is YYYY-MM-DD","errorcode":1002}
        error_body = json.dumps(
            {
                "timestamp": "2026-09-26 23:36:43",
                "message": "Field registrationDateStart has not correct value: 2024-13-40 - Correct format is YYYY-MM-DD",
                "errorcode": 1002,
            }
        ).encode()
        http_error = urllib.error.HTTPError(
            url="http://x", code=400, msg="Bad Request", hdrs=None, fp=__import__("io").BytesIO(error_body)
        )
        with mock.patch.object(acquisition.urllib.request, "urlopen", side_effect=http_error):
            with self.assertRaises(acquisition.PRHError) as ctx:
                acquisition.prh_search(registration_start="2024-13-40")
        self.assertEqual(ctx.exception.errorcode, 1002)

    def test_bad_request_raises_prh_error_with_upstream_details(self):
        error_body = json.dumps(
            {"timestamp": "2026-09-26 23:36:43", "message": "Field registrationDateStart has not correct value", "errorcode": 1002}
        ).encode()
        http_error = urllib.error.HTTPError(
            url="http://x", code=400, msg="Bad Request", hdrs=None, fp=__import__("io").BytesIO(error_body)
        )
        with mock.patch.object(acquisition.urllib.request, "urlopen", side_effect=http_error):
            with self.assertRaises(acquisition.PRHError) as ctx:
                acquisition.prh_search(name="Test")
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.errorcode, 1002)

    def test_too_many_requests_raises_prh_error_even_without_json_body(self):
        http_error = urllib.error.HTTPError(
            url="http://x", code=429, msg="Too Many Requests", hdrs=None, fp=__import__("io").BytesIO(b"rate limited")
        )
        with mock.patch.object(acquisition.urllib.request, "urlopen", side_effect=http_error):
            with self.assertRaises(acquisition.PRHError) as ctx:
                acquisition.prh_search(name="Test")
        self.assertEqual(ctx.exception.status, 429)

    def test_unreachable_transport_raises_prh_error(self):
        with mock.patch.object(acquisition.urllib.request, "urlopen", side_effect=OSError("no route")):
            with self.assertRaises(acquisition.PRHError):
                acquisition.prh_search(name="Test")

    def test_oversized_response_body_is_rejected_not_buffered_whole(self):
        class _Resp:
            status = 200

            def read(self, n=None):
                return b"x" * (n if n is not None else 10_000_000)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(acquisition.urllib.request, "urlopen", return_value=_Resp()):
            with self.assertRaises(acquisition.PRHError) as ctx:
                acquisition.prh_search(name="Test", max_bytes=1000)
        self.assertEqual(str(ctx.exception), "response_too_large")

    def test_non_dict_payload_is_rejected(self):
        payload = ["not", "a", "dict"]
        with mock.patch.object(acquisition.urllib.request, "urlopen", return_value=_urlopen_success(payload)):
            with self.assertRaises(acquisition.PRHError) as ctx:
                acquisition.prh_search(name="Test")
        self.assertEqual(str(ctx.exception), "invalid_response_shape")

    def test_current_name_prefers_entry_without_end_date_regardless_of_version(self):
        # version is a per-name sequence number, not a currency flag: this
        # payload has the historical name at version 1 and the current name
        # at version 2, the reverse of the common case.
        names = [
            {"name": "Old Name Oy", "version": 1, "endDate": "2020-01-01"},
            {"name": "New Name Oy", "version": 2, "endDate": None},
        ]
        self.assertEqual(acquisition._current_name(names), "New Name Oy")


# ---------------------------------------------------------------------------
# Website extraction and crawl bounds
# ---------------------------------------------------------------------------

SAMPLE_PAGE_HTML = """
<html>
<head><title>Example Co</title><style>.x { color: red }</style></head>
<body>
  <script>var trackingEmail = "tracker@analytics.invalid";</script>
  <p>Contact us at info@example.com or call +358 40 123 4567.</p>
  <a href="mailto:sales@example.com">Sales</a>
  <a href="tel:+358401234567">Call</a>
  <a href="/about">About</a>
  <a href="https://other.example/partner">Partner</a>
</body>
</html>
"""


class PageExtractorTests(unittest.TestCase):
    def test_extracts_title_visible_text_links_and_contacts(self):
        extractor = acquisition._PageExtractor("https://example.com/")
        extractor.feed(SAMPLE_PAGE_HTML)
        self.assertEqual(extractor.title, "Example Co")
        self.assertNotIn("color: red", extractor.text)
        self.assertNotIn("trackingEmail", extractor.text)
        self.assertIn("info@example.com", extractor.text)
        self.assertIn("https://example.com/about", extractor.links)
        self.assertIn("https://other.example/partner", extractor.links)
        self.assertEqual(extractor.mailto, ["sales@example.com"])
        self.assertEqual(extractor.tel, ["+358401234567"])


class ResearchWebsiteTests(unittest.TestCase):
    def _fake_fetch_factory(self):
        robots_txt = "User-agent: *\nDisallow: /private\n"

        def fake_fetch(url, timeout=10.0, **kwargs):
            if url.endswith("/robots.txt"):
                return acquisition.FetchResult(
                    requested_url=url,
                    final_url=url,
                    status=200,
                    content_type="text/plain",
                    headers={},
                    body=robots_txt.encode(),
                    text=robots_txt,
                    redirect_chain=[],
                    truncated=False,
                    fetched_at=acquisition._now(),
                )
            if url == "https://example.com/private":
                raise AssertionError("robots.txt-disallowed page must not be fetched")
            if url == "https://example.com/":
                html = (
                    "<html><head><title>Home</title></head><body>"
                    '<a href="/private">Private</a>'
                    '<a href="/public">Public</a>'
                    '<a href="https://other.example/x">Other</a>'
                    "</body></html>"
                )
            elif url == "https://example.com/public":
                html = "<html><body>Contact info@example.com or +358 40 999 8888</body></html>"
            else:
                raise AssertionError(f"unexpected fetch of {url}")
            return acquisition.FetchResult(
                requested_url=url,
                final_url=url,
                status=200,
                content_type="text/html",
                headers={},
                body=html.encode(),
                text=html,
                redirect_chain=[],
                truncated=False,
                fetched_at=acquisition._now(),
            )

        return fake_fetch

    def test_obeys_robots_and_bounds_crawl(self):
        with mock.patch.object(acquisition, "fetch_public_url", side_effect=self._fake_fetch_factory()):
            research = acquisition.research_website("https://example.com/", max_pages=5)
        self.assertEqual(research.pages_fetched, 2)
        self.assertIn("https://example.com/private", research.robots_disallowed)
        self.assertIn("https://example.com/public", research.internal_links)
        self.assertIn("https://other.example/x", research.external_links)
        emails = {c.value for c in research.contacts if c.kind == "email"}
        self.assertIn("info@example.com", emails)

    def test_reports_fetch_errors_without_aborting_crawl(self):
        def fake_fetch(url, timeout=10.0, **kwargs):
            if url.endswith("/robots.txt"):
                return acquisition.FetchResult(
                    requested_url=url, final_url=url, status=200, content_type="text/plain", headers={},
                    body=b"", text="", redirect_chain=[], truncated=False, fetched_at=acquisition._now(),
                )
            if url == "https://example.com/":
                raise acquisition.FetchError("upstream_failed")
            raise AssertionError("should not reach further pages")

        with mock.patch.object(acquisition, "fetch_public_url", side_effect=fake_fetch):
            research = acquisition.research_website("https://example.com/", max_pages=5)
        self.assertEqual(research.pages_fetched, 1)
        self.assertEqual(research.pages[0].error, "upstream_failed")
        self.assertTrue(research.errors)

    def test_robots_txt_404_is_permissive_and_crawl_proceeds(self):
        def fake_fetch(url, timeout=10.0, **kwargs):
            if url.endswith("/robots.txt"):
                return acquisition.FetchResult(
                    requested_url=url, final_url=url, status=404, content_type="text/html", headers={},
                    body=b"not found", text="not found", redirect_chain=[], truncated=False, fetched_at=acquisition._now(),
                )
            html = "<html><body>ok</body></html>"
            return acquisition.FetchResult(
                requested_url=url, final_url=url, status=200, content_type="text/html", headers={},
                body=html.encode(), text=html, redirect_chain=[], truncated=False, fetched_at=acquisition._now(),
            )

        with mock.patch.object(acquisition, "fetch_public_url", side_effect=fake_fetch):
            research = acquisition.research_website("https://example.com/", max_pages=5)
        self.assertEqual(research.pages_fetched, 1)
        self.assertEqual(research.errors, [])

    def test_robots_txt_timeout_skips_host_instead_of_defaulting_permissive(self):
        def fake_fetch(url, timeout=10.0, **kwargs):
            if url.endswith("/robots.txt"):
                raise acquisition.FetchError("upstream_failed")
            raise AssertionError("must not crawl a host whose robots policy is unknown")

        with mock.patch.object(acquisition, "fetch_public_url", side_effect=fake_fetch):
            research = acquisition.research_website("https://example.com/", max_pages=5)
        self.assertEqual(research.pages_fetched, 0)
        self.assertEqual(len(research.errors), 1)
        self.assertIn("robots_fetch_failed", research.errors[0])

    def test_robots_txt_server_error_skips_host_instead_of_defaulting_permissive(self):
        def fake_fetch(url, timeout=10.0, **kwargs):
            if url.endswith("/robots.txt"):
                return acquisition.FetchResult(
                    requested_url=url, final_url=url, status=503, content_type="text/html", headers={},
                    body=b"", text="", redirect_chain=[], truncated=False, fetched_at=acquisition._now(),
                )
            raise AssertionError("must not crawl a host whose robots policy is unknown")

        with mock.patch.object(acquisition, "fetch_public_url", side_effect=fake_fetch):
            research = acquisition.research_website("https://example.com/", max_pages=5)
        self.assertEqual(research.pages_fetched, 0)
        self.assertIn("status_503", research.errors[0])

    def test_crawl_rejects_cross_host_redirect_with_blocked_reason(self):
        """Regression: robots.txt was checked for example.com only, so a page
        redirecting to another host must not be fetched from that host."""
        seen_kwargs = []

        def fake_fetch(url, timeout=10.0, **kwargs):
            if url.endswith("/robots.txt"):
                return acquisition.FetchResult(
                    requested_url=url, final_url=url, status=404, content_type="text/html", headers={},
                    body=b"", text="", redirect_chain=[], truncated=False, fetched_at=acquisition._now(),
                )
            seen_kwargs.append(kwargs)
            if not kwargs.get("allow_cross_host_redirects", True):
                raise acquisition.FetchError("cross_host_redirect_blocked")
            raise AssertionError("crawl must disable cross-host redirects")

        with mock.patch.object(acquisition, "fetch_public_url", side_effect=fake_fetch):
            research = acquisition.research_website("https://example.com/", max_pages=3)
        self.assertEqual(seen_kwargs, [{"allow_cross_host_redirects": False}])
        self.assertEqual(research.pages[0].error, "cross_host_redirect_blocked")
        self.assertIn("cross_host_redirect_blocked", research.errors[0])

    def test_crawl_discards_same_host_redirect_into_robots_disallowed_path(self):
        robots_txt = "User-agent: *\nDisallow: /private\n"

        def fake_fetch(url, timeout=10.0, **kwargs):
            if url.endswith("/robots.txt"):
                return acquisition.FetchResult(
                    requested_url=url, final_url=url, status=200, content_type="text/plain", headers={},
                    body=robots_txt.encode(), text=robots_txt, redirect_chain=[], truncated=False,
                    fetched_at=acquisition._now(),
                )
            html = "<html><body>secret info@example.com</body></html>"
            return acquisition.FetchResult(
                requested_url=url, final_url="https://example.com/private/x", status=200, content_type="text/html",
                headers={}, body=html.encode(), text=html, redirect_chain=["https://example.com/private/x"],
                truncated=False, fetched_at=acquisition._now(),
            )

        with mock.patch.object(acquisition, "fetch_public_url", side_effect=fake_fetch):
            research = acquisition.research_website("https://example.com/", max_pages=3)
        self.assertEqual(research.pages, [])
        self.assertEqual(research.contacts, [])
        self.assertEqual(research.robots_disallowed, ["https://example.com/private/x"])

    def test_before_fetch_runs_per_page_and_can_abort_crawl(self):
        class Stop(Exception):
            pass

        calls = []

        def before_fetch():
            calls.append(1)
            if len(calls) == 2:
                raise Stop()

        with mock.patch.object(acquisition, "fetch_public_url", side_effect=self._fake_fetch_factory()) as fetch:
            with self.assertRaises(Stop):
                acquisition.research_website("https://example.com/", max_pages=5, before_fetch=before_fetch)
        # robots.txt + the first page only; the second page is never fetched
        self.assertEqual([c.args[0] for c in fetch.call_args_list],
                         ["https://example.com/robots.txt", "https://example.com/"])

    def test_phone_regex_does_not_capture_dates(self):
        html = "<html><body>Founded 2024-01-01. Call +358 40 999 8888 or visit on 31.12.2023.</body></html>"

        def fake_fetch(url, timeout=10.0, **kwargs):
            if url.endswith("/robots.txt"):
                return acquisition.FetchResult(
                    requested_url=url, final_url=url, status=404, content_type="text/html", headers={},
                    body=b"", text="", redirect_chain=[], truncated=False, fetched_at=acquisition._now(),
                )
            return acquisition.FetchResult(
                requested_url=url, final_url=url, status=200, content_type="text/html", headers={},
                body=html.encode(), text=html, redirect_chain=[], truncated=False, fetched_at=acquisition._now(),
            )

        with mock.patch.object(acquisition, "fetch_public_url", side_effect=fake_fetch):
            research = acquisition.research_website("https://example.com/", max_pages=1)
        phones = {c.value for c in research.contacts if c.kind == "phone"}
        self.assertIn("+358 40 999 8888", phones)
        self.assertFalse(any("2024-01-01" in p or "31.12.2023" in p for p in phones))


# ---------------------------------------------------------------------------
# SearXNG client: explicit degraded status, never raises for reachability
# ---------------------------------------------------------------------------


class SearchWebTests(unittest.TestCase):
    def test_parses_results_from_local_searxng(self):
        payload = {
            "results": [
                {"title": "Example", "url": "https://example.com", "content": "snippet", "engine": "duckduckgo"}
            ]
        }
        body = json.dumps(payload).encode()

        class _Resp:
            def read(self, n=None):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(acquisition.urllib.request, "urlopen", return_value=_Resp()):
            result = acquisition.search_web("acme oy")
        self.assertFalse(result.degraded)
        self.assertEqual(result.results[0].url, "https://example.com")

    def test_degrades_instead_of_raising_when_unreachable(self):
        with mock.patch.object(
            acquisition.urllib.request, "urlopen", side_effect=urllib.error.URLError("connection refused")
        ):
            result = acquisition.search_web("acme oy")
        self.assertTrue(result.degraded)
        self.assertIn("searxng_unreachable", result.error)

    def test_invalid_base_url_is_degraded_without_network_call(self):
        with mock.patch.object(acquisition.urllib.request, "urlopen") as urlopen:
            result = acquisition.search_web("acme oy", base_url="not-a-url")
        self.assertTrue(result.degraded)
        urlopen.assert_not_called()

    def test_unresponsive_engines_marks_degraded(self):
        payload = {"results": [], "unresponsive_engines": [["duckduckgo", "timeout"]]}
        body = json.dumps(payload).encode()

        class _Resp:
            def read(self, n=None):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(acquisition.urllib.request, "urlopen", return_value=_Resp()):
            result = acquisition.search_web("acme oy")
        self.assertTrue(result.degraded)


if __name__ == "__main__":
    unittest.main()
