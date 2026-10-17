"""Read public Google Hotels HTML with explicit, verified stay context.

Google's ``checkin=`` and ``adults=`` URL arguments are silently ignored.
The compact ``ts`` message below carries the same date/party state produced
by the normal Google Hotels date and adult controls. This is an undocumented
website format: fail closed if its echoed state or property layout changes.
No API key, browser, account, CAPTCHA handling, or booking action is used.
"""

from __future__ import annotations

import base64
from datetime import date
import json
import re
from urllib.parse import urlencode

from .providers import ProviderError
from .web_http import PublicWebClient


def _varint(number):
    result = bytearray()
    while number > 127:
        result.append((number & 127) | 128)
        number >>= 7
    result.append(number)
    return bytes(result)


def _integer(field, number):
    return _varint(field << 3) + _varint(number)


def _message(field, value):
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _date_message(value):
    return b"".join(_integer(i, part) for i, part in enumerate(
        (value.year, value.month, value.day), 1))


def stay_token(request):
    """Encode dates and one room containing the requested adult party."""
    try:
        check_in = date.fromisoformat(request["check_in"])
        check_out = date.fromisoformat(request["check_out"])
        adults = request["guests"]
        nights = (check_out - check_in).days
        if type(adults) is not int or not 1 <= adults <= 20 or not 1 <= nights <= 365:
            raise ValueError
        currency = request["currency"]
        if currency != "CAD":
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise ProviderError("Invalid stay parameters for direct hotel search.", "invalid_input") from None
    party = _message(1, _integer(1, 3)) * adults + _integer(2, 0)
    dates = (_message(1, _date_message(check_in)) +
             _message(2, _date_message(check_out)) + _integer(3, nights))
    stay = _message(1, _message(3, b"")) + _message(2,
        _message(2, dates) + _message(6, _integer(2, 1)))
    # Same state emitted by the public UI's "Lowest price" menu choice.
    currency_state = (_message(1, _integer(5, 3) + _message(7, currency.encode())) +
                      _message(3, b"") + _message(4, _integer(3, 1)))
    message = (_integer(1, 1) + _message(2, party) +
               _message(3, stay) + _message(5, currency_state))
    return base64.urlsafe_b64encode(message).decode().rstrip("=")


def _at(value, *indices):
    try:
        for index in indices:
            value = value[index]
        return value
    except (IndexError, KeyError, TypeError):
        return None


def _properties(value):
    """Find property records only; don't confuse sponsored destination cards."""
    if isinstance(value, list):
        if (len(value) > 20 and isinstance(value[1], str) and
                isinstance(value[2], list) and isinstance(value[20], str) and
                re.fullmatch(r"[A-Za-z0-9_-]{12,300}", value[20]) and
                value[20].startswith(("Cg", "Ch", "Ci", "Cj", "Ck", "Cl", "Cm", "Cn", "Co", "Cp"))):
            yield value
            return
        for child in value:
            yield from _properties(child)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _properties(child)


def _datasets(html):
    for match in re.finditer(
        r"AF_initDataCallback\(\{key:\s*'ds:\d+',\s*hash:\s*'[^']*',\s*data:(.*?),\s*sideChannel:",
        html, re.S,
    ):
        try:
            yield json.loads(match.group(1))
        except (ValueError, RecursionError):
            continue


def _display_amount(value):
    # Only a dedicated monetary field, never prose, crossed-out rates, or dates.
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(?:CA\$|CAD\s*|\$)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", value.strip())
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    return amount if 0 < amount < 10000000 else None


def _echo(request):
    return {"check_in_date": request["check_in"], "check_out_date": request["check_out"],
            "adults": request["guests"], "children": 0, "currency": request["currency"]}


def _stamp(page):
    return {"_source_url": page["url"], "_fetched_at": page.get("fetched_at"),
            "_provider_observed_at": page.get("observed_at") or page.get("fetched_at")}


def _verified_context(raw, request):
    context = _at(raw, 6, 1)
    expected_dates = [list(map(int, request[key].split("-"))) for key in ("check_in", "check_out")]
    return (isinstance(context, list) and _at(context, 3) == request["currency"] and
            _at(context, 4, 0) == expected_dates[0] and
            _at(context, 4, 1) == expected_dates[1] and
            _at(context, 13, 0) == request["guests"] and
            _at(context, 13, 2) in (None, 0) and
            _at(raw, 6, 2, 8, 0) == expected_dates[0] and
            _at(raw, 6, 2, 8, 1) == expected_dates[1])


