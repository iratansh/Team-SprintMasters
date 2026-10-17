import copy
from decimal import Decimal
import unittest

from hotel_search.models import SearchRequest
from hotel_search.normalize import (
    clean_evidence, location_status, money, normalize_phone, normalize_property,
    rank_hotels, safe_url,
)


class SearchRequestTests(unittest.TestCase):
    def test_valid_leap_day_stay_and_trimmed_city(self):
        request = SearchRequest("  Kingston  ", "2028-02-28", "2028-03-01", guests=2)
        self.assertEqual(request.city, "Kingston")
        self.assertEqual(request.to_dict()["nights"], 2)

    def test_rejects_impossible_malformed_reversed_and_excessive_dates(self):
        for check_in, check_out in [
            ("2027-02-29", "2027-03-01"),
            ("2028-2-28", "2028-03-01"),
            ("2028-03-01", "2028-03-01"),
            ("2028-03-02", "2028-03-01"),
            ("2028-01-01", "2029-01-01"),
            (None, "2028-03-01"),
        ]:
            with self.subTest(check_in=check_in, check_out=check_out):
                with self.assertRaises(ValueError):
                    SearchRequest("Kingston", check_in, check_out)

    def test_rejects_noninteger_and_out_of_range_guests(self):
        for guests in [True, False, 0, 21, "2", 2.0, None]:
            with self.subTest(guests=guests), self.assertRaises(ValueError):
                SearchRequest("Kingston", "2028-01-01", "2028-01-02", guests)

    def test_rejects_missing_unknown_fields_and_invalid_scope(self):
        valid = dict(city="Kingston", check_in="2028-01-01", check_out="2028-01-02")
        invalid = [None, {}, {**valid, "nights": 1}, {**valid, "currency": "USD"},
                   {**valid, "province": "Quebec"}, {**valid, "city": "\nKingston"}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                SearchRequest.from_dict(value)


class MoneyAndEvidenceTests(unittest.TestCase):
    def test_money_uses_only_positive_finite_numeric_values(self):
        for value in [True, False, None, "100", "$100 from", -1, 0,
                      float("nan"), float("inf"), Decimal("NaN"), 10000000]:
            with self.subTest(value=value):
                self.assertIsNone(money(value))
        self.assertEqual(money(Decimal("125.555")), 125.56)

    def test_subcent_value_cannot_become_a_zero_price(self):
        self.assertIsNone(money(0.001))

    def test_evidence_recursively_removes_secrets_from_fields_and_urls(self):
        evidence = {"api_key": "secret", "nested": [{"authorization": "secret",
            "link": "https://example.com/rate?api_key=secret&room=2#section"}]}
        cleaned = clean_evidence(evidence)
        self.assertEqual(cleaned, {"nested": [{"link": "https://example.com/rate?room=2"}]})
        self.assertEqual(evidence["api_key"], "secret")
        for url in ["javascript:alert(1)", "https://user:secret@example.com", "//example.com"]:
            self.assertIsNone(safe_url(url))


class PhoneAndLocationTests(unittest.TestCase):
    def setUp(self):
        self.request = SearchRequest("Kingston", "2028-01-01", "2028-01-03")

    def test_phone_normalization_keeps_extensions_separate(self):
        for raw in ["(613) 555-0123 ext. 42", "tel:+1-613-555-0123;ext=42", "6135550123 x42"]:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_phone(None, raw), ("+16135550123", "42"))
        self.assertEqual(normalize_phone("tel:+16135550123", "wrong"), ("+16135550123", None))

    def test_rejects_invalid_and_non_north_american_numbers(self):
        for raw in ["+44 20 7946 0958", "123", "1-800-FLOWERS", "+1 013 555 0123", "6131550123"]:
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_phone(None, raw)[0])

    def test_city_must_be_a_locality_not_a_street_or_nearby_city(self):
        self.assertEqual(location_status("12 Main Street, Kingston, ON K7L 1A1, Canada", self.request), "matched")
        for address in ["12 Kingston Street, Gananoque, ON, Canada", "12 Main Street, Kingston Mills, ON, Canada"]:
            with self.subTest(address=address):
                self.assertEqual(location_status(address, self.request), "outside_requested_city")
        self.assertEqual(location_status(None, self.request), "unknown")
        self.assertEqual(location_status("12 Main Street, Kingston, NY, USA", self.request), "unknown")


class PropertyNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.request = SearchRequest("Kingston", "2028-01-01", "2028-01-03", guests=2)
        self.listing = {"name": "Test Inn", "property_token": "token-a",
                        "address": "12 Main Street, Kingston, ON, Canada",
                        "phone": "613-555-0123", "total_rate": {"extracted_lowest": 210}}

    def normalize(self, listing=None, details=None):
        return normalize_property(listing or self.listing, details, self.request,
            source_url="https://www.google.com/travel/hotels?q=Kingston",
            observed_at="2028-01-01T00:00:00Z", fetched_at="2028-01-01T00:00:01Z")

    def test_full_stay_price_is_unverified_with_unknown_taxes(self):
        hotel = self.normalize()
        self.assertEqual(hotel["online_quote"]["amount"], 210)
        self.assertEqual(hotel["online_quote"]["basis"], "stay")
        self.assertEqual(hotel["online_quote"]["tax_fee_status"], "unknown")
        self.assertEqual(hotel["availability_status"], "online_rate_found")
        self.assertTrue(hotel["phone_verification_required"])
        self.assertTrue(hotel["call_ready"])

    def test_nightly_only_rate_is_never_multiplied_into_total(self):
        listing = copy.deepcopy(self.listing)
        listing.pop("total_rate")
        listing["rate_per_night"] = {"extracted_lowest": 95}
        hotel = self.normalize(listing)
        self.assertIsNone(hotel["online_quote"]["amount"])
        self.assertEqual(hotel["online_quote"]["nightly_amount"], 95)
        self.assertEqual(hotel["online_quote"]["basis"], "unknown")
        self.assertIn("missing_full_stay_price", hotel["not_call_ready_reasons"])

    def test_pre_tax_total_stays_explicitly_excluded(self):
        listing = {**self.listing, "total_rate": {"extracted_before_taxes_fees": 180}}
        hotel = self.normalize(listing)
        self.assertEqual(hotel["online_quote"]["amount"], 180)
        self.assertEqual(hotel["online_quote"]["tax_fee_status"], "excluded")

    def test_offer_amount_provider_and_url_always_come_from_same_offer(self):
        listing = {**self.listing, "prices": [
            {"source": "Expensive", "total_rate": {"extracted_lowest": 230}, "link": "https://expensive.example/book"},
            {"source": "Cheap", "total_rate": {"extracted_lowest": 200}, "link": "https://cheap.example/book"},
        ]}
        quote = self.normalize(listing)["online_quote"]
        self.assertEqual((quote["amount"], quote["provider"], quote["booking_url"]),
                         (200, "Cheap", "https://cheap.example/book"))

    def test_restricted_rate_and_wrong_occupancy_cannot_be_call_ready(self):
        for changes in [{"discount_remarks": ["Members only"]}, {"num_guests": 1}]:
            listing = {**self.listing, "prices": [{"source": "Restricted",
                "total_rate": {"extracted_lowest": 120}, **changes}]}
            with self.subTest(changes=changes):
                hotel = self.normalize(listing)
                self.assertFalse(hotel["call_ready"])

    def test_public_offer_preferred_over_lower_member_only_rate(self):
        listing = {**self.listing, "prices": [
            {"source": "Members", "discount_remarks": ["Sign-in required"], "total_rate": {"extracted_lowest": 100}},
            {"source": "Public", "total_rate": {"extracted_lowest": 220}},
        ]}
        hotel = self.normalize(listing)
        self.assertEqual(hotel["online_quote"]["provider"], "Public")
        self.assertTrue(hotel["call_ready"])

    def test_room_capacity_can_exceed_requested_guest_count(self):
        listing = {**self.listing, "prices": [{"source": "Public", "num_guests": "3",
                    "total_rate": {"extracted_lowest": 220}}]}
        hotel = self.normalize(listing)
        self.assertTrue(hotel["call_ready"])
        self.assertEqual(hotel["online_quote"]["num_guests"], 3)
        self.assertTrue(hotel["online_quote"]["num_guests_is_capacity"])

    def test_nested_member_conditions_cannot_be_hidden_by_headline_price(self):
        listing = {**self.listing, "prices": [{"source": "Conditional",
            "total_rate": {"extracted_lowest": 100},
            "rooms": [{"name": "King Room", "rates": [{"discount_remarks": ["Member rate"]}]}]}]}
        hotel = self.normalize(listing)
        self.assertFalse(hotel["call_ready"])
        self.assertEqual(hotel["online_quote"]["rate_eligibility"], "restricted")

    def test_wrong_property_details_cannot_supply_contact_or_rate(self):
        wrong = {"name": "Other Inn", "property_token": "token-b",
                 "phone": "613-555-0999", "address": "99 Main, Kingston, ON, Canada",
                 "total_rate": {"extracted_lowest": 1}}
        hotel = self.normalize(details=wrong)
        self.assertEqual(hotel["property_name"], "Test Inn")
        self.assertEqual(hotel["phone"], "+16135550123")
        self.assertEqual(hotel["online_quote"]["amount"], 210)
        self.assertIn("property_identity_unverified", hotel["not_call_ready_reasons"])

    def test_foreign_currency_and_nearby_city_not_ready(self):
        for changes, reason in [({"currency": "USD"}, "currency_mismatch"),
                                ({"address": "12 Kingston Road, Gananoque, ON, Canada"}, "outside_requested_city")]:
            hotel = self.normalize({**self.listing, **changes})
            self.assertFalse(hotel["call_ready"])
            self.assertIn(reason, hotel["not_call_ready_reasons"])

    def test_ranking_keeps_tax_bases_separate_and_unknown_amount_last(self):
        known = self.normalize({**self.listing, "name": "Known", "total_rate": {"extracted_lowest": 210}})
        excluded = self.normalize({**self.listing, "name": "Pre-tax", "total_rate": {"extracted_before_taxes_fees": 150}})
        unknown = self.normalize({**self.listing, "name": "No price", "total_rate": {}})
        ranked = rank_hotels([unknown, excluded, known])
        self.assertEqual([h["property_name"] for h in ranked], ["Known", "Pre-tax", "No price"])
        self.assertEqual([h["rank"] for h in ranked], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
