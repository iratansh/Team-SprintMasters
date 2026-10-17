import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from hotel_search.models import SearchRequest
from hotel_search.providers import ProviderError, SerpApiProvider


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.secret = "TEST_PRIVATE_API_KEY"
        self.provider = SerpApiProvider(self.secret, timeout=4, retries=1, fresh=True)
        self.request = SearchRequest("Kingston", "2028-01-01", "2028-01-03", 2).to_dict()
        self.payload = {"search_metadata": {"status": "Success", "created_at": "2028-01-01T00:00:00Z"},
                        "properties": []}

    @staticmethod
    def response(payload):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return response

    def test_search_pagination_and_rental_parameters_preserve_stay(self):
        with patch("hotel_search.providers.urlopen", return_value=self.response(self.payload)) as fetch:
            result = self.provider.search(self.request, next_page_token="page-2+token", vacation_rentals=True)
        params = parse_qs(urlsplit(fetch.call_args.args[0].full_url).query)
        expected = {"engine": "google_hotels", "q": "Kingston, Ontario, Canada",
            "check_in_date": "2028-01-01", "check_out_date": "2028-01-03", "adults": "2",
            "children": "0", "currency": "CAD", "gl": "ca", "hl": "en", "sort_by": "3",
            "api_key": self.secret, "next_page_token": "page-2+token", "vacation_rentals": "true",
            "no_cache": "true"}
        self.assertEqual(params, {key: [value] for key, value in expected.items()})
        self.assertEqual(fetch.call_args.kwargs["timeout"], 4)
        self.assertEqual(self.provider.request_count, 1)
        self.assertEqual(result["_provider_observed_at"], "2028-01-01T00:00:00Z")
        self.assertIsNotNone(result["_fetched_at"])
        self.assertNotIn(self.secret, result["_source_url"])

    def test_details_uses_exact_token_and_no_search_sort(self):
        with patch("hotel_search.providers.urlopen", return_value=self.response(self.payload)) as fetch:
            self.provider.details(self.request, "exact-property-token")
        params = parse_qs(urlsplit(fetch.call_args.args[0].full_url).query)
        self.assertEqual(params["property_token"], ["exact-property-token"])
        self.assertNotIn("sort_by", params)
        self.assertNotIn("next_page_token", params)
        self.assertEqual(params["adults"], ["2"])

    def test_configuration_and_missing_request_fail_before_network(self):
        with self.assertRaises(ProviderError) as caught:
            SerpApiProvider(" ")
        self.assertEqual(caught.exception.code, "configuration_error")
        with patch("hotel_search.providers.urlopen") as fetch:
            for action in [lambda: self.provider.search({}), lambda: self.provider.details(self.request, "")]:
                with self.assertRaises(ProviderError) as caught:
                    action()
                self.assertEqual(caught.exception.code, "invalid_input")
            fetch.assert_not_called()

    def test_transient_http_and_timeout_retries_are_bounded(self):
        for failure in [HTTPError("https://serpapi.com/?api_key=" + self.secret, 429, self.secret, {}, io.BytesIO()),
                        HTTPError("https://serpapi.com", 503, self.secret, {}, io.BytesIO()),
                        TimeoutError(self.secret), URLError(TimeoutError(self.secret))]:
            provider = SerpApiProvider(self.secret, retries=1)
            with self.subTest(failure=type(failure).__name__), \
                 patch("hotel_search.providers.urlopen", side_effect=[failure, self.response(self.payload)]) as fetch, \
                 patch.object(provider, "_backoff") as backoff:
                provider.search(self.request)
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(provider.request_count, 2)
                backoff.assert_called_once_with(0)

    def test_auth_and_client_errors_never_retry_or_expose_key(self):
        for status, code in [(401, "authentication_error"), (403, "authentication_error"), (400, "provider_error")]:
            failure = HTTPError("https://serpapi.com/?api_key=" + self.secret, status, self.secret, {}, io.BytesIO())
            with self.subTest(status=status), \
                 patch("hotel_search.providers.urlopen", side_effect=failure) as fetch, \
                 patch.object(self.provider, "_backoff") as backoff:
                with self.assertRaises(ProviderError) as caught:
                    self.provider.search(self.request)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(self.secret, str(caught.exception))
                self.assertEqual(fetch.call_count, 1)
                backoff.assert_not_called()

    def test_timeout_exhaustion_is_redacted(self):
        with patch("hotel_search.providers.urlopen", side_effect=TimeoutError(self.secret)) as fetch, \
             patch.object(self.provider, "_backoff"):
            with self.assertRaises(ProviderError) as caught:
                self.provider.search(self.request)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(fetch.call_count, 2)
        self.assertNotIn(self.secret, str(caught.exception))

    def test_connection_error_is_redacted(self):
        with patch("hotel_search.providers.urlopen", side_effect=URLError(self.secret)):
            with self.assertRaises(ProviderError) as caught:
                self.provider.search(self.request)
        self.assertEqual(caught.exception.code, "network_error")
        self.assertNotIn(self.secret, str(caught.exception))

    def test_provider_error_message_not_forwarded_and_no_results_classified(self):
        for message, code in [("Invalid API key: " + self.secret, "provider_error"),
                              ("Google Hotels hasn't returned any results", "no_results")]:
            payload = {"error": message}
            with self.subTest(code=code), patch("hotel_search.providers.urlopen", return_value=self.response(payload)):
                with self.assertRaises(ProviderError) as caught:
                    self.provider.search(self.request)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(self.secret, str(caught.exception))

    def test_malformed_json_nonobjects_and_failed_status_rejected(self):
        for payload in [b"<html>failure</html>", b"[]", b"null", b'{"search_metadata":{"status":"Error"}}', b"{}"]:
            response = MagicMock()
            response.__enter__.return_value.read.return_value = payload
            with self.subTest(payload=payload), patch("hotel_search.providers.urlopen", return_value=response):
                with self.assertRaises(ProviderError) as caught:
                    self.provider.search(self.request)
                self.assertEqual(caught.exception.code, "invalid_response")

    def test_only_trusted_google_source_url_is_retained(self):
        safe = "https://www.google.com/travel/hotels/entity/example?hl=en#fragment"
        unsafe_urls = ["https://attacker.example/travel/hotels", "https://www.google.com.evil.example/travel/hotels",
                       "https://www.google.com/travel/hotels?api_key=" + self.secret,
                       "https://www.google.com/travel/hotels?access_token=other", "javascript:alert(1)"]
        for source in [safe, *unsafe_urls]:
            payload = {"search_metadata": {"status": "Success", "google_hotels_url": source}}
            with self.subTest(source=source), patch("hotel_search.providers.urlopen", return_value=self.response(payload)):
                result = self.provider.search(self.request)
                if source == safe:
                    self.assertEqual(result["_source_url"], safe.split("#")[0])
                else:
                    self.assertTrue(result["_source_url"].startswith("https://www.google.com/travel/hotels?q="))
                self.assertNotIn(self.secret, result["_source_url"])


if __name__ == "__main__":
    unittest.main()
