"""Data-integrity regressions from the independent search review."""

from copy import deepcopy
import unittest

from hotel_search.models import SearchRequest
from hotel_search.normalize import normalize_property
from hotel_search.providers import ProviderError
from hotel_search.workflow import search_hotels


SOURCE = "https://www.google.com/travel/hotels?q=Kingston"
OBSERVED = "2026-10-17T14:00:00Z"
FETCHED = "2026-10-17T14:00:01Z"


def valid_property():
    return {
        "name": "Synthetic Review Inn",
        "property_token": "review-valid",
        "address": "12 Main Street, Kingston, ON, Canada",
        "phone": "+16135550123",
        "gps_coordinates": {"latitude": 44.23, "longitude": -76.48},
        "total_rate": {"extracted_lowest": 200},
    }


class OfflineProvider:
    """Deliberately minimal fake; no network or real call integration."""

    request_count = 0
    estimated_cost = 0

    def __init__(self, rows, details):
        self.rows = rows
        self.detail_rows = details

    def search(self, request, *, next_page_token=None, vacation_rentals=False):
        return {"properties": deepcopy(self.rows), "_source_url": SOURCE,
                "_provider_observed_at": OBSERVED, "_fetched_at": FETCHED}

    def details(self, request, property_token, *, vacation_rentals=False):
        if property_token not in self.detail_rows:
            raise ProviderError("No offline details supplied.", "no_results")
        return deepcopy(self.detail_rows[property_token])


class ReviewNormalizationRegressions(unittest.TestCase):
    def setUp(self):
        self.request = SearchRequest("Kingston", "2026-11-06", "2026-11-08", guests=1)
        self.listing = valid_property()

    def normalize(self, details=None, listing=None):
        return normalize_property(
            self.listing if listing is None else listing, details, self.request,
            source_url=SOURCE, observed_at=OBSERVED, fetched_at=FETCHED,
        )

    def test_documented_string_guest_count_does_not_hide_public_offer(self):
        details = deepcopy(self.listing)
        details["prices"] = [{
            "source": "Synthetic Public Provider",
            "link": "https://public.example/selected-stay",
            "num_guests": "1",
            "total_rate": {"extracted_lowest": 180},
        }]
        hotel = self.normalize(details)
        self.assertTrue(hotel["call_ready"])
        self.assertEqual(hotel["online_quote"]["amount"], 180)
        self.assertEqual(hotel["online_quote"]["provider"], "Synthetic Public Provider")
        self.assertTrue(hotel["online_quote"]["occupancy_matches"])

    def test_same_name_tokenless_details_with_different_address_are_rejected(self):
        details = deepcopy(self.listing)
        details.pop("property_token")
        details.pop("gps_coordinates")
        details.update(address="99 Other Street, Kingston, ON, Canada",
                       phone="+16135550999", total_rate={"extracted_lowest": 1})
        hotel = self.normalize(details)
        self.assertFalse(hotel["call_ready"])
        self.assertEqual(hotel["phone"], "+16135550123")
        self.assertEqual(hotel["online_quote"]["amount"], 200)
        self.assertIn("property_identity_unverified", hotel["not_call_ready_reasons"])

    def test_same_name_tokenless_details_with_distant_gps_are_rejected(self):
        details = deepcopy(self.listing)
        details.pop("property_token")
        details.update(gps_coordinates={"latitude": 44.40, "longitude": -76.70},
                       phone="+16135550999", total_rate={"extracted_lowest": 1})
        hotel = self.normalize(details)
        self.assertFalse(hotel["call_ready"])
        self.assertEqual(hotel["phone"], "+16135550123")
        self.assertIn("property_identity_unverified", hotel["not_call_ready_reasons"])

    def test_contact_only_details_preserve_listing_price_and_its_provenance(self):
        details = {key: deepcopy(value) for key, value in self.listing.items()
                   if key != "total_rate"}
        details.update(
            _source_url="https://www.google.com/travel/hotels/entity/review-valid",
            _provider_observed_at="2026-10-17T14:05:00Z",
            _fetched_at="2026-10-17T14:05:01Z",
        )
        hotel = self.normalize(details)
        self.assertTrue(hotel["call_ready"])
        self.assertEqual(hotel["online_quote"]["amount"], 200)
        self.assertEqual(hotel["online_quote"]["source_url"], SOURCE)
        self.assertEqual(hotel["online_quote"]["observed_at"], OBSERVED)
        self.assertEqual(hotel["online_quote"]["retrieved_at"], FETCHED)


class ReviewWorkflowRegressions(unittest.TestCase):
    def setUp(self):
        self.request = SearchRequest("Kingston", "2026-11-06", "2026-11-08")

    def test_malformed_row_token_or_address_does_not_discard_valid_candidate(self):
        malformed = [
            {"name": "Bad Token", "property_token": {"unexpected": "object"}},
            {"name": "Bad Address", "address": {"unexpected": "object"}},
        ]
        for bad_row in malformed:
            with self.subTest(row=bad_row):
                good = valid_property()
                provider = OfflineProvider([bad_row, good], {"review-valid": good})
                result = search_hotels(self.request, provider)
                good_hotels = [h for h in result["hotels"]
                               if h["property_token"] == "review-valid"]
                self.assertEqual(len(good_hotels), 1)
                self.assertTrue(good_hotels[0]["call_ready"])
                self.assertIn(good_hotels[0]["candidate_id"], result["shortlist"])
                self.assertTrue(result["errors"])
                self.assertFalse(any(h["call_ready"] for h in result["hotels"]
                                     if h["property_name"] == bad_row["name"]))


if __name__ == "__main__":
    unittest.main()
