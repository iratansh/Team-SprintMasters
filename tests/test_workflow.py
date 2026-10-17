from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from hotel_search.fixture import FixtureProvider
from hotel_search.models import SearchRequest
from hotel_search.providers import ProviderError
from hotel_search.workflow import search_hotels


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.trip = {"city": "Kingston", "check_in": "2028-01-01", "check_out": "2028-01-03", "guests": 2}
        self.request = SearchRequest.from_dict(self.trip)

    def listing(self, index, amount=200):
        return {"name": f"Inn {index}", "property_token": f"token-{index}",
                "total_rate": {"extracted_lowest": amount}}

    def provider(self, rows, pages=None, details=None):
        if pages is None:
            pages = [{"input_page_token": None, "response": {"properties": rows}}]
        if details is None:
            details = {row["property_token"]: {**row,
                "address": "12 Main Street, Kingston, ON, Canada", "phone": "613-555-0123"}
                for row in rows}
        fixture = {"synthetic_fixture": True, "request": self.trip, "search_pages": pages, "details": details}
        return FixtureProvider(fixture, self.request)

    def test_default_retains_all_results_and_limits_only_shortlist(self):
        rows = [self.listing(i, 200 + i) for i in range(9)]
        result = search_hotels(self.request, self.provider(rows))
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["hotels"]), 9)
        self.assertEqual(len(result["shortlist"]), 5)
        self.assertFalse(result["coverage"]["candidate_limit_applied"])
        self.assertIsNone(result["coverage"]["candidate_limit"])
        self.assertFalse(result["coverage"]["all_market_coverage"])

    def test_pagination_deduplicates_properties_and_stops_repeated_token(self):
        rows = [self.listing(1), self.listing(2)]
        pages = [
            {"input_page_token": None, "response": {"properties": [rows[0]], "serpapi_pagination": {"next_page_token": "page2"}}},
            {"input_page_token": "page2", "response": {"properties": rows, "serpapi_pagination": {"next_page_token": "page2"}}},
        ]
        provider = self.provider(rows, pages=pages)
        with patch.object(provider, "search", wraps=provider.search) as search:
            result = search_hotels(self.request, provider, max_pages=10)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(len(result["hotels"]), 2)
        self.assertEqual(result["coverage"]["modes"][0]["stop_reason"], "repeated_page_token")
        self.assertTrue(result["coverage"]["modes"][0]["has_more_pages"])

    def test_page_limit_and_invalid_pagination_explicitly_report_incomplete_coverage(self):
        for token, stop in [("more", "page_limit"), (42, "invalid_pagination")]:
            row = self.listing(1)
            pages = [{"input_page_token": None, "response": {"properties": [row], "serpapi_pagination": {"next_page_token": token}}}]
            with self.subTest(token=token):
                result = search_hotels(self.request, self.provider([row], pages=pages))
                self.assertEqual(result["coverage"]["modes"][0]["stop_reason"], stop)
                self.assertTrue(result["coverage"]["modes"][0]["has_more_pages"])

    def test_detail_failure_preserves_listing_and_unknown_contacts(self):
        row = self.listing(1)
        result = search_hotels(self.request, self.provider([row], details={}))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["hotels"]), 1)
        hotel = result["hotels"][0]
        self.assertIsNone(hotel["phone"])
        self.assertIsNone(hotel["address"])
        self.assertFalse(hotel["call_ready"])
        self.assertEqual(result["shortlist"], [])
        self.assertIn("property_details_unavailable", hotel["warnings"])
        self.assertEqual(result["errors"][0]["step"], "details")

    def test_search_echo_mismatch_is_fatal(self):
        for echo in [{"adults": 1}, {"currency": "USD"}, {"check_in_date": "2028-02-01"}, {"children": 1}]:
            pages = [{"input_page_token": None, "response": {"properties": [], "search_parameters": echo}}]
            with self.subTest(echo=echo), self.assertRaises(ProviderError) as caught:
                search_hotels(self.request, self.provider([], pages=pages))
            self.assertEqual(caught.exception.code, "query_mismatch")

    def test_detail_echo_mismatch_cannot_inject_rate_or_contacts(self):
        row = self.listing(1, 250)
        details = {row["property_token"]: {**row, "phone": "613-555-0999",
            "total_rate": {"extracted_lowest": 1}, "search_parameters": {"adults": 1}}}
        result = search_hotels(self.request, self.provider([row], details=details))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["hotels"][0]["online_quote"]["amount"], 250)
        self.assertIsNone(result["hotels"][0]["phone"])
        self.assertEqual(result["errors"][0]["code"], "query_mismatch")

    def test_nearby_city_and_unknown_full_stay_price_excluded_from_shortlist(self):
        rows = [self.listing(i) for i in range(3)]
        provider = self.provider(rows)
        provider.fixture["details"]["token-1"]["address"] = "12 Kingston Street, Gananoque, ON, Canada"
        provider.fixture["details"]["token-2"].pop("total_rate")
        provider.fixture["details"]["token-2"]["rate_per_night"] = {"extracted_lowest": 50}
        # Unknown means neither source has a full-stay price; contact-only
        # details must otherwise preserve a valid dated search-page quote.
        provider.fixture["search_pages"][0]["response"]["properties"][2].pop("total_rate")
        result = search_hotels(self.request, provider)
        ready = [h for h in result["hotels"] if h["call_ready"]]
        self.assertEqual([h["property_name"] for h in ready], ["Inn 0"])
        self.assertEqual(result["shortlist"], [ready[0]["candidate_id"]])
        self.assertEqual(len(result["hotels"]), 3)

    def test_no_results_is_distinct_from_provider_error(self):
        for code, status in [("no_results", "no_results"), ("network_error", "error")]:
            provider = self.provider([])
            with self.subTest(code=code), patch.object(provider, "search", side_effect=ProviderError("Unavailable", code)):
                result = search_hotels(self.request, provider)
            self.assertEqual(result["status"], status)
            self.assertEqual(result["hotels"], [])
            self.assertEqual(bool(result["errors"]), code != "no_results")

    def test_explicit_candidate_limit_keeps_cheapest_listing_and_reports_coverage(self):
        rows = [self.listing(1, 300), self.listing(2, 120), self.listing(3, 200)]
        provider = self.provider(rows)
        with patch.object(provider, "details", wraps=provider.details) as details:
            result = search_hotels(self.request, provider, limit=1)
        self.assertEqual(details.call_count, 1)
        self.assertEqual(result["hotels"][0]["property_name"], "Inn 2")
        self.assertEqual(result["coverage"]["candidates_seen"], 3)
        self.assertEqual(result["coverage"]["candidates_returned"], 1)
        self.assertTrue(result["coverage"]["candidate_limit_applied"])

    def test_missing_token_is_retained_with_detail_error(self):
        row = self.listing(1)
        row.pop("property_token")
        provider = self.provider([], pages=[{"input_page_token": None, "response": {"properties": [row]}}])
        result = search_hotels(self.request, provider)
        self.assertEqual(len(result["hotels"]), 1)
        self.assertEqual(result["errors"][0]["code"], "missing_property_token")
        self.assertFalse(result["hotels"][0]["call_ready"])

    def test_bounds_are_validated_before_any_provider_call(self):
        for options in [{"max_pages": 0}, {"max_pages": 11}, {"workers": 0}, {"limit": -1}, {"limit": 501}, {"shortlist_size": 0}]:
            provider = self.provider([])
            with self.subTest(options=options), patch.object(provider, "search") as search:
                with self.assertRaises(ValueError):
                    search_hotels(self.request, provider, **options)
                search.assert_not_called()

    def test_rentals_are_separate_mode_and_events_are_structured(self):
        events = []
        provider = self.provider([self.listing(1)])
        with patch.object(provider, "search", wraps=provider.search) as search:
            result = search_hotels(self.request, provider, include_vacation_rentals=True, event=events.append)
        self.assertEqual([call.kwargs["vacation_rentals"] for call in search.call_args_list], [False, True])
        self.assertEqual([m["mode"] for m in result["coverage"]["modes"]], ["hotels", "vacation_rentals"])
        self.assertTrue(events)
        self.assertEqual({event["run_id"] for event in events}, {result["run_id"]})
        self.assertTrue(all(event["tokens"] == 0 and event["elapsed_ms"] >= 0 for event in events))

    def test_bundled_fixture_is_network_free_and_never_supplies_callable_shortlist(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "examples/search.fixture.json").read_text())
        request = SearchRequest.from_dict(fixture["request"])
        with patch("hotel_search.providers.urlopen", side_effect=AssertionError("network must not run")):
            result = search_hotels(request, FixtureProvider(deepcopy(fixture), request), mode="fixture")
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["hotels"])
        self.assertEqual(result["shortlist"], [])
        self.assertTrue(all(not h["call_ready"] for h in result["hotels"]))
        self.assertTrue(any(h["fixture_would_be_call_ready"] for h in result["hotels"]))
        self.assertEqual(result["metrics"]["http_requests"], 0)
        self.assertEqual(result["metrics"]["llm_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
