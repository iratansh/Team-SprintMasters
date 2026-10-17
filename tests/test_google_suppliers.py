import json
import unittest
from urllib.parse import urlencode

from hotel_search.google_suppliers import extract_supplier_offers


TOKEN = "ChgIhYjY7K-Z1vvkARoLL2cvMXRmeV9ya20QAQ"
REQUEST = {"check_in": "2026-11-06", "check_out": "2026-11-08", "guests": 1, "currency": "CAD"}


def supplier(label="Booking.com", total="$175", nightly="$88", destination=None):
    row = [None] * 19
    url = "/travel/lodging/clk?" + urlencode({"pcurl": destination}) if destination else "/aclk?adurl="
    row[0] = [label, 184, url]
    price = [None] * 14
    price[4] = ["$75", nightly, 74.6, 87.7, 75, 88]
    price[5] = ["$149", total, 149.2, 175.4, 149, 175]
    price[12] = [[1, None, 0], None, None, [None, ""]]
    row[12] = price
    return row


def property_record(offers=None, group=21):
    context = [None] * 14
    context[3] = "CAD"
    context[4] = [[2026, 11, 6], [2026, 11, 8], 2, 1]
    context[13] = [1, None, 0]
    quote = [None] * 22
    quote[8] = [[2026, 11, 6], [2026, 11, 8], 2, 1]
    quote[group] = [supplier()] if offers is None else offers
    raw = [None] * 27
    raw[1], raw[2], raw[6], raw[20] = "A Hotel", [], [None, context, quote], TOKEN
    return raw


def page(rows):
    return "AF_initDataCallback({key: 'ds:2', hash: '7', data:" + json.dumps(rows) + ", sideChannel: {}});"


def extract(rows, **kwargs):
    return extract_supplier_offers(page(rows), REQUEST, property_token=TOKEN,
                                   source_url="https://www.google.com/travel/hotels/entity/" + TOKEN,
                                   observed_at="2026-10-17T18:00:00Z", **kwargs)


