import json
import unittest
from urllib.parse import parse_qs, urlsplit

from hotel_search.google_hotels import GoogleHotelsSource, stay_token
from hotel_search.models import SearchRequest
from hotel_search.providers import ProviderError


TOKEN = "ChgIhYjY7K-Z1vvkARoLL2cvMXRmeV9ya20QAQ"


def record():
    metadata = [None] * 38
    metadata[0] = [44.2419462, -76.6080398]
    metadata[1] = [[["4024 Bath Rd, Kingston, ON K7M 4Y4"]]]
    metadata[2] = ["(613) 634-7400", "tel:+16136347400"]
    metadata[29] = [None, None, "https://example.com/hotel"]
    context = [None] * 14
    context[3] = "CAD"
    context[4] = [[2026, 11, 6], [2026, 11, 8], 2, 1]
    context[13] = [3, None, 0]
    quote = [None] * 10
    quote[1] = ["$75", "$88", 74.6]
    quote[8] = [[2026, 11, 6], [2026, 11, 8], 2, 1]
    # Stay total deliberately differs from rounded nightly * nights.
    quote[9] = ["$149", "$175"]
    raw = [None] * 27
    raw[1], raw[2], raw[3] = "Bayside Inn & Waterfront Suites", metadata, ["3-star hotel", 3]
    raw[6] = [None, context, quote]
    raw[20] = TOKEN
    return raw


def html(rows, adults=3):
    return ('<html><div data-adults="' + str(adults) + '"></div><script>' +
            "AF_initDataCallback({key: 'ds:0', hash: '1', data:" + json.dumps(rows) +
            ', sideChannel: {}});</script></html>')


class Client:
    request_count = 0
    cache_hits = 0

    def __init__(self, content):
        self.content = content
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        self.request_count += 1
        return {"text": self.content, "url": url, "fetched_at": "2026-10-17T12:00:00Z",
                "observed_at": "2026-10-17T11:59:00Z"}


