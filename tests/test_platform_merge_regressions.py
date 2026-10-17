"""Cross-platform identity and quote-provenance regression cases."""

import unittest
from unittest.mock import patch

from hotel_search.direct import DirectWebProvider
from hotel_search.models import SearchRequest
from hotel_search.normalize import normalize_property
from hotel_search.platforms import merge_properties, search_platforms


REQUEST = SearchRequest(None, "2026-11-06", "2026-11-08", 1)


def property_row(platform="google", amount=200):
    url = ("https://www.google.com/travel/search" if platform == "google" else
           "https://www.booking.com/hotel/ca/example.html")
    row = {"name": "Example Inn", "address": "123 King St, Kingston, ON K7L 1A1",
           "phone": "+16135550100", "property_token": "google-token" if platform == "google" else "booking:example",
           "currency": "CAD", "_platform": platform, "_source_url": url,
           "link": "https://example.com" if platform == "google" else url,
           "total_rate": {"extracted_lowest": amount},
           "discovery_source": "google_hotels_direct" if platform == "google" else platform}
    if platform == "booking":
        row["_platform_offer"] = {"platform": "booking", "provider": "Booking.com",
            "listing_url": url, "source_url": url, "amount": amount, "currency": "CAD",
            "basis": "stay", "tax_fee_status": "unknown", "scope_verified": True,
            "check_in": REQUEST.check_in, "check_out": REQUEST.check_out, "guests": REQUEST.guests,
            "discovery_method": "direct_platform"}
    return row


def normalize(row):
    return normalize_property(row, None, REQUEST, source_url=row["_source_url"],
                              observed_at="2026-10-17T18:00:00Z", fetched_at="2026-10-17T18:00:00Z")


class PlatformMergeRegressionTests(unittest.TestCase):
    def test_native_quote_keeps_supplier_identity_and_url(self):
        row = merge_properties([property_row("booking", 150)], REQUEST.to_dict())[0]
        quote = normalize(row)["online_quote"]
        self.assertEqual(quote["amount"], 150)
        self.assertEqual(quote["provider"], "Booking.com")
        self.assertEqual(quote["booking_url"], "https://www.booking.com/hotel/ca/example.html")

    def test_merged_native_lower_quote_keeps_native_quote_provenance(self):
        rows = merge_properties([property_row(), property_row("booking", 150)], REQUEST.to_dict())
        self.assertEqual(len(rows), 1)
        # Keep Google token for exact-entity contact enrichment.
        self.assertEqual(rows[0]["property_token"], "google-token")
        quote = normalize(rows[0])["online_quote"]
        self.assertEqual(quote["amount"], 150)
        self.assertEqual(quote["provider"], "Booking.com")
        self.assertEqual(quote["source_url"], "https://www.booking.com/hotel/ca/example.html")
        self.assertEqual(quote["booking_url"], "https://www.booking.com/hotel/ca/example.html")

    def test_explicit_wrong_stay_cannot_replace_google_quote(self):
        native = property_row("booking", 150)
        native["_platform_offer"]["check_out"] = "2026-11-09"
        rows = merge_properties([property_row(), native], REQUEST.to_dict())
        self.assertEqual(rows[0]["total_rate"]["extracted_lowest"], 200)
        self.assertEqual(normalize(rows[0])["online_quote"]["source_url"], "https://www.google.com/travel/search")

    def test_google_fallback_preserves_osm_identity_and_discovery(self):
        provider = DirectWebProvider(platforms=["google"])
        row = property_row()
        row.update({"property_token": "osm:node:123", "discovery_source": "openstreetmap"})
        row.pop("_platform")
        row.pop("total_rate")
        provider._origins[row["property_token"]] = "osm"
        with patch.object(provider, "_search_single_source", return_value={"properties": [row]}):
            result = search_platforms(provider, REQUEST.to_dict())
        self.assertEqual(provider._origins[row["property_token"]], "osm")
        self.assertNotEqual(result["properties"][0]["_platform"], "google")
        self.assertNotIn("google_hotels_aggregate", [offer["discovery_method"]
                        for offer in result["properties"][0]["_platform_offers"]])


if __name__ == "__main__":
    unittest.main()
