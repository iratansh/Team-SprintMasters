import copy
import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from hotel_search.osm_source import discover_properties
from hotel_search.providers import ProviderError


class OpenStreetMapSourceTests(unittest.TestCase):
    def setUp(self):
        self.element = {
            "type": "node", "id": 123, "lat": 44.23, "lon": -76.48,
            "tags": {"name": "Test Lodge", "tourism": "motel",
                     "addr:housenumber": "10", "addr:street": "Main Street",
                     "addr:city": "Kingston", "addr:province": "ON",
                     "phone": "+1 613 555 0123", "website": "https://example.test"},
        }

    @staticmethod
    def response(payload):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return response

    def discover(self, elements, request=None, **kwargs):
        with patch("hotel_search.osm_source.urlopen", return_value=self.response({"elements": elements})):
            return discover_properties(request or {}, **kwargs)

    def test_ontario_query_and_provenance_never_claim_prices_or_availability(self):
        with patch("hotel_search.osm_source.urlopen", return_value=self.response({"elements": [self.element]})) as fetch:
            result = discover_properties({"check_in": "2028-01-01", "guests": 4}, max_results=9)
        query = parse_qs(fetch.call_args.args[0].data.decode())["data"][0]
        self.assertIn('["ISO3166-2"="CA-ON"]', query)
        self.assertIn("out tags center 9", query)
        self.assertIn('^(phone|contact:phone)$', query)
        fetch.assert_called_once()
        item = result[0]
        self.assertEqual(item["property_token"], "osm:node:123")
        self.assertEqual(item["_source_url"], "https://www.openstreetmap.org/node/123")
        self.assertEqual(item["address"], "10 Main Street, Kingston, ON")
        self.assertTrue(item["_location_verified"])
        self.assertIsNone(item["_provider_observed_at"])
        self.assertNotIn("availability", item)
        self.assertNotIn("total_rate", item)

    def test_contact_aliases_way_centers_and_missing_address(self):
        element = copy.deepcopy(self.element)
        element.update(type="way", center={"lat": element.pop("lat"), "lon": element.pop("lon")})
        tags = element["tags"]
        tags["contact:phone"] = tags.pop("phone")
        tags["contact:website"] = tags.pop("website").replace("https://", "")
        for key in list(tags):
            if key.startswith("addr:"):
                del tags[key]
        result = self.discover([element])[0]
        self.assertEqual(result["link"], "https://example.test")
        self.assertEqual(result["gps_coordinates"], {"latitude": 44.23, "longitude": -76.48})
        self.assertIsNone(result["address"])
        self.assertIsNone(result["city"])

    def test_city_is_escaped_and_requires_real_tag_match(self):
        city = 'A.b";out;'
        with patch("hotel_search.osm_source.urlopen", return_value=self.response({"elements": [self.element]})) as fetch:
            self.assertEqual(discover_properties({"city": city}), [])
        query = parse_qs(fetch.call_args.args[0].data.decode())["data"][0]
        self.assertIn('A\\\\.b\\";out;', query)
        self.assertEqual(len(self.discover([self.element], {"city": "kingston"})), 1)
        del self.element["tags"]["addr:city"]
        self.assertEqual(self.discover([self.element], {"city": "Kingston"}), [])

    def test_rejects_wrong_province_missing_coordinates_and_invalid_data(self):
        for change in ({"lat": 20}, {"lat": float("nan")}, {"lon": True}, {"id": True}, {"type": "area"}):
            element = copy.deepcopy(self.element)
            element.update(change)
            self.assertEqual(self.discover([element]), [])
        for field, value in (("addr:province", "QC"), ("addr:country", "US"), ("name", ""), ("tourism", "museum")):
            element = copy.deepcopy(self.element)
            element["tags"][field] = value
            self.assertEqual(self.discover([element]), [])

    def test_limit_and_duplicate_ids_are_bounded(self):
        self.assertEqual(len(self.discover([self.element] * 3)), 1)
        self.assertEqual(len(self.discover([self.element] * 3, max_results=1)), 1)
        for kwargs in ({"max_results": 0}, {"max_results": True}, {"max_results": 501}, {"timeout": 0}, {"timeout": float("inf")}):
            with self.assertRaises(ProviderError):
                discover_properties({}, **kwargs)
        with patch("hotel_search.osm_source.urlopen") as fetch:
            for request in ({"city": "\n"}, {"country": "US"}, {"province": "Quebec"}):
                with self.assertRaises(ProviderError):
                    discover_properties(request)
            fetch.assert_not_called()

    def test_network_errors_do_not_retry_or_leak_response_details(self):
        failures = [(HTTPError("https://example.test", 429, "private", {}, io.BytesIO()), "rate_limit"),
                    (TimeoutError("private"), "timeout"), (URLError("private"), "network_error")]
        for failure, code in failures:
            with patch("hotel_search.osm_source.urlopen", side_effect=failure) as fetch:
                with self.assertRaises(ProviderError) as caught:
                    discover_properties({})
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("private", str(caught.exception))
                fetch.assert_called_once()

    def test_partial_server_results_and_malformed_responses_are_rejected(self):
        for payload in ({"elements": [self.element], "remark": "runtime error"}, {"elements": {}}, [], {}):
            with patch("hotel_search.osm_source.urlopen", return_value=self.response(payload)):
                with self.assertRaises(ProviderError):
                    discover_properties({})
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>blocked</html>"
        with patch("hotel_search.osm_source.urlopen", return_value=response):
            with self.assertRaises(ProviderError) as caught:
                discover_properties({})
            self.assertEqual(caught.exception.code, "invalid_response")


if __name__ == "__main__":
    unittest.main()
