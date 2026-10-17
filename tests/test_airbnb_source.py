import base64
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from hotel_search.airbnb_source import AirbnbSource
from hotel_search.providers import ProviderError


class AirbnbSourceTests(unittest.TestCase):
    def setUp(self):
        self.request = {"city": None, "check_in": "2026-11-06", "check_out": "2026-11-08", "guests": 1, "currency": "CAD"}
        self.context = {"staysSearchRequest": {"rawParams": [
            {"filterName": k, "filterValues": [v]} for k, v in {
                "checkin": "2026-11-06", "checkout": "2026-11-08", "adults": "1",
                "currency": "CAD", "query": "Ontario, Canada"}.items()]}}
        self.row = {"__typename": "StaySearchResult", "subtitle": "Woodland cabin",
                    "title": "Cabin in Reaboro", "demandStayListing": {
                        "id": base64.b64encode(b"DemandStayListing:123456789").decode(),
                        "location": {"coordinate": {"latitude": 44.25, "longitude": -78.67}}},
                    "structuredDisplayPrice": {"primaryLine": {"price": "$970\u00a0CAD", "qualifier": "total"}}}

    def response(self, rows=None, context=None):
        payload = {"niobeClientData": [["StaysSearch:" + json.dumps(self.context if context is None else context),
            {"data": {"presentation": {"staysSearch": {"results": {"searchResults": [self.row] if rows is None else rows}}}}}]]}
        return {"text": '<script id="data-deferred-state-0" type="application/json">' + json.dumps(payload) + '</script>',
                "url": "https://www.airbnb.ca/s/Ontario--Canada/homes", "fetched_at": "2026-10-17T16:00:00Z"}

    def search(self, response=None, request=None):
        client = Mock()
        client.get.return_value = self.response() if response is None else response
        return AirbnbSource(client=client).search(self.request if request is None else request), client

    def test_public_hydrated_prices_and_identity(self):
        result, client = self.search()
        row = result["properties"][0]
        self.assertEqual(row["property_token"], "airbnb:123456789")
        self.assertEqual(row["total_rate"]["extracted_lowest"], 970)
        self.assertEqual(row["_platform_offer"]["basis"], "stay")
        self.assertTrue(row["_platform_offer"]["scope_verified"])
        self.assertIsNone(row["address"])
        self.assertIsNone(row["phone"])
        self.assertFalse(row["_location_verified"])
        self.assertEqual(result["search_parameters"]["adults"], 1)
        self.assertEqual(parse_qs(urlsplit(client.get.call_args.args[0]).query)["checkout"], ["2026-11-08"])

    def test_date_guest_currency_or_location_mismatch_never_produces_usable_price(self):
        for field in ("checkin", "checkout", "adults", "currency", "query"):
            context = deepcopy(self.context)
            for item in context["staysSearchRequest"]["rawParams"]:
                if item["filterName"] == field:
                    item["filterValues"] = ["wrong"]
            result, _ = self.search(self.response(context=context))
            row = result["properties"][0]
            self.assertNotIn("search_parameters", result)
            self.assertNotIn("total_rate", row)
            self.assertIsNone(row["_platform_offer"]["amount"])
            self.assertIn("_indicative_price_evidence", row)

    def test_discount_uses_current_dedicated_amount(self):
        self.row["structuredDisplayPrice"]["primaryLine"] = {
            "originalPrice": "$900 CAD", "discountedPrice": "$700 CAD", "qualifier": "total",
            "accessibilityLabel": "$700 CAD total, originally $900 CAD"}
        result, _ = self.search()
        self.assertEqual(result["properties"][0]["total_rate"]["extracted_lowest"], 700)

    def test_nightly_unknown_currency_and_ambiguous_text_are_not_totals(self):
        for primary in ({"price": "$50 CAD", "qualifier": "night"},
                        {"price": "$50", "qualifier": "total"},
                        {"price": "$50 USD", "qualifier": "total"},
                        {"price": "$50 CAD originally $80 CAD", "qualifier": "total"}):
            self.row["structuredDisplayPrice"]["primaryLine"] = primary
            result, _ = self.search()
            self.assertNotIn("total_rate", result["properties"][0])

    def test_listing_date_override_and_split_stay_invalidate_price(self):
        for override in ({"checkin": "2026-12-01"}, {"adults": 2}, {"splitStaysUuid": "split"}):
            self.row["listingParamOverrides"] = override
            result, _ = self.search()
            self.assertNotIn("total_rate", result["properties"][0])

    def test_unsupported_and_blocked_are_explicit_failures(self):
        for html, code in (("<html>Shell only</html>", "unsupported_layout"),
                           ("<html>Verify you are human</html>", "website_blocked")):
            response = self.response()
            response["text"] = html
            with self.assertRaises(ProviderError) as caught:
                self.search(response)
            self.assertEqual(caught.exception.code, code)

    def test_login_redirect_and_foreign_host_rejected(self):
        for url in ("https://www.airbnb.ca/login", "https://other.example/search"):
            response = self.response()
            response["url"] = url
            with self.assertRaises(ProviderError) as caught:
                self.search(response)
            self.assertEqual(caught.exception.code, "website_blocked")

    def test_empty_hydrated_results_valid_and_duplicates_removed(self):
        result, _ = self.search(self.response(rows=[]))
        self.assertEqual(result["properties"], [])
        result, _ = self.search(self.response(rows=[self.row, self.row]))
        self.assertEqual(len(result["properties"]), 1)

    def test_invalid_identifiers_do_not_create_fabricated_listing(self):
        self.row["demandStayListing"]["id"] = base64.b64encode(b"Other:123").decode()
        with self.assertRaises(ProviderError) as caught:
            self.search()
        self.assertEqual(caught.exception.code, "unsupported_layout")

    def test_city_input_and_adults_are_flexible(self):
        request = {**self.request, "city": "Kingston", "guests": 3}
        for item in self.context["staysSearchRequest"]["rawParams"]:
            if item["filterName"] == "adults":
                item["filterValues"] = ["3"]
            if item["filterName"] == "query":
                item["filterValues"] = ["Kingston, Ontario, Canada"]
        result, client = self.search(request=request)
        self.assertTrue(result["properties"][0]["_platform_offer"]["scope_verified"])
        self.assertIn("Kingston--Ontario--Canada", client.get.call_args.args[0])

    def test_invalid_inputs_fail_before_fetch(self):
        for request in ({**self.request, "guests": True}, {**self.request, "currency": "USD"},
                        {**self.request, "check_out": "2026-01-01"}, {**self.request, "city": "\n"}):
            client = Mock()
            with self.assertRaises(ProviderError):
                AirbnbSource(client=client).search(request)
            client.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
