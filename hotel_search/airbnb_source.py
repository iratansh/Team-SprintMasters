"""Bounded discovery from Airbnb's public search HTML, with no API key.

Only JSON already embedded in a public page is read; no authenticated or
private API is called. Prices are accepted only from an explicit total field
in results whose hydrated request echoes dates, occupancy and currency.
Host phones and exact addresses normally require a confirmed reservation:
https://www.airbnb.com/help/article/147
https://www.airbnb.com/help/article/4116
They remain unknown here. Approximate map coordinates are not an address.
"""

from __future__ import annotations

import base64
from datetime import date
from html.parser import HTMLParser
import json
import math
import re
from urllib.parse import quote, urlencode, urlsplit

from .providers import ProviderError
from .web_http import PublicWebClient


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active = False
        self.parts = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.active = (attrs.get("type") == "application/json"
                           and attrs.get("id", "").startswith("data-deferred-state-"))
            self.parts = []

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.active:
            self.scripts.append("".join(self.parts))
            self.active = False


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _scope(cache_key, request):
    """Match the cache-key attached to these results, not arbitrary page prose."""
    if not isinstance(cache_key, str) or not cache_key.startswith("StaysSearch:"):
        return False
    try:
        context = json.loads(cache_key.split(":", 1)[1])
        params = context["staysSearchRequest"]["rawParams"]
        values = {}
        for item in params:
            key = item["filterName"]
            if key in values:
                return False
            values[key] = item["filterValues"]
        wanted = {"checkin": request["check_in"], "checkout": request["check_out"],
                  "adults": str(request["guests"]), "currency": request["currency"],
                  "query": _location(request)}
        return (all(values.get(k) == [v] for k, v in wanted.items())
                and values.get("children", ["0"]) == ["0"]
                and values.get("infants", ["0"]) == ["0"])
    except (ValueError, KeyError, TypeError):
        return False


def _location(request):
    return f'{request["city"]}, Ontario, Canada' if request.get("city") else "Ontario, Canada"


def _identifier(row):
    listing = row.get("demandStayListing")
    if not isinstance(listing, dict):
        return None
    identifier = listing.get("id")
    if not isinstance(identifier, str) or len(identifier) > 200:
        return None
    try:
        decoded = base64.b64decode(identifier, validate=True).decode("ascii")
    except (ValueError, UnicodeError):
        return None
    match = re.fullmatch(r"DemandStayListing:([1-9][0-9]{0,24})", decoded)
    return match[1] if match else None


def _money(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\$\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\.([0-9]{1,2}))?\s*CAD", value.strip())
    if not match:
        return None
    amount = float(match[1].replace(",", "") + "." + (match[2] or "0"))
    return amount if 0 < amount < 10000000 else None


