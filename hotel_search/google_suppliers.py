"""Supplier quotes disclosed by public Google Hotels property records.

These are explicitly *Google-discovered* offers, not independent searches of
the supplier websites. Undocumented array layouts are accepted only at the
observed property/offer paths, never by proximity to a seller name in prose.
Unknown layouts fail closed. No redirect URLs are fetched by this module.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from .google_hotels import _at, _datasets, _display_amount, _properties, _verified_context, stay_token
from .providers import ProviderError


_ALIASES = {
    "booking.com": "booking", "booking": "booking",
    "priceline": "priceline", "priceline.com": "priceline",
    "airbnb": "airbnb", "airbnb.ca": "airbnb", "airbnb.com": "airbnb",
    "expedia": "expedia", "expedia.ca": "expedia", "expedia.com": "expedia",
    "hotels.com": "hotels", "hotels.com canada": "hotels",
    "agoda": "agoda", "agoda.com": "agoda",
    "hostelworld": "hostelworld", "hostelworld.com": "hostelworld",
    "vio.com": "vio", "bluepillow.ca": "bluepillow", "bluepillow": "bluepillow",
    "super.com": "super", "trip.com": "trip",
}


def _supplier_url(value):
    """Keep the actual declared destination, without following ad redirects."""
    if not isinstance(value, str):
        return None, None
    if not value.startswith(("/travel/lodging/clk?", "/aclk?", "https://", "http://")):
        return None, None
    booking_url = urljoin("https://www.google.com", value)
    parts = urlsplit(booking_url)
    query = parse_qs(parts.query)
    destination = _at(query, "pcurl", 0)
    if destination is None and parts.hostname not in ("www.google.com", "google.com"):
        destination = booking_url
    if isinstance(destination, str) and destination.startswith(("https://", "http://")):
        return destination, booking_url
    return None, booking_url


def _destination_mismatch(url, request):
    """Reject explicit conflicting stay facts, including supplier redirects.

    The Google property context still supplies the positive scope evidence;
    a destination need not repeat every field. Prices are never read from URL
    parameters, whose tax/fee and unit meanings vary by supplier.
    """
    if not url:
        return False
    query = {key.casefold(): values for key, values in parse_qs(urlsplit(url).query).items()}
    # Some organic pcurl URLs retain a percent-encoded entire query string.
    if len(query) == 1 and any("=" in key or "%3d" in key for key in query):
        query = {key.casefold(): values for key, values in
                 parse_qs(unquote(urlsplit(url).query)).items()}
    checks = (
        (("checkin", "check_in", "startdate"), request["check_in"]),
        (("checkout", "check_out", "enddate"), request["check_out"]),
        (("group_adults", "req_adults", "adults", "numberofadults"), str(request["guests"])),
        (("currency", "selected_currency", "cur", "partnercurrency", "mpg", "mpl"), "CAD"),
        (("group_children", "req_children", "numberofchildren"), "0"),
    )
    for keys, expected in checks:
        for key in keys:
            for observed in query.get(key, []):
                normalized = observed
                if key in ("checkin", "checkout") and re.fullmatch(r"\d{8}", observed):
                    normalized = f"{observed[:4]}-{observed[4:6]}-{observed[6:]}"
                if normalized != expected:
                    return True
    # Agoda encodes dates as six separate calendar components.
    for prefix, expected in (("ckin", request["check_in"]), ("ckout", request["check_out"])):
        for suffix, part in zip(("year", "month", "day"), expected.split("-")):
            values = query.get(prefix + suffix, [])
            if any(not value.isdecimal() or int(value) != int(part) for value in values):
                return True
    return False


def extract_supplier_offers(html: str, request: dict, *, property_token: str,
                            source_url: str, observed_at: str | None = None) -> list[dict]:
    """Extract seller-coherent quotes for one exact property and requested stay.

    Known shapes: property[6][2][2|12|21] contains offer records. offer[0]
    identifies the seller; offer[12][4] and [5] are dedicated nightly/stay
    monetary tuples, respectively. Index 1 in each tuple is the displayed
    tax-inclusive amount, but tax_fee_status remains unknown because platform
    mandatory fees may still differ. The output preserves the original fields.
    A mismatched scope retains a discovery link with null price and false
    scope_verified. No rounded nightly rate is multiplied into a stay total.
    """
    try:
        stay_token(request)
    except (ProviderError, TypeError):
        return []
    if not isinstance(html, str) or not isinstance(property_token, str):
        return []
    result = {}
    for dataset in _datasets(html):
        for raw in _properties(dataset):
            if raw[20] != property_token:
                continue
            property_verified = _verified_context(raw, request)
            for group_index in (21, 12, 2):
                offers = _at(raw, 6, 2, group_index)
                if not isinstance(offers, list):
                    continue
                for offer in offers:
                    label = _at(offer, 0, 0)
                    if not isinstance(label, str):
                        continue
                    platform = _ALIASES.get(label.strip().casefold())
                    if platform is None or not isinstance(_at(offer, 12), list):
                        continue
                    listing_url, booking_url = _supplier_url(_at(offer, 0, 2))
                    if booking_url is None:
                        continue
                    occupancy = _at(offer, 12, 12, 0)
                    occupancy_verified = (
                        isinstance(occupancy, list) and
                        type(_at(occupancy, 0)) is int and
                        occupancy[0] == request["guests"] and
                        _at(occupancy, 2) in (None, 0)
                    )
                    verified = (property_verified and occupancy_verified and
                                not _destination_mismatch(listing_url, request))
                    total = _at(offer, 12, 5)
                    nightly = _at(offer, 12, 4)
                    amount, basis = None, "unknown"
                    if verified:
                        amount = _display_amount(_at(total, 1))
                        if amount is not None:
                            basis = "stay"
                        else:
                            amount = _display_amount(_at(nightly, 1))
                            if amount is not None:
                                basis = "night"
                    row = {
                        "platform": platform, "provider": label, "listing_id": None,
                        "listing_url": listing_url, "booking_url": booking_url,
                        "source_url": source_url, "observed_at": observed_at,
                        "discovery_method": "google_hotels_supplier", "amount": amount,
                        "currency": "CAD", "basis": basis, "tax_fee_status": "unknown",
                        "check_in": request["check_in"], "check_out": request["check_out"],
                        "guests": request["guests"], "scope_verified": verified,
                        "raw": {
                            "property_token": property_token, "supplier_label": label,
                            "supplier_id": _at(offer, 0, 1),
                            "record_path": [6, 2, group_index],
                            "displayed_stay_base_and_with_taxes_fees": total,
                            "displayed_nightly_base_and_with_taxes_fees": nightly,
                            "supplier_occupancy": occupancy,
                            "property_context_verified": property_verified,
                            "supplier_destination_mismatch": _destination_mismatch(listing_url, request),
                            "price_precision": "Displayed rounded quote; verify exact total by phone.",
                        },
                    }
                    # Do not count paid/organic duplicates as extra inventory.
                    # Prefer organic URL evidence; retain genuinely different rates.
                    key = (platform, amount, basis, verified)
                    previous = result.get(key)
                    if previous is None or (listing_url and not previous["listing_url"]):
                        result[key] = row
    return list(result.values())

