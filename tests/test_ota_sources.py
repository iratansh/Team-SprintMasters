"""Contract tests with small public-markup examples, not fabricated live claims."""

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from hotel_search.ota_sources import OtaSource, PLATFORM_NAMES
from hotel_search.providers import ProviderError


REQUEST = {"province": "Ontario", "country": "Canada", "check_in": "2026-11-06",
           "check_out": "2026-11-08", "guests": 2, "currency": "CAD"}
SCOPED = "?checkin=2026-11-06&checkout=2026-11-08&group_adults=2&group_children=0"


class Client:
    def __init__(self, html, url=None, error=None):
        self.html, self.url, self.error, self.urls = html, url, error, []

    def get(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return {"text": self.html, "url": self.url or url,
                "fetched_at": "2026-10-17T15:00:00Z", "observed_at": None}


def structured(platform="booking", **extra):
    urls = {"booking": "https://www.booking.com/hotel/ca/example.html" + SCOPED,
            "priceline": "https://www.priceline.com/relax/at/12345/from/20261106/to/20261108",
            "expedia": "https://www.expedia.ca/Kingston-Hotels-Example.h12345.Hotel-Information",
            "hotels": "https://ca.hotels.com/ho12345/example-kingston-canada/",
            "agoda": "https://www.agoda.com/example/hotel/kingston-on-ca.html"}
    node = {"@type": "Hotel", "name": "Example Hotel", "url": urls[platform],
            "address": {"streetAddress": "10 Main St", "addressLocality": "Kingston", "addressRegion": "ON", "addressCountry": "CA"},
            "telephone": "+16135550100", **extra}
    return '<script type="application/ld+json">' + json.dumps({"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item": node}]}) + '</script>'


def offer(**extra):
    return {"@type": "Offer", "totalPrice": "240.50", "priceCurrency": "CAD",
            "checkIn": REQUEST["check_in"], "checkOut": REQUEST["check_out"], "numAdults": 2, **extra}


def card(href=None, label="CAD 240.50", summary="2 nights, 2 adults", extra=""):
    href = href or "/hotel/ca/example.html" + SCOPED
    return f'''<div data-testid="property-card"><a data-testid="title-link" href="{href}">
      <div data-testid="title">Example Hotel</div></a>
      <span data-testid="address">Kingston, Ontario, Canada</span>
      <span>{summary}</span><span data-testid="price-and-discounted-price">{label}</span>{extra}</div>'''


def inputs(**overrides):
    values = {"checkin": REQUEST["check_in"], "checkout": REQUEST["check_out"], "group_adults": "2", "group_children": "0", **overrides}
    return "".join(f'<input name="{key}" value="{value}">' for key, value in values.items())