def _record(row, request, page, scope_verified):
    if not isinstance(row, dict) or row.get("__typename") != "StaySearchResult":
        return None
    identifier = _identifier(row)
    name = _text((row.get("nameLocalized") or {}).get("localizedStringWithTranslationPreference"))
    name = name or _text(row.get("subtitle")) or _text(row.get("title"))
    if not identifier or not name:
        return None
    overrides = row.get("listingParamOverrides") or {}
    for key, expected in (("checkin", request["check_in"]), ("checkout", request["check_out"]),
                          ("adults", request["guests"]), ("children", 0), ("infants", 0)):
        if overrides.get(key) is not None and str(overrides[key]) != str(expected):
            scope_verified = False
    if any(overrides.get(key) for key in ("splitStaysUuid", "splitStaysLocationsAcpIds", "splitStaysLocationPrecisions")):
        scope_verified = False
    price = row.get("structuredDisplayPrice") or {}
    primary = price.get("primaryLine") or {}
    displayed = primary.get("discountedPrice") or primary.get("price")
    # Do not parse the accessibility label: it can contain a crossed-out amount.
    amount = _money(displayed) if scope_verified and primary.get("qualifier") == "total" else None
    params = {"check_in": request["check_in"], "check_out": request["check_out"],
              "adults": request["guests"], "currency": request["currency"]}
    link = f"https://www.airbnb.ca/rooms/{identifier}?" + urlencode(params)
    observed = page.get("observed_at") or page.get("fetched_at")
    result = {"name": name, "property_token": "airbnb:" + identifier,
              "address": None, "phone": None, "link": link, "type": "vacation rental",
              "currency": request["currency"], "discovery_source": "airbnb",
              "_source_url": page["url"], "_fetched_at": page.get("fetched_at"),
              "_provider_observed_at": observed, "_platform": "airbnb", "_listing_id": identifier,
              "_location_verified": False,
              "_extraction_warnings": ["airbnb_host_phone_and_exact_address_not_public", "airbnb_map_coordinates_are_approximate"],
              "_platform_offer": {"platform": "airbnb", "listing_id": identifier,
                  "listing_url": link, "source_url": page["url"], "amount": amount,
                  "currency": "CAD" if _money(displayed) is not None else None,
                  "basis": "stay" if amount is not None else "unknown",
                  "check_in": request["check_in"] if scope_verified else None,
                  "check_out": request["check_out"] if scope_verified else None,
                  "guests": request["guests"] if scope_verified else None,
                  "scope_verified": scope_verified, "tax_fee_status": "unknown",
                  "observed_at": observed, "retrieved_at": page.get("fetched_at"),
                  "availability": "online_listing_only"}}
    location = row["demandStayListing"].get("location") or {}
    point = location.get("coordinate") or {}
    lat, lon = point.get("latitude"), point.get("longitude")
    if (type(lat) in (float, int) and type(lon) in (float, int)
            and math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        result["gps_coordinates"] = {"latitude": lat, "longitude": lon}
    evidence = {"source": "Airbnb public search hydration", "displayed_price": displayed,
                "qualifier": primary.get("qualifier"), "accessibility_label": primary.get("accessibilityLabel"),
                "scope_verified": scope_verified, "basis": "stay" if amount is not None else "unknown",
                "tax_fee_status": "unknown"}
    if amount is not None:
        result["total_rate"] = {"extracted_lowest": amount}
        result["_price_evidence"] = evidence
    else:
        result["_indicative_price_evidence"] = evidence
    return result


class AirbnbSource:
    estimated_cost = 0
    pagination_supported = False

    def __init__(self, client=None, timeout=20, retries=0, fresh=False,
                 cache_dir="outputs/web-cache", max_properties=60):
        if type(max_properties) is not int or not 1 <= max_properties <= 500:
            raise ProviderError("Airbnb result limit must be from 1 to 500.", "configuration_error")
        self.client = client or PublicWebClient(timeout=timeout, retries=retries, fresh=fresh, cache_dir=cache_dir)
        self.max_properties = max_properties

    def search(self, request):
        try:
            start, end = date.fromisoformat(request["check_in"]), date.fromisoformat(request["check_out"])
            city = request.get("city")
            valid = (end > start and type(request["guests"]) is int and 1 <= request["guests"] <= 20
                     and request.get("currency", "CAD") == "CAD"
                     and request.get("province", "Ontario") == "Ontario"
                     and request.get("country", "Canada") == "Canada"
                     and (city is None or isinstance(city, str) and city.strip() and len(city) <= 120
                          and all(ord(c) >= 32 for c in city)))
            if not valid:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ProviderError("Airbnb needs valid stay dates, 1–20 adults and Ontario/CAD scope.", "invalid_input") from None
        request = {**request, "currency": "CAD", "city": city.strip() if city else None}
        slug = _location(request).replace(", ", "--").replace(" ", "-")
        url = "https://www.airbnb.ca/s/" + quote(slug, safe="-") + "/homes?" + urlencode({
            "checkin": request["check_in"], "checkout": request["check_out"],
            "adults": request["guests"], "currency": "CAD"})
        page = self.client.get(url)
        final = urlsplit(page["url"])
        if final.hostname not in {"www.airbnb.ca", "www.airbnb.com", "airbnb.ca", "airbnb.com"}:
            raise ProviderError("Airbnb redirected outside the public search site.", "website_blocked")
        if re.search(r"/(?:login|signup|authenticate|captcha|challenge|consent)(?:/|$)", final.path):
            raise ProviderError("Airbnb requires login, consent or human verification.", "website_blocked")
        parser = _Scripts()
        try:
            parser.feed(page["text"])
        except (ValueError, RecursionError):
            raise ProviderError("Airbnb returned unsupported public HTML.", "unsupported_layout") from None
        results_found, rows_present, verified, records, seen = False, False, False, [], set()
        for script in parser.scripts:
            try:
                hydration = json.loads(script)
                entries = hydration.get("niobeClientData", [])
            except (ValueError, TypeError, RecursionError, AttributeError):
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, list) or len(entry) != 2:
                    continue
                cache_key, payload = entry
                try:
                    rows = payload["data"]["presentation"]["staysSearch"]["results"]["searchResults"]
                except (KeyError, TypeError):
                    continue
                if not isinstance(rows, list):
                    continue
                results_found = True
                rows_present = rows_present or bool(rows)
                scoped = _scope(cache_key, request)
                verified = verified or scoped
                for row in rows[:self.max_properties]:
                    try:
                        record = _record(row, request, page, scoped)
                    except (TypeError, AttributeError, KeyError):
                        continue
                    if record and record["property_token"] not in seen and len(records) < self.max_properties:
                        records.append(record)
                        seen.add(record["property_token"])
        if not results_found:
            lowered = page["text"].lower()
            if any(value in lowered for value in ("verify you are human", "access denied", "captcha", "before you continue")):
                raise ProviderError("Airbnb blocked public search access or requires consent.", "website_blocked")
            raise ProviderError("Airbnb public search hydration is unavailable or changed.", "unsupported_layout")
        if rows_present and not records:
            raise ProviderError("Airbnb returned listings in an unsupported layout.", "unsupported_layout")
        result = {"properties": records, "_source_url": page["url"],
                  "_coverage_warning": "Airbnb first public result window only; not exhaustive or globally cheapest. Host phones and exact addresses are generally unavailable before booking. Approximate locations need independent verification."}
        if verified:
            result["search_parameters"] = {"check_in_date": request["check_in"], "check_out_date": request["check_out"],
                                           "adults": request["guests"], "children": 0, "currency": "CAD"}
        return result
