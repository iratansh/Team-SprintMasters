from copy import deepcopy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from hotel_search import cli
from hotel_search.direct import DirectWebProvider
from hotel_search.fixture import FixtureProvider
from hotel_search.models import SearchRequest
from hotel_search.providers import ProviderError
from hotel_search.workflow import search_hotels


TRIP = {"check_in": "2026-11-06", "check_out": "2026-11-08", "guests": 2}
REQUEST = SearchRequest.from_dict(TRIP)
STAMP = "2026-10-17T14:00:00+00:00"
GOOGLE_URL = "https://www.google.com/travel/search?q=Ontario"
WEBSITE_URL = "https://example.test/"


def property_row(token="direct-one", **changes):
    return {"name": "Example Hotel", "property_token": token,
            "address": "10 Main Street, Kingston, ON K7L 1A1, Canada",
            "phone": "+1 613 555 0100", "link": None,
            "_source_url": GOOGLE_URL, "_fetched_at": STAMP,
            "_provider_observed_at": None, **changes}


def response(text, url=WEBSITE_URL):
    return {"text": text, "url": url, "fetched_at": STAMP, "from_cache": False}


def website_page(name="Example Hotel", offer=None):
    hotel = {"@type": "Hotel", "name": name, "telephone": "+1 613 555 0100",
             "address": {"streetAddress": "10 Main Street", "addressLocality": "Kingston",
                         "addressRegion": "ON", "postalCode": "K7L 1A1", "addressCountry": "CA"}}
    if offer is not None:
        hotel["offers"] = offer
    return '<script type="application/ld+json">' + json.dumps(hotel) + '</script>'


