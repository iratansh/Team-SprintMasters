"""Our own direct hotel discovery and page enrichment, without SerpApi."""

from copy import deepcopy
import threading

from .normalize import normalized, normalize_phone, safe_url
from .osm_source import discover_properties
from .providers import ProviderError
from .web_extract import extract_property
from .web_http import PublicWebClient


class DirectWebProvider:
    name = "direct_web"
    estimated_cost = 0

    def __init__(self, *, timeout=20, retries=1, fresh=False, cache_dir="outputs/web-cache",
                 source="auto", seed_urls=None, max_properties=60, client=None, platforms=None):
        if source not in {"auto", "google", "osm", "websites"}:
            raise ProviderError("Unknown direct discovery source.", "configuration_error")
        self.client = client or PublicWebClient(timeout=timeout, retries=retries, cache_dir=cache_dir, fresh=fresh)
        self.source = source
        if platforms is not None:
            from .platforms import platform_names
            self.platforms = platform_names(platforms)
        else:
            self.platforms = None
        self.seed_urls = [safe_url(url) for url in (seed_urls or [])]
        if any(url is None for url in self.seed_urls):
            raise ProviderError("Seed websites must be public HTTP(S) URLs.", "invalid_input")
        if source == "websites" and not self.seed_urls:
            raise ProviderError("Website discovery needs at least one --website-url.", "configuration_error")
        if not 1 <= max_properties <= 500:
            raise ProviderError("Direct result limit must be from 1 to 500.", "invalid_input")
        self.timeout = timeout
        self.max_properties = max_properties
        self.diagnostics = []
        self.warnings = [
            "Direct web parsing depends on public website layouts; blocked pages or layout changes are reported, not bypassed.",
            "OpenStreetMap and static website listings do not establish availability or date-specific prices.",
        ]
        self.coverage_summary = {"search_source": source, "sources_used": [], "paid_search_requests": 0}
        self._records = {}
        self._origins = {}
        self._extra_requests = 0
        self._lock = threading.Lock()
        self._google_source = None

    def _supplier_offers(self, row, request, property_token):
        """Read the already-cached exact Google detail page for seller offers."""
        source_url = safe_url(row.get("_source_url"))
        if not source_url or "google." not in source_url or "/travel/hotels/entity/" not in source_url:
            return []
        from .google_suppliers import extract_supplier_offers
        try:
            page = self.client.get(source_url)
            return extract_supplier_offers(page["text"], request, property_token=property_token,
                                           source_url=page["url"], observed_at=page.get("fetched_at"))
        except ProviderError as exc:
            self._record_error("google_supplier_offers", exc)
            return []

    @property
    def request_count(self):
        return self.client.request_count + self._extra_requests

    @property
    def cache_hits(self):
        return self.client.cache_hits

    def _record_error(self, source, exc):
        with self._lock:
            self.diagnostics.append({"step": "discovery", "source": source,
                                     "code": exc.code, "message": str(exc)})

    def _google(self):
        if self._google_source is None:
            from .google_hotels import GoogleHotelsSource
            self._google_source = GoogleHotelsSource(client=self.client)
        return self._google_source

    def _website(self, url, request):
        response = self.client.get(url)
        result = extract_property(response["text"], response["url"], request)
        if result:
            result["_fetched_at"] = response["fetched_at"]
            result["_from_cache"] = response["from_cache"]
        return result

    def search(self, request, *, next_page_token=None, vacation_rentals=False):
        if self.platforms and self.source == "auto":
            from .platforms import search_platforms
            return search_platforms(self, request, next_page_token=next_page_token, vacation_rentals=vacation_rentals)
        return self._search_single_source(request, next_page_token=next_page_token, vacation_rentals=vacation_rentals)

    def _search_single_source(self, request, *, next_page_token=None, vacation_rentals=False):
        properties = []
        result = None
        if self.source in {"auto", "google"}:
            try:
                result = self._google().search(request, next_page_token=next_page_token, vacation_rentals=vacation_rentals)
                properties = result.get("properties", [])
                self.coverage_summary["sources_used"].append("google_hotels_direct")
                for row in properties:
                    row.setdefault("discovery_source", "google_hotels_direct")
                    self._origins[row["property_token"]] = "google"
            except ProviderError as exc:
                if self.source == "google" or next_page_token:
                    raise
                self._record_error("google_hotels_direct", exc)
        if not properties and not next_page_token and not vacation_rentals and self.source in {"auto", "osm"}:
            try:
                self._extra_requests += 1
                properties = discover_properties(request, timeout=self.timeout, max_results=self.max_properties)
                self.coverage_summary["sources_used"].append("openstreetmap")
                for row in properties:
                    self._origins[row["property_token"]] = "osm"
            except ProviderError as exc:
                self._record_error("openstreetmap", exc)
                if self.source == "osm" and not self.seed_urls:
                    raise
        if not next_page_token and not vacation_rentals:
            for url in self.seed_urls:
                try:
                    row = self._website(url, request)
                    if row:
                        properties.append(row)
                        self._origins[row["property_token"]] = "website"
                        self.coverage_summary["sources_used"].append("official_website")
                    else:
                        self.warnings.append("A seed website did not identify one unambiguous accommodation property.")
                except ProviderError as exc:
                    self._record_error("official_website", exc)
        for row in properties:
            self._records[row["property_token"]] = deepcopy(row)
        if not properties and self.diagnostics and not result:
            raise ProviderError("Direct discovery sources could not provide hotel data; inspect source errors or try a supplied property website.", "discovery_unavailable")
        self.coverage_summary["sources_used"] = list(dict.fromkeys(self.coverage_summary["sources_used"]))
        return {**(result or {}), "properties": properties,
                "_source_url": (result or {}).get("_source_url")}

    def details(self, request, property_token, *, vacation_rentals=False):
        if property_token not in self._records:
            raise ProviderError("Unknown property identity for this search.", "invalid_property")
        row = deepcopy(self._records[property_token])
        if self._origins.get(property_token) == "google":
            try:
                detail = self._google().details(request, property_token, vacation_rentals=vacation_rentals)
                if detail:
                    # Keep the actual listing-price source; details can provide their own
                    # provenance separately, without moving a quote to an unrelated page.
                    row.update(detail)
                    suppliers = self._supplier_offers(detail, request, property_token)
                    if suppliers:
                        row["_platform_offers"] = list(row.get("_platform_offers", [])) + suppliers
                        row["_platforms"] = list(dict.fromkeys(list(row.get("_platforms", [])) + [offer["platform"] for offer in suppliers]))
                        with self._lock:
                            statuses = self.coverage_summary.setdefault("platform_status", [])
                            for offer in suppliers:
                                status = next((item for item in statuses if item["platform"] == offer["platform"]), None)
                                if status is None:
                                    status = {"platform": offer["platform"], "direct_search_status": "not_requested",
                                              "direct_candidates": 0, "supplier_offers": 0, "discovery_methods": []}
                                    statuses.append(status)
                                status["supplier_offers"] += 1
                                if "google_hotels_supplier" not in status["discovery_methods"]:
                                    status["discovery_methods"].append("google_hotels_supplier")
                    if not any(detail.get(key) for key in ("total_rate", "prices", "featured_prices")):
                        for key in ("total_rate", "rate_per_night", "prices", "featured_prices"):
                            row.pop(key, None)
            except ProviderError as exc:
                self._record_error("google_property_details", exc)
                row.setdefault("_extraction_warnings", []).append("google_property_details_unavailable")
        if self._origins.get(property_token) == "airbnb":
            # Public Airbnb search does not expose host phone/exact address.
            # Preserve the listing and quote without probing private contacts.
            return row
        url = safe_url(row.get("link"))
        if url and self._origins.get(property_token) != "website" and (not row.get("phone") or not row.get("address")):
            try:
                extra = self._website(url, request)
                if extra and self._same_property(row, extra):
                    for key in ("address", "phone", "phone_link"):
                        if not row.get(key) and extra.get(key):
                            row[key] = extra[key]
                    row["_contact_source_url"] = extra["_source_url"]
                    row["_indicative_price_evidence"] = extra.get("_indicative_price_evidence", [])
                    if not row.get("total_rate") and extra.get("total_rate"):
                        for key in ("total_rate", "rate_per_night", "_price_scope", "_price_unit", "_tax_fee_status", "_source_url", "_fetched_at"):
                            if key in extra:
                                row[key] = extra[key]
                elif extra:
                    row.setdefault("_extraction_warnings", []).append("website_property_identity_mismatch")
            except ProviderError as exc:
                self._record_error("official_website", exc)
                row.setdefault("_extraction_warnings", []).append("website_contact_enrichment_unavailable")
        return row

    @staticmethod
    def _same_property(left, right):
        if (left.get("address") and right.get("address") and
                normalized(left["address"]) != normalized(right["address"])):
            return False
        if normalized(left.get("name")) == normalized(right.get("name")):
            return True
        left_phone = normalize_phone(left.get("phone_link"), left.get("phone"))[0]
        right_phone = normalize_phone(right.get("phone_link"), right.get("phone"))[0]
        return bool(left_phone and left_phone == right_phone and left.get("address") and
                    normalized(left["address"]) == normalized(right.get("address")))
