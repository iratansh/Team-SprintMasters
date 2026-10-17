"""Ontario-wide search must retain multiple municipalities without leaking provinces."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from hotel_search.cli import main
from hotel_search.fixture import FixtureProvider
from hotel_search.models import SearchRequest
from hotel_search.normalize import location_status
from hotel_search.providers import SerpApiProvider
from hotel_search.workflow import search_hotels


class ProvinceScopeTests(unittest.TestCase):
    def setUp(self):
        self.request = SearchRequest.from_dict({"check_in": "2026-11-06", "check_out": "2026-11-08"})

    def test_missing_city_defaults_to_entire_province(self):
        self.assertIsNone(self.request.city)
        self.assertEqual(self.request.scope, "province")
        self.assertEqual(self.request.search_location, "Ontario, Canada")
        self.assertEqual(self.request.to_dict()["scope"], "province")
        self.assertEqual(SearchRequest(city=None, check_in="2026-11-06", check_out="2026-11-08"), self.request)

    def test_provider_sends_province_query_for_search_and_details(self):
        provider = SerpApiProvider("not-a-real-key")
        with patch.object(provider, "_fetch", return_value={}) as fetch:
            provider.search(self.request.to_dict())
            self.assertEqual(fetch.call_args.args[0]["q"], "Ontario, Canada")
            provider.details(self.request.to_dict(), "exact-token")
            self.assertEqual(fetch.call_args.args[0]["q"], "Ontario, Canada")

    def test_every_ontario_locality_is_eligible_including_toronto(self):
        for address in ["1 Main, Kingston, ON K7L 0A1, Canada",
                        "2 Main, Ottawa, Ontario K1A 0B1, Canada",
                        "3 Main, Thunder Bay, ON, Canada",
                        "4 Main, Toronto, ON M5V 0A1, Canada",
                        "5 Main Ottawa ON K1A 0B1, Canada"]:
            with self.subTest(address=address):
                self.assertEqual(location_status(address, self.request), "matched")

    def test_outside_province_and_ambiguous_addresses_never_match(self):
        for address in ["1 Ontario Street, Montreal, QC H1A 0A1, Canada",
                        "2 Main, Winnipeg, MB, Canada", "3 Main, Ontario, CA 91761, USA",
                        "4 Main, Ontario, OR", "5 Main, Toronto, Canada", None]:
            with self.subTest(address=address):
                self.assertNotEqual(location_status(address, self.request), "matched")

    def test_city_filter_still_restricts_locality(self):
        city = SearchRequest("Kingston", "2026-11-06", "2026-11-08")
        self.assertEqual(city.scope, "city")
        self.assertEqual(location_status("1 Main, Ottawa, ON, Canada", city), "outside_requested_city")

    def test_one_file_retains_and_ranks_multiple_cities(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "examples/search.fixture.json").read_text())
        fixture = deepcopy(fixture)
        fixture["details"]["demo_harbour"]["address"] = "1 Main, Montreal, QC, Canada"
        provider = FixtureProvider(fixture, self.request)
        result = search_hotels(self.request, provider)
        self.assertEqual(len(result["hotels"]), 3)
        self.assertEqual(result["coverage"]["geographic_scope"], "province")
        self.assertEqual(result["coverage"]["search_location"], "Ontario, Canada")
        by_id = {hotel["candidate_id"]: hotel for hotel in result["hotels"]}
        self.assertEqual([by_id[key]["property_name"] for key in result["shortlist"]], ["Example Maple Motel"])
        self.assertFalse(result["coverage"]["all_market_coverage"])

    def test_cli_omitted_city_and_explicit_scope_both_accept_province_fixture(self):
        for flags in [[], ["--province-wide"], ["--province-wide", "--check-in", "2026-11-06", "--check-out", "2026-11-08"]]:
            output = io.StringIO()
            with self.subTest(flags=flags), redirect_stdout(output), redirect_stderr(io.StringIO()), patch("hotel_search.providers.urlopen", side_effect=AssertionError("unexpected network")):
                self.assertEqual(main(["--demo", *flags, "--output", "-"]), 0)
                self.assertIsNone(json.loads(output.getvalue())["request"]["city"])


if __name__ == "__main__":
    unittest.main()
