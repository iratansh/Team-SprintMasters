"""Explicit, network-free fixture adapter for integration development."""

from copy import deepcopy
from datetime import datetime, timezone

from .models import SearchRequest
from .providers import ProviderError


class FixtureProvider:
    request_count = 0
    estimated_cost = 0

    def __init__(self, fixture, request):
        if not isinstance(fixture, dict) or fixture.get("synthetic_fixture") is not True:
            raise ValueError("fixture must explicitly declare synthetic_fixture: true")
        if SearchRequest.from_dict(fixture.get("request")).to_dict() != request.to_dict():
            raise ValueError("fixture stay does not match the requested city, dates or guests")
        if not isinstance(fixture.get("search_pages"), list) or not isinstance(fixture.get("details"), dict):
            raise ValueError("fixture requires search_pages and details")
        self.fixture = fixture

    def _annotate(self, result):
        result = deepcopy(result)
        result["_fetched_at"] = datetime.now(timezone.utc).isoformat()
        result["_provider_observed_at"] = result.get("search_metadata", {}).get("created_at")
        result["_source_url"] = "https://example.com/synthetic-fixture"
        return result

    def search(self, request, *, next_page_token=None, vacation_rentals=False):
        if vacation_rentals:
            return self._annotate({"properties": []})
        for page in self.fixture["search_pages"]:
            if page.get("input_page_token") == next_page_token:
                return self._annotate(page["response"])
        raise ProviderError("No fixture page matches the requested token.", "no_results")

    def details(self, request, property_token, *, vacation_rentals=False):
        if property_token not in self.fixture["details"]:
            raise ProviderError("No details supplied for this fixture property.", "no_results")
        return self._annotate(self.fixture["details"][property_token])