class GoogleHotelsTests(unittest.TestCase):
    def setUp(self):
        self.request = SearchRequest("Kingston", "2026-11-06", "2026-11-08", 3).to_dict()

    def source(self, rows=None, adults=3):
        client = Client(html([record()] if rows is None else rows, adults))
        return GoogleHotelsSource(client=client), client

    def test_live_style_embedded_record_keeps_explicit_stay_total(self):
        source, client = self.source()
        result = source.search(self.request)
        row = result["properties"][0]
        self.assertEqual(row["total_rate"], {"extracted_lowest": 175, "extracted_before_taxes_fees": 149})
        self.assertEqual(row["rate_per_night"]["extracted_lowest"], 88)
        self.assertNotEqual(row["total_rate"]["extracted_lowest"], 2 * 88)
        self.assertEqual(row["_price_evidence"]["verified_adults"], 3)
        self.assertEqual(row["_provider_observed_at"], "2026-10-17T11:59:00Z")
        query = parse_qs(urlsplit(client.urls[0]).query)
        self.assertEqual(query["curr"], ["CAD"])
        self.assertIn("ts", query)
        self.assertNotIn("checkin", query)
        self.assertNotIn("api_key", query)

    def test_default_scope_url_is_ontario(self):
        source, client = self.source()
        request = {**self.request, "city": None}
        source.search(request)
        query = parse_qs(urlsplit(client.urls[0]).query)
        self.assertEqual(query["q"], ["hotels in Ontario, Canada"])

    def test_short_google_entity_tokens_are_valid_properties(self):
        raw = record()
        raw[20] = "CgoI4NzJmsPmkpU6EAE"
        source, _ = self.source([raw])
        rows = source.search(self.request)["properties"]
        self.assertEqual(rows[0]["property_token"], raw[20])

    def test_each_context_dimension_must_match(self):
        for dimension in ("currency", "adult", "year", "checkout", "quote_dates"):
            raw = record()
            if dimension == "currency": raw[6][1][3] = "USD"
            if dimension == "adult": raw[6][1][13][0] = 2
            if dimension == "year": raw[6][1][4][0][0] = 2027
            if dimension == "checkout": raw[6][1][4][1][2] = 9
            if dimension == "quote_dates": raw[6][2][8][0][2] = 7
            source, _ = self.source([raw])
            with self.subTest(dimension=dimension), self.assertRaises(ProviderError) as caught:
                source.search(self.request)
            self.assertEqual(caught.exception.code, "query_mismatch")

    def test_individual_mismatch_retains_contacts_without_false_price(self):
        valid = record()
        invalid = record()
        invalid[20] = TOKEN + "2"
        invalid[6][1][13][0] = 2
        source, _ = self.source([valid, invalid])
        rows = source.search(self.request)["properties"]
        self.assertIn("total_rate", rows[0])
        self.assertNotIn("total_rate", rows[1])
        self.assertEqual(rows[1]["phone_link"], "tel:+16136347400")

    def test_visible_adult_control_mismatch_fails_closed(self):
        source, _ = self.source(adults=2)
        with self.assertRaises(ProviderError) as caught:
            source.search(self.request)
        self.assertEqual(caught.exception.code, "query_mismatch")

    def test_missing_stay_total_never_gets_invented_from_nightly(self):
        raw = record()
        raw[6][2][9] = None
        source, _ = self.source([raw])
        row = source.search(self.request)["properties"][0]
        self.assertIsNone(row["total_rate"]["extracted_lowest"])
        self.assertEqual(row["rate_per_night"]["extracted_lowest"], 88)

    def test_nonmonetary_and_from_labels_are_not_prices(self):
        raw = record()
        raw[6][2][9] = ["from $149", "$175 per person"]
        source, _ = self.source([raw])
        row = source.search(self.request)["properties"][0]
        self.assertIsNone(row["total_rate"]["extracted_lowest"])
        self.assertIsNone(row["total_rate"]["extracted_before_taxes_fees"])

    def test_details_match_exact_entity_and_only_enrich_contacts(self):
        raw = record()
        raw[6][1][13][0] = 2  # Detail quote cannot overwrite search's 3-adult price.
        source, client = self.source([raw])
        row = source.details(self.request, TOKEN)
        self.assertEqual(row["address"], "4024 Bath Rd, Kingston, ON K7M 4Y4")
        self.assertEqual(row["phone_link"], "tel:+16136347400")
        self.assertEqual(row["property_token"], TOKEN)
        self.assertNotIn("total_rate", row)
        self.assertIn("/entity/" + TOKEN, client.urls[0])
        with self.assertRaises(ProviderError):
            source.details(self.request, TOKEN + "2")

    def test_access_checks_are_not_bypassed(self):
        for content in ("Our systems have detected unusual traffic", '<form class="g-recaptcha">'):
            source = GoogleHotelsSource(client=Client(content))
            with self.subTest(content=content), self.assertRaises(ProviderError) as caught:
                source.search(self.request)
            self.assertEqual(caught.exception.code, "source_blocked")

    def test_invalid_tokens_and_pagination_rejected_without_network(self):
        source, client = self.source()
        for token in ("../somewhere", "https://example.com/hotel", ""):
            with self.assertRaises(ProviderError):
                source.details(self.request, token)
        with self.assertRaises(ProviderError):
            source.search(self.request, next_page_token="page2")
        self.assertEqual(client.urls, [])

    def test_stay_url_changes_for_every_relevant_input(self):
        original = stay_token(self.request)
        for changes in ({"guests": 1}, {"guests": 6}, {"check_in": "2026-11-05"},
                        {"check_out": "2026-11-09"}):
            self.assertNotEqual(original, stay_token({**self.request, **changes}))
        for changes in ({"guests": 0}, {"guests": True}, {"currency": "USD"},
                        {"check_out": "2026-11-05"}):
            with self.assertRaises(ProviderError):
                stay_token({**self.request, **changes})

    def test_incomplete_or_executable_dataset_is_not_evaluated(self):
        for content in (html([]), "AF_initDataCallback({key: 'ds:0', hash: '1', data:alert(1), sideChannel: {}})"):
            source = GoogleHotelsSource(client=Client('<div data-adults="3"></div>' + content))
            with self.assertRaises(ProviderError) as caught:
                source.search(self.request)
            self.assertEqual(caught.exception.code, "no_results")


if __name__ == "__main__":
    unittest.main()