def _property(raw, request, page, *, quote=True):
    result = {"name": raw[1], "property_token": raw[20],
              "currency": request["currency"], **_stamp(page)}
    metadata = raw[2]
    coordinates = _at(metadata, 0)
    if isinstance(coordinates, list) and len(coordinates) == 2:
        result["gps_coordinates"] = dict(zip(("latitude", "longitude"), coordinates))
    address = _at(metadata, 1, 0, 0, 0)
    if isinstance(address, str):
        result["address"] = address
    for index, key in ((0, "phone"), (1, "phone_link")):
        phone = _at(metadata, 2, index)
        if isinstance(phone, str):
            result[key] = phone
    website = _at(metadata, 29, 2)
    if isinstance(website, str) and website.startswith(("https://", "http://")):
        result["link"] = website
    if isinstance(_at(raw, 3, 0), str):
        result["hotel_class"] = raw[3][0]
    if isinstance(_at(raw, 11, 0), str):
        result["description"] = raw[11][0]
    result["_location_evidence"] = _at(metadata, 21)
    if quote and _verified_context(raw, request):
        nightly = _at(raw, 6, 2, 1)
        total = _at(raw, 6, 2, 9)
        nightly_base = _display_amount(_at(nightly, 0))
        nightly_with_tax = _display_amount(_at(nightly, 1))
        total_base = _display_amount(_at(total, 0))
        total_with_tax = _display_amount(_at(total, 1))
        result["rate_per_night"] = {"extracted_lowest": nightly_with_tax,
                                    "extracted_before_taxes_fees": nightly_base}
        result["total_rate"] = {"extracted_lowest": total_with_tax,
                                "extracted_before_taxes_fees": total_base}
        result["_price_evidence"] = {
            "source": "Google Hotels embedded property record",
            "verified_check_in": request["check_in"], "verified_check_out": request["check_out"],
            "verified_adults": _at(raw, 6, 1, 13, 0), "verified_currency": _at(raw, 6, 1, 3),
            "displayed_nightly_base_and_with_taxes_fees": nightly[:2] if isinstance(nightly, list) else None,
            "displayed_stay_base_and_with_taxes_fees": total,
            "amount_precision": "displayed rounded amounts; verify exact total by phone",
            "basis": "stay", "tax_fee_status": "included_as_displayed",
            "price_unit": "unknown", "availability": "online_listing_only",
        }
    return result


class GoogleHotelsSource:
    estimated_cost = 0
    pagination_supported = False

    def __init__(self, timeout=20, retries=1, fresh=False, cache_dir="outputs/web-cache", *, client=None):
        self.client = client or PublicWebClient(timeout=timeout, retries=retries,
                                                fresh=fresh, cache_dir=cache_dir)

    @property
    def request_count(self):
        return self.client.request_count

    @property
    def cache_hits(self):
        return self.client.cache_hits

    def _url(self, request, property_token=None, vacation_rentals=False):
        params = {"hl": "en", "gl": "ca", "curr": request["currency"], "ts": stay_token(request)}
        if property_token is not None:
            if not isinstance(property_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,300}", property_token):
                raise ProviderError("Invalid Google Hotels property token.", "invalid_input")
            base = "https://www.google.com/travel/hotels/entity/" + property_token
        else:
            location = ", ".join(value for value in (request.get("city"), "Ontario", "Canada") if value)
            params["q"] = ("vacation rentals in " if vacation_rentals else "hotels in ") + location
            base = "https://www.google.com/travel/search"
        return base + "?" + urlencode(params)

    def _page(self, url):
        page = self.client.get(url)
        text = page["text"]
        if ("/sorry/" in page["url"] or "consent.google." in page["url"] or
                "/travel/unsupported" in page["url"] or
                "Our systems have detected unusual traffic" in text or "g-recaptcha" in text):
            raise ProviderError("Google Hotels requires a supported session or access check; no bypass was attempted.", "source_blocked")
        return page

    def search(self, request, *, next_page_token=None, vacation_rentals=False):
        if next_page_token:
            raise ProviderError("Direct hotel search does not support result-page tokens.", "invalid_input")
        page = self._page(self._url(request, vacation_rentals=vacation_rentals))
        adults = set(re.findall(r'data-adults="(\d+)"', page["text"]))
        if adults != {str(request["guests"])}:
            raise ProviderError("Google Hotels did not echo the requested adult count.", "query_mismatch")
        records = {}
        for dataset in _datasets(page["text"]):
            for raw in _properties(dataset):
                candidate = _property(raw, request, page)
                # Mismatched individual quotes remain contact/discovery candidates.
                previous = records.get(raw[20])
                if previous is None or ("total_rate" in candidate and "total_rate" not in previous):
                    records[raw[20]] = candidate
        if not records:
            raise ProviderError("No readable Google Hotels property records were returned.", "no_results")
        if not any("total_rate" in row for row in records.values()):
            raise ProviderError("Google Hotels did not return verifiable rates for the requested stay.", "query_mismatch")
        return {"properties": list(records.values()), "search_parameters": _echo(request),
                "_sort_order": "lowest_price" if 'aria-label="Lowest price, Sort by, Selected"' in page["text"] else "unknown",
                "_coverage_warning": "First Google Hotels result set only; not exhaustive or guaranteed cheapest.",
                **_stamp(page)}

    def details(self, request, property_token, *, vacation_rentals=False):
        page = self._page(self._url(request, property_token=property_token))
        for dataset in _datasets(page["text"]):
            for raw in _properties(dataset):
                if raw[20] == property_token:
                    # Preserve search-page quote provenance. Detail records are used
                    # for exact-entity contact enrichment, never mismatched prices.
                    result = _property(raw, request, page, quote=False)
                    return {**result, "search_parameters": _echo(request)}
        raise ProviderError("Google Hotels did not return the requested property's details.", "invalid_response")
