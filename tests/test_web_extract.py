import json
import unittest

from hotel_search.web_extract import extract_property


REQUEST = {"check_in": "2026-11-06", "check_out": "2026-11-08", "guests": 2, "currency": "CAD"}
URL = "https://example.test/stay?arrival=2026-11-06"


def page(value):
    return '<script type="application/ld+json">' + json.dumps(value) + '</script>'


def hotel(**extra):
    return {"@type": "Hotel", "name": "Example Hotel", "telephone": "+1 613 555 0100",
            "address": {"@type": "PostalAddress", "streetAddress": "10 Main Street",
                        "addressLocality": "Kingston", "addressRegion": "ON",
                        "postalCode": "K7L 1A1", "addressCountry": "CA"}, **extra}


def scoped_offer(**extra):
    return {"@type": "Offer", "checkIn": "2026-11-06", "checkOut": "2026-11-08",
            "numAdults": 2, "totalPrice": "240.50", "priceCurrency": "CAD", **extra}


class WebExtractTests(unittest.TestCase):
    def extract(self, value):
        return extract_property(page(value), URL, REQUEST)

    def test_static_contact_page_excludes_unrelated_footer(self):
        html = '''<title>Example Motel | Kingston</title><main><h1>Example Motel</h1>
            <address>10 Main Street<br>Kingston, ON K7L 1A1, Canada</address>
            <a href="tel:+16135550100">Call us</a></main>
            <footer><address>Another Hotel, Ottawa</address>
            <a href="tel:+16135550999">Partner hotel</a></footer>'''
        result = extract_property(html, URL, REQUEST)
        self.assertEqual(result["name"], "Example Motel")
        self.assertEqual(result["phone"], "+16135550100")
        self.assertIn("Kingston, ON", result["address"])
        self.assertEqual(result["availability_status"], "unknown")
        self.assertNotIn("total_rate", result)
        self.assertNotIn("rate_per_night", result)

    def test_structured_property_and_schema_type_url(self):
        result = self.extract(hotel(**{"@type": ["LocalBusiness", "https://schema.org/Hotel"]}))
        self.assertEqual(result["type"], "Hotel")
        self.assertEqual(result["address"], "10 Main Street, Kingston, ON K7L 1A1, Canada")
        self.assertEqual(result["phone_link"], "tel:+1 613 555 0100")
        self.assertEqual(result["_source_url"], URL)
        self.assertTrue(result["_fetched_at"].endswith("+00:00"))

    def test_graph_and_main_entity_id_select_one_property(self):
        value = {"@graph": [
            {"@type": "WebPage", "mainEntity": {"@id": "#hotel"}},
            hotel(**{"@id": "#hotel"}), hotel(name="Other Hotel", **{"@id": "#other"})]}
        self.assertEqual(self.extract(value)["name"], "Example Hotel")

    def test_nested_main_entity(self):
        value = {"@type": "WebPage", "mainEntity": hotel()}
        self.assertEqual(self.extract(value)["name"], "Example Hotel")

    def test_multiple_properties_rejected_even_if_title_names_one(self):
        html = "<title>Example Hotel</title>" + page({"@graph": [hotel(), hotel(name="Other Hotel")]})
        self.assertIsNone(extract_property(html, URL, REQUEST))

    def test_unknown_fields_are_not_invented(self):
        result = self.extract({"@type": "Hostel", "name": "Minimal Hostel"})
        self.assertIsNone(result["address"])
        self.assertIsNone(result["phone"])
        self.assertNotIn("currency", result)

    def test_nonhotel_contact_page_is_not_property(self):
        self.assertIsNone(extract_property("<title>Tourism Ontario</title><address>Ontario</address><a href='tel:+16135550100'>Call</a>", URL, REQUEST))

    def test_results_page_is_not_property(self):
        self.assertIsNone(extract_property("<title>Best hotels in Kingston</title><address>ON</address>", URL, REQUEST))

    def test_ambiguous_static_contacts_rejected(self):
        self.assertIsNone(extract_property("<h1>Example Hotel</h1><a href='tel:+16135550100'>One</a><a href='tel:+16135550200'>Two</a>", URL, REQUEST))

    def test_generic_price_range_offer_and_aggregate_stay_indicative(self):
        result = self.extract(hotel(priceRange="$70–$150", offers=[
            {"@type": "Offer", "price": "70", "priceCurrency": "CAD"},
            {"@type": "AggregateOffer", "lowPrice": "65", "priceCurrency": "CAD"},
        ]))
        self.assertNotIn("total_rate", result)
        self.assertNotIn("rate_per_night", result)
        self.assertEqual(len(result["_indicative_price_evidence"]), 3)

    def test_scoped_explicit_full_stay_offer(self):
        result = self.extract(hotel(offers=scoped_offer()))
        self.assertEqual(result["total_rate"]["extracted_lowest"], 240.50)
        self.assertEqual(result["_price_scope"], {**REQUEST})
        self.assertEqual(result["availability_status"], "unknown")

    def test_missing_or_mismatched_scope_never_populates_ranked_price(self):
        variants = [{"checkIn": "2026-11-05"}, {"checkOut": "2026-11-09"},
                    {"numAdults": 1}, {"numAdults": None}, {"numAdults": True},
                    {"numChildren": 1}, {"priceCurrency": None}, {"checkIn": None}]
        for changes in variants:
            with self.subTest(changes=changes):
                result = self.extract(hotel(offers=scoped_offer(**changes)))
                self.assertNotIn("total_rate", result)

    def test_validity_dates_are_not_stay_dates(self):
        result = self.extract(hotel(offers={"@type": "Offer", "totalPrice": "200",
            "validFrom": REQUEST["check_in"], "validThrough": REQUEST["check_out"],
            "priceCurrency": "CAD", "numAdults": 2}))
        self.assertNotIn("total_rate", result)
        self.assertIn("_indicative_price_evidence", result)

    def test_room_capacity_does_not_establish_booking_occupancy(self):
        offer = scoped_offer()
        del offer["numAdults"]
        offer["occupancy"] = {"@type": "QuantitativeValue", "maxValue": 2}
        self.assertNotIn("total_rate", self.extract(hotel(offers=offer)))

    def test_explicit_nightly_quote_is_not_full_stay_total(self):
        offer = scoped_offer(totalPrice=None, priceSpecification={"price": "110",
            "priceCurrency": "CAD", "unitText": "per night"})
        result = self.extract(hotel(offers=offer))
        self.assertEqual(result["rate_per_night"]["extracted_lowest"], 110)
        self.assertNotIn("total_rate", result)

    def test_vat_inclusion_does_not_imply_fee_inclusion(self):
        result = self.extract(hotel(offers=scoped_offer(valueAddedTaxIncluded=True, unitText="per bed")))
        self.assertTrue(result["_tax_included"])
        self.assertEqual(result["_tax_fee_status"], "unknown")
        self.assertEqual(result["_price_unit"], "bed")

    def test_unavailable_offer_is_not_ranked(self):
        result = self.extract(hotel(offers=scoped_offer(availability="https://schema.org/SoldOut")))
        self.assertNotIn("total_rate", result)
        self.assertIn("_indicative_price_evidence", result)

    def test_scoped_room_offer_preserves_room_and_bed_type(self):
        result = self.extract(hotel(offers=scoped_offer(itemOffered={
            "@type": "HotelRoom", "name": "Twin room",
            "bed": {"@type": "BedDetails", "typeOfBed": "Single", "numberOfBeds": 2}})))
        self.assertEqual(result["room_type"], "Twin room")
        self.assertEqual(result["bed_type"]["numberOfBeds"], 2)
        self.assertEqual(result["_price_unit"], "room")

    def test_mixed_nightly_and_total_offers_are_not_compared(self):
        result = self.extract(hotel(offers=[scoped_offer(), scoped_offer(totalPrice=None,
            priceSpecification={"price": 100, "unitText": "night"})]))
        self.assertNotIn("total_rate", result)
        self.assertNotIn("rate_per_night", result)
        self.assertIn("ambiguous_offer_price_basis_or_currency", result["_extraction_warnings"])

    def test_malformed_graph_identifier_is_not_a_crash(self):
        result = self.extract({"@type": "WebPage", "mainEntity": hotel(**{"@id": ["bad"]})})
        self.assertEqual(result["name"], "Example Hotel")

    def test_unsafe_property_url_uses_evidence_source(self):
        self.assertEqual(self.extract(hotel(url="javascript:alert(1)"))["link"], URL)

    def test_bad_numeric_values_never_quote(self):
        for amount in [True, 0, -10, "NaN", "Infinity", "$200", "from 100", "0.001"]:
            with self.subTest(amount=amount):
                self.assertNotIn("total_rate", self.extract(hotel(offers=scoped_offer(totalPrice=amount))))

    def test_malformed_json_can_fall_back_to_static_page(self):
        result = extract_property('<script type="application/ld+json">{bad}</script><h1>Example Inn</h1>', URL, REQUEST)
        self.assertEqual(result["name"], "Example Inn")

    def test_url_token_is_stable_across_query_variants(self):
        first = self.extract(hotel())
        second = extract_property(page(hotel()), "https://example.test/stay?other=1", REQUEST)
        self.assertEqual(first["property_token"], second["property_token"])

    def test_unsafe_source_url_rejected(self):
        self.assertIsNone(extract_property(page(hotel()), "file:///private.html", REQUEST))


if __name__ == "__main__":
    unittest.main()
