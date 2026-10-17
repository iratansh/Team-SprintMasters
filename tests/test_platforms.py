"""Offline integration checks for concurrent multi-platform discovery."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

from hotel_search.cli import main
from hotel_search.direct import DirectWebProvider
from hotel_search.models import SearchRequest
from hotel_search.platforms import PLATFORMS, merge_properties, platform_names
from hotel_search.providers import ProviderError
from hotel_search.workflow import search_hotels


REQUEST = SearchRequest(check_in="2028-01-01", check_out="2028-01-03", guests=2)
TRIP = REQUEST.to_dict()
STAMP = "2026-10-17T16:00:00Z"


def row(platform="google", token=None, amount=200, **changes):
    result = {"name": "Example Hotel", "property_token": token or platform + ":one",
              "address": "10 Main Street, Kingston, ON K7L 1A1, Canada",
              "phone": "+1 613 555 0100", "link": None, "currency": "CAD",
              "total_rate": {"extracted_lowest": amount}, "_platform": platform,
              "_source_url": "https://www.google.com/travel/search" if platform == "google" else "https://www.booking.com/hotel/ca/example.html",
              "_fetched_at": STAMP, "_provider_observed_at": STAMP,
              "_price_evidence": {"provider": platform}}
    if platform != "google":
        result["_platform_offer"] = {"platform": platform, "amount": amount, "currency": "CAD",
            "basis": "stay", "scope_verified": True, "check_in": REQUEST.check_in,
            "check_out": REQUEST.check_out, "guests": REQUEST.guests,
            "source_url": result["_source_url"], "listing_url": result["_source_url"]}
    return {**result, **changes}


class PlatformTests(unittest.TestCase):
    def provider(self, platforms):
        return DirectWebProvider(platforms=platforms, client=MagicMock(request_count=0, cache_hits=0))

    def test_selection_is_case_insensitive_deduplicated_and_validated(self):
        self.assertEqual(platform_names(" Google, Airbnb,google,booking "), ["google", "airbnb", "booking"])
        for selection in ("", "unknown", "google,unknown", []):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                platform_names(selection)

    def test_omitting_platforms_keeps_existing_programmatic_single_source_behavior(self):
        provider = DirectWebProvider(client=MagicMock())
        with patch.object(provider, "_search_single_source", return_value={"properties": []}) as single:
            provider.search(TRIP)
        single.assert_called_once()

    def test_cli_defaults_to_all_platforms_without_loading_key_or_search_api(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        outcome = {"status": "success", "hotels": [], "shortlist": []}
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr), \
             patch("hotel_search.cli.DirectWebProvider") as provider, \
             patch("hotel_search.cli.SerpApiProvider") as paid, \
             patch("hotel_search.cli.load_env") as load_env, \
             patch("hotel_search.cli.search_hotels", return_value=outcome):
            code = main(["--check-in", "2028-01-01", "--check-out", "2028-01-03", "--output", "-"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "success")
        self.assertEqual(platform_names(provider.call_args.kwargs["platforms"]), list(PLATFORMS))
        paid.assert_not_called()
        load_env.assert_not_called()

    def test_cli_passes_explicit_selection(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
             patch("hotel_search.cli.DirectWebProvider") as provider, \
             patch("hotel_search.cli.search_hotels", return_value={"status": "success", "hotels": [], "shortlist": []}):
            code = main(["--platforms", "airbnb,booking", "--check-in", "2028-01-01", "--check-out", "2028-01-03", "--output", "-"])
        self.assertEqual(code, 0)
        self.assertEqual(provider.call_args.kwargs["platforms"], "airbnb,booking")

    def test_concurrent_source_failure_is_isolated_and_reported(self):
        provider = self.provider(["google", "airbnb"])
        rendezvous = threading.Barrier(2)

        def google(*args, **kwargs):
            rendezvous.wait(timeout=3)
            return {"properties": [row()], "_source_url": "https://www.google.com/travel/"}

        def airbnb(*args, **kwargs):
            rendezvous.wait(timeout=3)
            raise ProviderError("Public page blocked.", "website_blocked")

        with patch.object(provider, "_search_single_source", side_effect=google), \
             patch("hotel_search.airbnb_source.AirbnbSource.search", side_effect=airbnb):
            result = provider.search(TRIP)
        self.assertEqual(len(result["properties"]), 1)
        statuses = {s["platform"]: s for s in provider.coverage_summary["platform_status"]}
        self.assertEqual(statuses["airbnb"]["direct_search_status"], "blocked")
        self.assertEqual(statuses["google"]["direct_search_status"], "success")
        self.assertEqual(provider.diagnostics[0]["source"], "airbnb")

    def test_unexpected_native_parser_failure_does_not_abort_healthy_sources(self):
        provider = self.provider(["google", "airbnb"])
        with patch.object(provider, "_search_single_source", return_value={"properties": [row()]}), \
             patch("hotel_search.airbnb_source.AirbnbSource.search", side_effect=ValueError("Unexpected page shape")):
            result = provider.search(TRIP)
        self.assertEqual(len(result["properties"]), 1)
        self.assertTrue(any(s["platform"] == "airbnb" and s["direct_search_status"] == "unavailable"
                            for s in provider.coverage_summary["platform_status"]))

    def test_every_selected_ota_is_searched_once_and_one_block_does_not_hide_other_results(self):
        provider = self.provider(list(PLATFORMS))

        def ota(name, **kwargs):
            source = MagicMock()
            if name == "booking":
                source.search.side_effect = ProviderError("Challenge page.", "website_blocked")
            else:
                source.search.return_value = {"properties": [row(name, name=name + " Hotel")]}
            return source

        with patch.object(provider, "_search_single_source", return_value={"properties": [row()]}), \
             patch("hotel_search.airbnb_source.AirbnbSource.search", return_value={"properties": []}) as airbnb, \
             patch("hotel_search.ota_sources.OtaSource", side_effect=ota) as native:
            result = provider.search(TRIP)
        self.assertEqual(len(result["properties"]), 5)
        self.assertEqual(sorted(c.args[0] for c in native.call_args_list),
                         sorted(["booking", "priceline", "expedia", "hotels", "agoda"]))
        airbnb.assert_called_once()
        statuses = {s["platform"]: s for s in provider.coverage_summary["platform_status"]}
        self.assertEqual(statuses["booking"]["direct_search_status"], "blocked")
        self.assertEqual(statuses["airbnb"]["direct_search_status"], "no_results")
        self.assertEqual(set(statuses), set(PLATFORMS))

    def test_exact_name_full_address_merge_preserves_google_identity_all_offers_and_native_provenance(self):
        google = row(amount=200)
        booking = row("booking", amount=150)
        merged = merge_properties([booking, google], TRIP)
        self.assertEqual(len(merged), 1)
        hotel = merged[0]
        self.assertEqual(hotel["property_token"], google["property_token"])
        self.assertEqual(set(hotel["_platforms"]), {"google", "booking"})
        self.assertEqual(len(hotel["_platform_offers"]), 2)
        self.assertEqual(hotel["total_rate"]["extracted_lowest"], 150)
        self.assertEqual(hotel["_source_url"], booking["_source_url"])
        self.assertEqual(hotel["_price_evidence"], booking["_price_evidence"])
        self.assertEqual(google["total_rate"]["extracted_lowest"], 200)

    def test_branches_missing_addresses_and_approximate_map_points_never_merge(self):
        for address in ("99 Main Street, Kingston, ON, Canada", None, "Kingston, Ontario, Canada"):
            with self.subTest(address=address):
                google = row()
                native = row("airbnb", address=address, gps_coordinates={"latitude": 44.2, "longitude": -76.4})
                google["gps_coordinates"] = native["gps_coordinates"]
                merged = merge_properties([native, google], TRIP)
                self.assertEqual(len(merged), 2)
        self.assertEqual(len(merge_properties([row(address=None), row("airbnb", address=None)], TRIP)), 2)

    def test_unverified_or_wrong_currency_offer_never_replaces_verified_quote(self):
        for changes in ({"scope_verified": False}, {"currency": "USD"}, {"basis": "night"}):
            native = row("booking", amount=10)
            native["_platform_offer"].update(changes)
            hotel = merge_properties([row(), native], TRIP)[0]
            self.assertEqual(hotel["total_rate"]["extracted_lowest"], 200)
            self.assertEqual(len(hotel["_platform_offers"]), 2)

    def test_native_airbnb_without_public_contacts_stays_out_of_both_calling_queues(self):
        provider = self.provider(["airbnb"])
        native = row("airbnb", address=None, phone=None, link="https://www.airbnb.ca/rooms/123", amount=80)
        with patch("hotel_search.airbnb_source.AirbnbSource.search", return_value={"properties": [native]}), \
             patch.object(provider, "_website", return_value=None):
            result = search_hotels(REQUEST, provider)
        self.assertEqual(len(result["hotels"]), 1)
        self.assertIsNone(result["hotels"][0]["phone"])
        self.assertFalse(result["hotels"][0]["call_ready"])
        self.assertEqual(result["shortlist"], [])
        self.assertEqual(result["enquiry_shortlist"], [])

    def test_contact_enrichment_cannot_erase_lower_native_quote(self):
        provider = self.provider(["google", "booking"])
        google, native = row(), row("booking", amount=150)
        details = row()
        del details["total_rate"]
        fake_google = MagicMock()
        fake_google.details.return_value = details
        fake_google.search.return_value = {"properties": [google]}
        with patch.object(provider, "_google", return_value=fake_google), \
             patch("hotel_search.ota_sources.OtaSource.search", return_value={"properties": [native]}):
            result = search_hotels(REQUEST, provider)
        hotel = result["hotels"][0]
        self.assertEqual(hotel["online_quote"]["amount"], 150)
        self.assertEqual(hotel["online_quote"]["source_url"], native["_source_url"])
        self.assertEqual(len(hotel["platform_offers"]), 2)

    def test_native_platforms_are_not_repeated_for_google_pagination_or_rental_pass(self):
        provider = self.provider(["airbnb"])
        with patch("hotel_search.airbnb_source.AirbnbSource.search") as native:
            self.assertEqual(provider.search(TRIP, next_page_token="next")["properties"], [])
            self.assertEqual(provider.search(TRIP, vacation_rentals=True)["properties"], [])
        native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