class DirectProviderTests(unittest.TestCase):
    def test_same_hotel_brand_cannot_override_conflicting_branch_address(self):
        left = property_row()
        right = property_row(address="99 Other Street, Ottawa, ON, Canada")
        self.assertFalse(DirectWebProvider._same_property(left, right))

    def client(self):
        return MagicMock(request_count=0, cache_hits=0)

    def google(self, rows=None):
        google = MagicMock()
        rows = [property_row()] if rows is None else rows
        google.search.return_value = {"properties": deepcopy(rows), "_source_url": GOOGLE_URL, "_fetched_at": STAMP}
        google.details.side_effect = lambda request, token, **kwargs: deepcopy(next(row for row in rows if row["property_token"] == token))
        return google

    def test_auto_google_primary_uses_no_paid_provider_or_osm(self):
        provider = DirectWebProvider(client=self.client())
        google = self.google()
        with patch.object(provider, "_google", return_value=google), \
             patch("hotel_search.direct.discover_properties") as osm, \
             patch("hotel_search.providers.SerpApiProvider") as paid:
            result = search_hotels(REQUEST, provider)
        paid.assert_not_called()
        osm.assert_not_called()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["coverage"]["provider"], "direct_web")
        self.assertEqual(result["coverage"]["source_details"]["paid_search_requests"], 0)
        self.assertEqual(result["coverage"]["source_details"]["sources_used"], ["google_hotels_direct"])

    def test_google_layout_or_context_failure_falls_back_with_diagnostic(self):
        for code in ["layout_changed", "query_mismatch"]:
            with self.subTest(code=code):
                provider = DirectWebProvider(client=self.client())
                google = self.google()
                google.search.side_effect = ProviderError("Cannot establish the live search context.", code)
                lead = property_row("osm:node:123", discovery_source="openstreetmap")
                with patch.object(provider, "_google", return_value=google), \
                     patch("hotel_search.direct.discover_properties", return_value=[lead]) as osm:
                    result = search_hotels(REQUEST, provider)
                osm.assert_called_once()
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["errors"][0]["code"], code)
                self.assertEqual(result["errors"][0]["source"], "google_hotels_direct")
                self.assertEqual(result["coverage"]["source_details"]["sources_used"], ["openstreetmap"])

    def test_osm_unpriced_leads_are_enquiry_ready_not_priced_shortlist(self):
        provider = DirectWebProvider(source="osm", client=self.client())
        lead = property_row("osm:node:123", discovery_source="openstreetmap", _source_url="https://www.openstreetmap.org/node/123")
        with patch("hotel_search.direct.discover_properties", return_value=[lead]):
            result = search_hotels(REQUEST, provider)
        hotel = result["hotels"][0]
        self.assertIsNone(hotel["online_quote"]["amount"])
        self.assertEqual(hotel["availability_status"], "unknown")
        self.assertTrue(hotel["enquiry_ready"])
        self.assertFalse(hotel["call_ready"])
        self.assertEqual(result["shortlist"], [])
        self.assertEqual(result["enquiry_shortlist"], [hotel["candidate_id"]])
        self.assertEqual(result["metrics"]["estimated_api_cost"], 0)

    def test_seed_website_uses_real_extractor_and_http_evidence(self):
        client = self.client()
        client.get.return_value = response(website_page())
        provider = DirectWebProvider(source="websites", seed_urls=[WEBSITE_URL], client=client)
        result = search_hotels(REQUEST, provider)
        client.get.assert_called_once_with(WEBSITE_URL)
        hotel = result["hotels"][0]
        self.assertEqual(hotel["property_name"], "Example Hotel")
        self.assertEqual(hotel["phone"], "+16135550100")
        self.assertTrue(hotel["enquiry_ready"])
        self.assertEqual(hotel["source_url"], WEBSITE_URL)
        self.assertEqual(hotel["retrieved_at"], STAMP)

    def test_website_contacts_cannot_be_injected_into_different_named_property(self):
        client = self.client()
        client.get.return_value = response(website_page(name="Unrelated Motel"))
        lead = property_row(phone=None, link=WEBSITE_URL)
        provider = DirectWebProvider(source="osm", client=client)
        with patch("hotel_search.direct.discover_properties", return_value=[lead]):
            result = search_hotels(REQUEST, provider)
        hotel = result["hotels"][0]
        self.assertIsNone(hotel["phone"])
        self.assertFalse(hotel["enquiry_ready"])
        self.assertIn("website_property_identity_mismatch", hotel["warnings"])

    def test_matching_website_contacts_preserve_google_listing_price_provenance(self):
        client = self.client()
        client.get.return_value = response(website_page())
        lead = property_row(phone=None, link=WEBSITE_URL, total_rate={"extracted_lowest": 210},
                            _price_scope={**TRIP, "currency": "CAD"})
        google = self.google([lead])
        google.details.return_value = property_row(phone=None, link=WEBSITE_URL)
        google.details.side_effect = None
        provider = DirectWebProvider(client=client)
        with patch.object(provider, "_google", return_value=google):
            result = search_hotels(REQUEST, provider)
        hotel = result["hotels"][0]
        self.assertEqual(hotel["phone"], "+16135550100")
        self.assertEqual(hotel["online_quote"]["amount"], 210)
        self.assertEqual(hotel["online_quote"]["source_url"], GOOGLE_URL)
        self.assertEqual(hotel["contact_source_url"], WEBSITE_URL)

    def test_matching_scoped_website_quote_supplies_price_and_correct_provenance(self):
        client = self.client()
        offer = {"@type": "Offer", "checkIn": TRIP["check_in"], "checkOut": TRIP["check_out"],
                 "numAdults": 2, "totalPrice": "180", "priceCurrency": "CAD"}
        client.get.return_value = response(website_page(offer=offer))
        lead = property_row("osm:node:123", phone=None, link=WEBSITE_URL)
        provider = DirectWebProvider(source="osm", client=client)
        with patch("hotel_search.direct.discover_properties", return_value=[lead]):
            result = search_hotels(REQUEST, provider)
        hotel = result["hotels"][0]
        self.assertEqual(hotel["online_quote"]["amount"], 180)
        self.assertEqual(hotel["online_quote"]["source_url"], WEBSITE_URL)
        self.assertTrue(hotel["call_ready"])
        self.assertEqual(result["shortlist"], [hotel["candidate_id"]])

    def test_pagination_does_not_repeat_seed_fetch_or_osm(self):
        provider = DirectWebProvider(seed_urls=[WEBSITE_URL], client=self.client())
        google = self.google()
        google.search.side_effect = [
            {"properties": [property_row()], "serpapi_pagination": {"next_page_token": "second"}},
            {"properties": [property_row("direct-two", name="Second Hotel")]},
        ]
        seed = property_row("web:seed", name="Seed Inn")
        with patch.object(provider, "_google", return_value=google), \
             patch.object(provider, "_website", return_value=seed) as fetch, \
             patch("hotel_search.direct.discover_properties") as osm:
            first = provider.search(REQUEST.to_dict())
            second = provider.search(REQUEST.to_dict(), next_page_token="second")
        fetch.assert_called_once_with(WEBSITE_URL, REQUEST.to_dict())
        osm.assert_not_called()
        self.assertEqual(len(first["properties"]), 2)
        self.assertEqual(len(second["properties"]), 1)
        self.assertEqual(google.search.call_args.kwargs["next_page_token"], "second")

    def test_forced_google_errors_propagate_without_osm_fallback(self):
        provider = DirectWebProvider(source="google", client=self.client())
        google = self.google()
        google.search.side_effect = ProviderError("Layout unsupported.", "layout_changed")
        with patch.object(provider, "_google", return_value=google), \
             patch("hotel_search.direct.discover_properties") as osm:
            with self.assertRaises(ProviderError) as caught:
                provider.search(REQUEST.to_dict())
        self.assertEqual(caught.exception.code, "layout_changed")
        osm.assert_not_called()

    def test_all_sources_failing_keeps_original_diagnostics(self):
        provider = DirectWebProvider(client=self.client())
        google = self.google()
        google.search.side_effect = ProviderError("Rate limited.", "rate_limited")
        with patch.object(provider, "_google", return_value=google), \
             patch("hotel_search.direct.discover_properties", side_effect=ProviderError("OSM unreachable.", "network_error")):
            with self.assertRaises(ProviderError) as caught:
                provider.search(REQUEST.to_dict())
        self.assertEqual(caught.exception.code, "discovery_unavailable")
        self.assertEqual([item["code"] for item in provider.diagnostics], ["rate_limited", "network_error"])

    def test_contact_fetch_failure_is_explicit_and_preserves_lead(self):
        client = self.client()
        client.get.side_effect = ProviderError("Public page blocked.", "blocked")
        provider = DirectWebProvider(source="osm", client=client)
        lead = property_row("osm:node:123", phone=None, link=WEBSITE_URL)
        with patch("hotel_search.direct.discover_properties", return_value=[lead]):
            result = search_hotels(REQUEST, provider)
        self.assertEqual(result["status"], "partial")
        self.assertIn("website_contact_enrichment_unavailable", result["hotels"][0]["warnings"])
        self.assertEqual(result["errors"][0]["code"], "blocked")

    def test_configuration_and_unknown_identity_errors_have_stable_codes(self):
        cases = [({"source": "bad"}, "configuration_error"),
                 ({"source": "websites"}, "configuration_error"),
                 ({"seed_urls": ["file:///private"]}, "invalid_input"),
                 ({"max_properties": 0}, "invalid_input")]
        for kwargs, code in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ProviderError) as caught:
                DirectWebProvider(client=self.client(), **kwargs)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ProviderError) as caught:
            DirectWebProvider(client=self.client()).details(REQUEST.to_dict(), "unknown")
        self.assertEqual(caught.exception.code, "invalid_property")


class DirectCliTests(unittest.TestCase):
    def test_cli_defaults_to_direct_without_reading_key_file_or_using_serpapi(self):
        row = property_row(total_rate={"extracted_lowest": 200})
        fixture = {"synthetic_fixture": True, "request": TRIP,
                   "search_pages": [{"input_page_token": None, "response": {"properties": [row]}}],
                   "details": {row["property_token"]: row}}
        provider = FixtureProvider(fixture, REQUEST)
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "hotels.json"
            with patch.dict(os.environ, {}, clear=True), \
                 patch("hotel_search.cli.DirectWebProvider", return_value=provider) as direct, \
                 patch("hotel_search.cli.SerpApiProvider") as paid, \
                 patch("hotel_search.cli.load_env") as load, \
                 patch("sys.stderr", new_callable=io.StringIO):
                code = cli.main(["--check-in", TRIP["check_in"], "--check-out", TRIP["check_out"],
                                 "--guests", "2", "--output", str(destination)])
            result = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        direct.assert_called_once()
        self.assertEqual(direct.call_args.kwargs["source"], "auto")
        paid.assert_not_called()
        load.assert_not_called()
        self.assertEqual(len(result["hotels"]), 1)


if __name__ == "__main__":
    unittest.main()