class OtaSourceTests(unittest.TestCase):
    def extract(self, html, platform="booking", **kwargs):
        return OtaSource(platform, client=Client(html), **kwargs).search(REQUEST)

    def test_public_search_urls_include_flexible_dates_occupancy_and_ontario(self):
        for name in PLATFORM_NAMES:
            with self.subTest(platform=name):
                source = OtaSource(name, client=Client(""))
                url = source.search_url(REQUEST)
                self.assertIn("ontario", url.lower())
                if name == "priceline":
                    self.assertIn("/from/20261106/to/20261108/rooms/1/adults/2", url)
                else:
                    self.assertIn("2026-11-06", url)
                    self.assertIn("2026-11-08", url)
                self.assertIn("CAD", url)

    def test_city_filter_is_optional_and_propagates_to_all_searches(self):
        request = {**REQUEST, "city": "Kingston"}
        for name in PLATFORM_NAMES:
            self.assertIn("kingston", OtaSource(name).search_url(request).lower())

    def test_booking_search_query_occupancy_and_sort(self):
        values = parse_qs(urlsplit(OtaSource("booking").search_url({**REQUEST, "guests": 4})).query)
        self.assertEqual(values["group_adults"], ["4"])
        self.assertEqual(values["group_children"], ["0"])
        self.assertEqual(values["order"], ["price"])

    def test_supported_platforms_and_invalid_configuration(self):
        self.assertEqual(set(PLATFORM_NAMES), {"booking", "priceline", "expedia", "hotels", "agoda"})
        for kwargs in ({"platform": "unknown"}, {"platform": "booking", "max_results": 0}, {"platform": "booking", "max_results": True}):
            with self.assertRaises(ProviderError):
                OtaSource(**kwargs)

    def test_all_platforms_read_matching_full_stay_structured_offer(self):
        for name in PLATFORM_NAMES:
            with self.subTest(platform=name):
                result = self.extract(structured(name, offers=offer()), platform=name)
                row = result["properties"][0]
                self.assertEqual(row["total_rate"]["extracted_lowest"], 240.50)
                self.assertEqual(row["_platform_offer"]["amount"], 240.50)
                self.assertEqual(row["_platform_offer"]["basis"], "stay")
                self.assertTrue(row["_platform_offer"]["scope_verified"])
                self.assertEqual(row["_platform_offer"]["guests"], 2)
                self.assertTrue(row["property_token"].startswith(name + ":"))
                self.assertEqual(row["discovery_source"], name)
                self.assertEqual(row["_fetched_at"], "2026-10-17T15:00:00Z")
                self.assertEqual(row["phone"], "+16135550100")

    def test_structured_unknown_price_stays_unknown(self):
        row = self.extract(structured(offers={"@type": "Offer", "price": 90, "priceCurrency": "CAD"}))["properties"][0]
        self.assertNotIn("total_rate", row)
        self.assertIsNone(row["_platform_offer"]["amount"])
        self.assertFalse(row["_platform_offer"]["scope_verified"])
        self.assertIn("_indicative_price_evidence", row)

    def test_mismatched_scope_unavailable_and_nightly_offers_do_not_rank(self):
        for value in (offer(numAdults=1), offer(checkIn="2026-11-05"), offer(availability="https://schema.org/SoldOut"),
                      offer(totalPrice=None, priceSpecification={"unitText": "night", "price": 120, "priceCurrency": "CAD"})):
            with self.subTest(offer=value):
                row = self.extract(structured(offers=value))["properties"][0]
                self.assertNotIn("total_rate", row)
                self.assertNotIn("rate_per_night", row)
                self.assertIsNone(row["_platform_offer"]["amount"])

    def test_cards_require_returned_link_and_card_scope_echo(self):
        row = self.extract(card(extra="<span>includes taxes and charges</span>"))["properties"][0]
        self.assertEqual(row["total_rate"]["extracted_lowest"], 240.50)
        self.assertEqual(row["_tax_fee_status"], "included")
        self.assertIsNone(row["phone"])
        self.assertEqual(row["_listing_id"], "ca/example")

    def test_requested_url_dates_alone_do_not_make_card_price_scoped(self):
        row = self.extract(card(href="/hotel/ca/example.html", summary="Great value"))["properties"][0]
        self.assertNotIn("total_rate", row)
        self.assertFalse(row["_platform_offer"]["scope_verified"])

    def test_echoed_inputs_verify_card_scope_with_explicit_total(self):
        row = self.extract(inputs() + card(href="/hotel/ca/example.html", summary="Total for your stay"))["properties"][0]
        self.assertEqual(row["total_rate"]["extracted_lowest"], 240.50)

    def test_input_conflicts_and_card_occupancy_mismatch_stay_unknown(self):
        for html in (inputs(group_adults="1") + card(href="/hotel/ca/example.html"),
                     card(summary="2 nights, 1 adult"), card(summary="3 nights, 2 adults"),
                     card(summary="2 nights, 2 adults, 1 child"),
                     inputs() + card(summary="2 nights, 1 adult"),
                     inputs() + card(summary="2 nights, 2 adults, per night"),
                     inputs() + card(href="/hotel/ca/example.html?checkin=2026-11-05"),
                     card(label="$240.50"), card(label="US$240.50"), card(label="CAD 0")):
            with self.subTest(html=html):
                row = self.extract(html)["properties"][0]
                self.assertNotIn("total_rate", row)

    def test_duplicate_listing_has_one_row_and_prefers_scoped_offer(self):
        result = self.extract(structured(offers=offer()) + card(label="CAD 999.00") + card())
        self.assertEqual(len(result["properties"]), 1)
        self.assertEqual(result["properties"][0]["total_rate"]["extracted_lowest"], 240.50)

    def test_never_imports_other_platform_or_external_contacts_from_footer(self):
        html = card() + '<footer><a href="tel:+16135559999">Corporate support</a></footer>'
        self.assertIsNone(self.extract(html)["properties"][0]["phone"])
        with self.assertRaises(ProviderError):
            self.extract(structured("priceline"))

    def test_fake_similar_domain_and_offsite_redirect_are_rejected(self):
        for url in ("https://booking.com.evil.test/hotel/ca/fake.html", "https://example.test/results"):
            client = Client(structured(), url=url)
            with self.assertRaises(ProviderError) as caught:
                OtaSource("booking", client=client).search(REQUEST)
            self.assertEqual(caught.exception.code, "source_blocked")

    def test_html_challenges_and_http_blocks_report_source_blocked(self):
        for html in ("<div id='challenge-container'>Loading</div>", "<script>AwsWafIntegration.getToken()</script>",
                     "<h1>Verify that you are not a robot</h1>", "<div class='px-captcha'></div>"):
            with self.assertRaises(ProviderError) as caught:
                self.extract(html)
            self.assertEqual(caught.exception.code, "source_blocked")
        for code in ("website_blocked", "rate_limit"):
            with self.assertRaises(ProviderError) as caught:
                OtaSource("booking", client=Client("", error=ProviderError("HTTP", code))).search(REQUEST)
            self.assertEqual(caught.exception.code, "source_blocked")

    def test_client_rendered_shell_and_bare_no_results_are_not_empty_inventory(self):
        for html in ("<div id='root'></div><script src='app.js'></script>", "<p>No properties found</p>"):
            with self.assertRaises(ProviderError) as caught:
                self.extract(html)
            self.assertEqual(caught.exception.code, "layout_changed")

    def test_explicit_scoped_no_results_and_first_page_coverage(self):
        result = self.extract(inputs() + "<p>No properties found</p>")
        self.assertEqual(result["properties"], [])
        self.assertIn("first search page", result["_coverage_warning"])

    def test_structured_multiple_properties_and_bound(self):
        first = structured()
        second = structured().replace("example.html", "second.html").replace("Example Hotel", "Second Hotel")
        self.assertEqual(len(self.extract(first + second)["properties"]), 2)
        self.assertEqual(len(self.extract(first + second, max_results=1)["properties"]), 1)


if __name__ == "__main__":
    unittest.main()
