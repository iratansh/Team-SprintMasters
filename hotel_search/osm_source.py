"""Key-free, bounded accommodation discovery using OpenStreetMap Overpass.

This is a partial directory, not an inventory or booking API. It deliberately
selects mapped accommodations with both a telephone and a website; it cannot
establish date-specific rates, occupancy, availability, or business freshness.
City filtering requires an explicit matching ``addr:city`` tag and therefore
misses otherwise valid properties with incomplete addresses. Public Overpass
instances are shared services: each invocation makes one request, without
automatic retries or a second endpoint. Cache discovery results in the caller.

Reference: https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL
Usage: https://dev.overpass-api.de/overpass-doc/en/preface/commons.html
OSM attribution: https://www.openstreetmap.org/copyright
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .normalize import safe_url
from .providers import ProviderError


_ENDPOINT = "https://overpass-api.de/api/interpreter"
_MAX_RESPONSE_BYTES = 4_000_000
_TOURISM = "hotel|motel|hostel|guest_house|apartment|chalet|camp_site"


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _query(city: str | None, timeout: float, max_results: int) -> str:
    # JSON string encoding safely quotes user-controlled city text for QL.
    # Escape regexp metacharacters separately because the city is an exact match.
    city_filter = ""
    if city:
        expression = "^" + re.escape(city) + "$"
        city_filter = '["addr:city"~' + json.dumps(expression, ensure_ascii=False) + ',i]'
    seconds = max(1, min(25, int(timeout) - 2))
    return (
        f'[out:json][timeout:{seconds}][maxsize:33554432];'
        'area["ISO3166-2"="CA-ON"]["admin_level"="4"]->.ontario;'
        f'nwr(area.ontario)["tourism"~"^({_TOURISM})$"]["name"]'
        '[~"^(phone|contact:phone)$"~"."]'
        '[~"^(website|contact:website)$"~"."]'
        f'{city_filter};out tags center {max_results};'
    )


def _address(tags: dict) -> str | None:
    full = _text(tags.get("addr:full"))
    if full:
        return full
    street = _text(tags.get("addr:street")) or _text(tags.get("addr:place"))
    if not street:
        return None
    house = _text(tags.get("addr:housenumber"))
    parts = [" ".join(value for value in (house, street) if value)]
    # Ontario is established by the query boundary, but missing address tags
    # stay missing. In particular, do not infer a city from the request.
    for key in ("addr:city", "addr:province", "addr:state", "addr:postcode", "addr:country"):
        value = _text(tags.get(key))
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts)


def _coordinates(element: dict) -> dict | None:
    point = element if "lat" in element else element.get("center")
    if not isinstance(point, dict):
        return None
    lat, lon = point.get("lat"), point.get("lon")
    if isinstance(lat, bool) or isinstance(lon, bool):
        return None
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    # A coarse Ontario envelope is a defensive check; actual membership is
    # supplied by the Overpass administrative-area filter, not this rectangle.
    if not math.isfinite(lat) or not math.isfinite(lon):
        return None
    if not (41.5 <= lat <= 57.0 and -95.5 <= lon <= -74.0):
        return None
    return {"latitude": lat, "longitude": lon}


def _property(element: dict, fetched_at: str, city: str | None) -> dict | None:
    tags = element.get("tags")
    if not isinstance(tags, dict):
        return None
    kind, identifier = element.get("type"), element.get("id")
    if kind not in {"node", "way", "relation"} or type(identifier) is not int or identifier <= 0:
        return None
    name = _text(tags.get("name"))
    tourism = _text(tags.get("tourism"))
    coordinates = _coordinates(element)
    province = _text(tags.get("addr:province")) or _text(tags.get("addr:state"))
    country = _text(tags.get("addr:country"))
    if province and province.casefold() not in {"on", "ontario", "ca-on"}:
        return None
    if country and country.casefold() not in {"ca", "can", "canada"}:
        return None
    mapped_city = _text(tags.get("addr:city"))
    if city and (not mapped_city or mapped_city.casefold() != city.casefold()):
        return None
    if not name or tourism not in _TOURISM.split("|") or not coordinates:
        return None
    phone = _text(tags.get("phone")) or _text(tags.get("contact:phone"))
    website = _text(tags.get("website")) or _text(tags.get("contact:website"))
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    website = safe_url(website)
    if not phone or not website:
        return None
    source = f"https://www.openstreetmap.org/{kind}/{identifier}"
    return {
        "name": name,
        "address": _address(tags),
        "phone": phone,
        "phone_link": "tel:" + phone,
        "link": website,
        "type": tourism,
        "property_token": f"osm:{kind}:{identifier}",
        "gps_coordinates": coordinates,
        "city": mapped_city,
        "province": "Ontario",
        "country": "Canada",
        "_source_url": source,
        "_fetched_at": fetched_at,
        "_provider_observed_at": None,
        "_location_verified": True,
        "discovery_source": "openstreetmap",
    }


def discover_properties(request: dict, *, timeout: float = 30.0, max_results: int = 60) -> list[dict]:
    """Return a bounded, partial list of mapped Ontario accommodations.

    Dates and guest count are intentionally unused: OSM has no live inventory.
    The output limit is applied by Overpass before validation, so the returned
    list can be shorter than requested. OSM ID order is not a cheapest ranking.
    Raises :class:`ProviderError` for network or malformed/incomplete responses.
    """
    if not isinstance(request, dict):
        raise ProviderError("Discovery request must be an object.", "invalid_input")
    city = request.get("city")
    if city is not None:
        if not isinstance(city, str) or not city.strip() or len(city) > 120 or any(ord(c) < 32 for c in city):
            raise ProviderError("Invalid discovery city.", "invalid_input")
        city = city.strip()
    if request.get("province", "Ontario") != "Ontario" or request.get("country", "Canada") != "Canada":
        raise ProviderError("Discovery is limited to Ontario, Canada.", "invalid_input")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ProviderError("Invalid discovery timeout.", "configuration_error")
    if type(max_results) is not int or not 1 <= max_results <= 500:
        raise ProviderError("Discovery result limit must be from 1 to 500.", "configuration_error")
    network_request = Request(
        _ENDPOINT,
        data=urlencode({"data": _query(city, timeout, max_results)}).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "SprintMasters-HotelSearch/1.0 (Ontario accommodation discovery)",
        },
    )
    try:
        with urlopen(network_request, timeout=timeout) as response:
            body = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        code = "rate_limit" if status == 429 else "provider_error"
        raise ProviderError(f"OpenStreetMap discovery returned HTTP {status}.", code) from None
    except (TimeoutError, socket.timeout):
        raise ProviderError("OpenStreetMap discovery timed out.", "timeout") from None
    except URLError as exc:
        code = "timeout" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else "network_error"
        raise ProviderError("Could not reach OpenStreetMap discovery.", code) from None
    except OSError:
        raise ProviderError("OpenStreetMap discovery connection failed.", "network_error") from None
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ProviderError("OpenStreetMap discovery response exceeded the size limit.", "invalid_response")
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise ProviderError("OpenStreetMap discovery returned invalid JSON.", "invalid_response") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ProviderError("OpenStreetMap discovery returned an invalid response.", "invalid_response")
    # Overpass can return partial elements with a timeout or memory-error remark.
    if payload.get("remark"):
        raise ProviderError("OpenStreetMap discovery could not complete its query.", "provider_error")
    fetched_at = datetime.now(timezone.utc).isoformat()
    properties = []
    seen = set()
    for element in payload["elements"][:max_results]:
        if not isinstance(element, dict):
            continue
        item = _property(element, fetched_at, city)
        if item and item["property_token"] not in seen:
            properties.append(item)
            seen.add(item["property_token"])
    return properties