class SupplierOfferTests(unittest.TestCase):
    def test_keeps_explicit_stay_total_and_google_provenance(self):
        row = extract([property_record()])[0]
        self.assertEqual(row["amount"], 175)
        self.assertNotEqual(row["amount"], 2 * 88)
        self.assertEqual(row["basis"], "stay")
        self.assertEqual(row["discovery_method"], "google_hotels_supplier")
        self.assertEqual(row["tax_fee_status"], "unknown")
        self.assertEqual(row["provider"], "Booking.com")
        self.assertTrue(row["scope_verified"])
        self.assertEqual(row["raw"]["supplier_id"], 184)

    def test_platform_aliases_and_seller_prices_stay_together(self):
        labels = ["Booking.com", "Priceline", "Airbnb", "Expedia.ca", "Hotels.com", "Agoda"]
        offers = [supplier(label, total=f"${100+i}") for i, label in enumerate(labels)]
        result = extract([property_record(offers)])
        self.assertEqual([r["platform"] for r in result], ["booking", "priceline", "airbnb", "expedia", "hotels", "agoda"])
        self.assertEqual([r["amount"] for r in result], list(range(100, 106)))

    def test_each_known_group_is_supported(self):
        for index in (2, 12, 21):
            with self.subTest(index=index):
                self.assertEqual(extract([property_record(group=index)])[0]["amount"], 175)

    def test_mismatched_property_scope_suppresses_price(self):
        for dimension in ("currency", "adults", "checkin", "quote_checkout"):
            raw = property_record()
            if dimension == "currency": raw[6][1][3] = "USD"
            if dimension == "adults": raw[6][1][13][0] = 2
            if dimension == "checkin": raw[6][1][4][0][2] = 7
            if dimension == "quote_checkout": raw[6][2][8][1][2] = 9
            with self.subTest(dimension=dimension):
                result = extract([raw])[0]
                self.assertIsNone(result["amount"])
                self.assertFalse(result["scope_verified"])

    def test_wrong_or_missing_offer_occupancy_suppresses_price(self):
        for occupancy in (None, [], [2, None, 0], [1, None, 1], [True, None, 0], "1"):
            offer = supplier()
            offer[12][12][0] = occupancy
            row = extract([property_record([offer])])[0]
            self.assertFalse(row["scope_verified"])
            self.assertIsNone(row["amount"])

    def test_nightly_only_quote_never_becomes_a_stay_total(self):
        row = extract([property_record([supplier(total=None)])])[0]
        self.assertEqual(row["amount"], 88)
        self.assertEqual(row["basis"], "night")

    def test_bad_monetary_fields_never_use_prose_or_numeric_ids(self):
        for value in ("save $175", "US$175", "175 CAD", 175, 0, "$0", "$-175", "$nan", "$Infinity"):
            row = extract([property_record([supplier(total=value, nightly=None)])])[0]
            self.assertIsNone(row["amount"])
            self.assertEqual(row["basis"], "unknown")

    def test_other_property_and_review_supplier_labels_are_ignored(self):
        raw = property_record()
        raw[20] = "ChgIOTHERpropertyToken123456"
        self.assertEqual(extract([raw, ["Priceline", "$1", "https://example.com"]]), [])

    def test_supplier_url_and_listing_identity_are_not_fabricated(self):
        destination = "https://www.booking.com/searchresults.en.html?" + urlencode({
            "checkin": "2026-11-06", "checkout": "2026-11-08", "group_adults": 1,
            "selected_currency": "CAD", "highlighted_hotels": "123456"})
        row = extract([property_record([supplier(destination=destination)])])[0]
        self.assertEqual(row["listing_url"], destination)
        self.assertIsNone(row["listing_id"])
        self.assertTrue(row["scope_verified"])
        self.assertTrue(row["booking_url"].startswith("https://www.google.com/travel/lodging/clk?"))

    def test_explicit_destination_mismatches_override_matching_property_context(self):
        for key, value in (("checkin", "20261107"), ("checkout", "20261109"),
                           ("adults", "2"), ("cur", "USD"), ("group_children", "1")):
            url = "https://www.priceline.com/r/?" + urlencode({key: value})
            row = extract([property_record([supplier("Priceline", destination=url)])])[0]
            with self.subTest(key=key):
                self.assertFalse(row["scope_verified"])
                self.assertTrue(row["raw"]["supplier_destination_mismatch"])
                self.assertIsNone(row["amount"])

    def test_compact_dates_in_priceline_destination_are_accepted(self):
        url = "https://www.priceline.com/r/?checkin=20261106&checkout=20261108&adults=1&cur=CAD"
        row = extract([property_record([supplier("Priceline", destination=url)])])[0]
        self.assertTrue(row["scope_verified"])

    def test_agoda_calendar_components_are_checked(self):
        url = "https://www.agoda.com/partners/partnersearch.aspx?CkInYear=2026&CkInMonth=11&CkInDay=07"
        row = extract([property_record([supplier("Agoda", destination=url)])])[0]
        self.assertFalse(row["scope_verified"])

    def test_unknown_offer_shape_returns_no_claim(self):
        for offer in (None, [], ["Booking.com", "$1"], {"name": "Priceline", "price": 1}):
            self.assertEqual(extract([property_record([offer])]), [])

    def test_organic_offer_is_preferred_over_duplicate_ad(self):
        raw = property_record([supplier(destination="https://www.booking.com/hotel/ca/test.html")])
        raw[6][2][2] = [supplier()]
        raw[6][2][12] = [supplier()]
        rows = extract([raw])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw"]["record_path"], [6, 2, 21])

    def test_unsupported_request_and_invalid_html_fail_closed(self):
        for request in (None, {}, {**REQUEST, "currency": "USD"}):
            rows = extract_supplier_offers(page([property_record()]), request,
                        property_token=TOKEN, source_url="https://example.com")
            self.assertEqual(rows, [])
        self.assertEqual(extract_supplier_offers("<html>Priceline $1</html>", REQUEST,
                        property_token=TOKEN, source_url="https://example.com"), [])


if __name__ == "__main__":
    unittest.main()
