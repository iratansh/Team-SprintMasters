"""Public HTTP transport security, cache isolation, and bounded failure checks."""

import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from hotel_search.providers import ProviderError
from hotel_search.web_http import PublicWebClient, _PublicRedirect, public_url


class PublicWebClientTests(unittest.TestCase):
    URL = "https://hotel.example.test/rates?checkin=2028-01-01&checkout=2028-01-03&adults=2"
    PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    def setUp(self):
        self.dns = patch("hotel_search.web_http.socket.getaddrinfo", return_value=self.PUBLIC_DNS)
        self.dns.start()
        self.addCleanup(self.dns.stop)
        self.client = PublicWebClient(cache_dir=None, retries=1)

    def response(self, payload=b"<html>Rates</html>", *, url=None, content_type="text/html; charset=utf-8", encoding=None):
        response = MagicMock()
        response.headers = {"Content-Type": content_type}
        if encoding:
            response.headers["Content-Encoding"] = encoding
        response.read.return_value = payload
        response.geturl.return_value = url or self.URL
        response.__enter__.return_value = response
        return response

    def test_fetch_uses_supplied_public_url_without_paid_service_or_key_injection(self):
        response = self.response()
        with patch("hotel_search.web_http.build_opener") as opener:
            opener.return_value.open.return_value = response
            result = self.client.get(self.URL)
        request = opener.return_value.open.call_args.args[0]
        self.assertEqual(request.full_url, self.URL)
        self.assertNotIn("serpapi", request.full_url)
        self.assertFalse(any("key" in key.lower() or "authorization" in key.lower() for key in request.headers))
        self.assertEqual(result["text"], "<html>Rates</html>")
        self.assertIsNone(result["observed_at"])
        self.assertFalse(result["from_cache"])
        self.assertEqual(self.client.request_count, 1)

    def test_credentials_non_http_nonstandard_ports_and_private_dns_rejected_before_network(self):
        invalid = ["file:///etc/passwd", "https://user:secret@example.test", "http://example.test:8080", "javascript:alert(1)"]
        with patch("hotel_search.web_http.build_opener") as opener:
            for url in invalid:
                with self.subTest(url=url), self.assertRaises(ProviderError) as caught:
                    self.client.get(url)
                self.assertEqual(caught.exception.code, "invalid_url")
            opener.assert_not_called()
        for address in ("127.0.0.1", "10.0.0.2", "169.254.169.254", "::1", "fc00::1"):
            with patch("hotel_search.web_http.socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 80))]), \
                 self.subTest(address=address), self.assertRaises(ProviderError):
                public_url("https://hotel.example.test")

    def test_mixed_public_and_private_dns_and_resolution_failure_rejected(self):
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.2", 443))
        for answer in ([], self.PUBLIC_DNS + [private]):
            with patch("hotel_search.web_http.socket.getaddrinfo", return_value=answer), self.assertRaises(ProviderError):
                public_url(self.URL)
        with patch("hotel_search.web_http.socket.getaddrinfo", side_effect=socket.gaierror("private")), self.assertRaises(ProviderError) as caught:
            public_url(self.URL)
        self.assertNotIn("private", str(caught.exception))

    def test_redirect_destination_revalidated_and_public_redirect_allowed(self):
        handler = _PublicRedirect()
        request = Request(self.URL)
        with patch("hotel_search.web_http.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 80))]), \
             self.assertRaises(ProviderError):
            handler.redirect_request(request, None, 302, "Found", {}, "http://private.example.test")
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://public.example.test/rates")
        self.assertEqual(redirected.full_url, "https://public.example.test/rates")

    def test_final_response_url_is_revalidated_even_if_opener_skips_redirect_handler(self):
        with patch("hotel_search.web_http.build_opener") as opener, self.assertRaises(ProviderError) as caught:
            opener.return_value.open.return_value = self.response(url="https://user:secret@hotel.example.test")
            self.client.get(self.URL)
        self.assertEqual(caught.exception.code, "invalid_url")

    def test_transient_errors_retry_once_and_count_attempts(self):
        failures = [HTTPError(self.URL, 429, "private", {}, io.BytesIO()),
                    HTTPError(self.URL, 503, "private", {}, io.BytesIO()),
                    TimeoutError("private"), URLError(TimeoutError("private"))]
        for failure in failures:
            client = PublicWebClient(cache_dir=None, retries=1)
            with self.subTest(failure=type(failure).__name__), \
                 patch("hotel_search.web_http.build_opener") as opener, patch("hotel_search.web_http.time.sleep") as pause:
                opener.return_value.open.side_effect = [failure, self.response()]
                self.assertEqual(client.get(self.URL)["text"], "<html>Rates</html>")
                self.assertEqual(client.request_count, 2)
                pause.assert_called_once()

    def test_retry_exhaustion_is_bounded_and_errors_are_redacted(self):
        with patch("hotel_search.web_http.build_opener") as opener, patch("hotel_search.web_http.time.sleep"):
            opener.return_value.open.side_effect = TimeoutError("secret detail")
            with self.assertRaises(ProviderError) as caught:
                self.client.get(self.URL)
        self.assertEqual(self.client.request_count, 2)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertNotIn("secret", str(caught.exception))

    def test_access_and_non_timeout_network_failures_never_retry(self):
        failures = [(HTTPError(self.URL, 403, "secret", {}, io.BytesIO()), "website_blocked"),
                    (HTTPError(self.URL, 404, "secret", {}, io.BytesIO()), "http_error"),
                    (URLError("secret"), "network_error")]
        for failure, code in failures:
            client = PublicWebClient(cache_dir=None, retries=1)
            with patch("hotel_search.web_http.build_opener") as opener, patch("hotel_search.web_http.time.sleep") as pause:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(ProviderError) as caught:
                    client.get(self.URL)
            self.assertEqual(client.request_count, 1)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("secret", str(caught.exception))
            pause.assert_not_called()

    def test_exact_query_cache_preserves_dates_guests_and_tracks_hits(self):
        with tempfile.TemporaryDirectory() as directory, patch("hotel_search.web_http.build_opener") as opener:
            client = PublicWebClient(cache_dir=directory)
            opener.return_value.open.return_value = self.response()
            first = client.get(self.URL)
            second = client.get(self.URL)
            self.assertFalse(first["from_cache"])
            self.assertTrue(second["from_cache"])
            self.assertEqual(client.request_count, 1)
            self.assertEqual(client.cache_hits, 1)
            other_date = self.URL.replace("checkin=2028-01-01", "checkin=2028-01-02")
            other_guests = self.URL.replace("adults=2", "adults=3")
            client.get(other_date)
            client.get(other_guests)
            self.assertEqual(client.request_count, 3)
            self.assertEqual(len(list(Path(directory).glob("*.json"))), 3)

    def test_expired_wrong_url_and_private_final_cached_records_are_not_used(self):
        with tempfile.TemporaryDirectory() as directory, patch("hotel_search.web_http.build_opener") as opener:
            client = PublicWebClient(cache_dir=directory)
            opener.return_value.open.return_value = self.response()
            initial = client.get(self.URL)
            path = Path(directory) / (hashlib.sha256(self.URL.encode()).hexdigest() + ".json")
            for change in ({"cached_at": 0}, {"url_requested": self.URL + "&other=true"}, {"url": "file:///private"}, {"text": []}):
                cached = copy.deepcopy(initial)
                cached.update(change)
                path.write_text(json.dumps(cached), encoding="utf-8")
                self.assertFalse(client.get(self.URL)["from_cache"])
            self.assertEqual(client.request_count, 5)
            self.assertEqual(client.cache_hits, 0)

    def test_fresh_forces_fetch_and_corrupt_cache_is_replaced(self):
        with tempfile.TemporaryDirectory() as directory, patch("hotel_search.web_http.build_opener") as opener:
            client = PublicWebClient(cache_dir=directory, fresh=True)
            opener.return_value.open.return_value = self.response()
            client.get(self.URL)
            client.get(self.URL)
            self.assertEqual(client.request_count, 2)
            self.assertEqual(client.cache_hits, 0)
            path = next(Path(directory).glob("*.json"))
            path.write_text("malformed", encoding="utf-8")
            client.fresh = False
            self.assertFalse(client.get(self.URL)["from_cache"])
            self.assertEqual(client.request_count, 3)

    def test_wire_response_size_is_bounded_and_binary_content_is_rejected(self):
        for response, code in [(self.response(payload=b"x" * (8 * 1024 * 1024 + 1)), "response_too_large"),
                               (self.response(content_type="image/png"), "unsupported_content")]:
            with patch("hotel_search.web_http.build_opener") as opener, self.assertRaises(ProviderError) as caught:
                opener.return_value.open.return_value = response
                self.client.get(self.URL)
            self.assertEqual(caught.exception.code, code)
            response.read.assert_called_once_with(8 * 1024 * 1024 + 1)

    def test_gzip_and_unknown_charset_decode_without_changing_provenance(self):
        compressed = gzip.compress("Rate: café".encode())
        with patch("hotel_search.web_http.build_opener") as opener:
            opener.return_value.open.return_value = self.response(compressed, encoding="gzip", content_type="text/html; charset=not-a-real-charset")
            result = self.client.get(self.URL)
        self.assertEqual(result["text"], "Rate: café")
        self.assertEqual(result["url_requested"], self.URL)
        with patch("hotel_search.web_http.build_opener") as opener:
            opener.return_value.open.return_value = self.response(b"\xff")
            self.assertEqual(self.client.get(self.URL)["text"], "\ufffd")

    def test_expanded_gzip_size_is_rejected(self):
        payload = gzip.compress(b"x" * (16 * 1024 * 1024 + 1))
        with patch("hotel_search.web_http.build_opener") as opener, self.assertRaises(ProviderError) as caught:
            opener.return_value.open.return_value = self.response(payload, encoding="gzip")
            self.client.get(self.URL)
        self.assertEqual(caught.exception.code, "response_too_large")

    def test_malformed_and_truncated_gzip_are_classified_provider_failures(self):
        for payload in (b"invalid gzip", gzip.compress(b"hello")[:-4]):
            with patch("hotel_search.web_http.build_opener") as opener, self.subTest(payload=payload), self.assertRaises(ProviderError):
                opener.return_value.open.return_value = self.response(payload, encoding="gzip")
                self.client.get(self.URL)


if __name__ == "__main__":
    unittest.main()
