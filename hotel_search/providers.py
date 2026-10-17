"""Small SerpApi adapter; requests and responses never appear in errors."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


class ProviderError(Exception):
    def __init__(self, message: str, code: str = "provider_error") -> None:
        super().__init__(message)
        self.code = code


class SerpApiProvider:
    """Fetch date-specific hotel results and details by exact property token."""

    _ENDPOINT = "https://serpapi.com/search.json"
    estimated_cost = None

    def __init__(
        self,
        api_key: str,
        timeout: float = 30.0,
        retries: int = 1,
        fresh: bool = False,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ProviderError("A SerpApi API key is required.", "configuration_error")
        if timeout <= 0 or retries < 0:
            raise ProviderError("Invalid provider timeout or retry count.", "configuration_error")
        self._api_key = api_key.strip()
        self.timeout = timeout
        self.retries = retries
        self.fresh = fresh
        self._request_count = 0
        self._count_lock = threading.Lock()

    @property
    def request_count(self) -> int:
        with self._count_lock:
            return self._request_count

    def search(
        self,
        request: dict,
        *,
        next_page_token: str | None = None,
        vacation_rentals: bool = False,
    ) -> dict:
        params = self._parameters(request, vacation_rentals)
        params["sort_by"] = "3"
        if next_page_token:
            params["next_page_token"] = next_page_token
        return self._fetch(params)

    def details(
        self,
        request: dict,
        property_token: str,
        *,
        vacation_rentals: bool = False,
    ) -> dict:
        if not isinstance(property_token, str) or not property_token.strip():
            raise ProviderError("A property token is required.", "invalid_input")
        params = self._parameters(request, vacation_rentals)
        params["property_token"] = property_token
        return self._fetch(params)

    def _parameters(self, request: dict, vacation_rentals: bool) -> dict:
        try:
            location = ", ".join(
                value.strip() for value in (request.get("city"), request["province"], request["country"])
                if isinstance(value, str) and value.strip()
            )
            params = {
                "engine": "google_hotels",
                "q": location,
                "check_in_date": request["check_in"],
                "check_out_date": request["check_out"],
                "adults": request["guests"],
                "children": 0,
                "currency": request["currency"],
                "gl": "ca",
                "hl": "en",
                "api_key": self._api_key,
            }
        except (KeyError, TypeError):
            raise ProviderError("Search request is missing required fields.", "invalid_input") from None
        if vacation_rentals:
            params["vacation_rentals"] = "true"
        if self.fresh:
            params["no_cache"] = "true"
        return params

    def _fetch(self, params: dict) -> dict:
        request = Request(
            self._ENDPOINT + "?" + urlencode(params),
            headers={"Accept": "application/json", "User-Agent": "SprintMasters-HotelSearch/1.0"},
        )
        for attempt in range(self.retries + 1):
            with self._count_lock:
                self._request_count += 1
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
            except HTTPError as exc:
                status = exc.code
                exc.close()
                if (status == 429 or 500 <= status <= 599) and attempt < self.retries:
                    self._backoff(attempt)
                    continue
                if status in (401, 403):
                    raise ProviderError("SerpApi authentication or access failed.", "authentication_error") from None
                code = "rate_limit" if status == 429 else "provider_error"
                raise ProviderError(f"SerpApi returned HTTP {status}.", code) from None
            except (TimeoutError, socket.timeout):
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                raise ProviderError("SerpApi request timed out.", "timeout") from None
            except URLError as exc:
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    if attempt < self.retries:
                        self._backoff(attempt)
                        continue
                    raise ProviderError("SerpApi request timed out.", "timeout") from None
                raise ProviderError("Could not connect to SerpApi.", "network_error") from None
            except OSError:
                raise ProviderError("SerpApi connection failed.", "network_error") from None

            try:
                result = json.loads(payload)
            except (ValueError, UnicodeError):
                raise ProviderError("SerpApi returned invalid JSON.", "invalid_response") from None
            if not isinstance(result, dict):
                raise ProviderError("SerpApi returned a non-object response.", "invalid_response")
            if result.get("error"):
                error = str(result["error"]).casefold()
                no_results = any(
                    marker in error
                    for marker in (
                        "no results", "hasn't returned any results", "has not returned any results",
                        "did not return any results", "returned no results", "no hotels found",
                    )
                )
                if no_results:
                    raise ProviderError("SerpApi found no results for this request.", "no_results")
                raise ProviderError("SerpApi reported a search error.")
            metadata = result.get("search_metadata")
            if not isinstance(metadata, dict) or metadata.get("status") != "Success":
                raise ProviderError("SerpApi did not return a successful search.", "invalid_response")
            result["_fetched_at"] = datetime.now(timezone.utc).isoformat()
            result["_provider_observed_at"] = metadata.get("created_at")
            result["_source_url"] = self._source_url(metadata, params)
            return result
        raise ProviderError("SerpApi request failed.")

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(0.25 * (2 ** min(attempt, 3)), 1.0))

    def _source_url(self, metadata: dict, params: dict) -> str:
        candidate = metadata.get("google_hotels_url")
        if isinstance(candidate, str) and self._api_key not in candidate:
            try:
                parsed = urlsplit(candidate)
                if (
                    parsed.scheme == "https"
                    and parsed.netloc in ("www.google.com", "google.com", "www.google.ca", "google.ca")
                    and parsed.path.startswith("/travel/hotels")
                    and not any(word in parsed.query.casefold() for word in ("api_key", "access_token", "secret"))
                ):
                    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
            except ValueError:
                pass
        # A readable discovery source, not a booking URL or a confirmed quote.
        query = (
            f"{params['q']} accommodation {params['check_in_date']} to "
            f"{params['check_out_date']}, {params['adults']} adults, {params['currency']}"
        )
        return "https://www.google.com/travel/hotels?" + urlencode({"q": query})
