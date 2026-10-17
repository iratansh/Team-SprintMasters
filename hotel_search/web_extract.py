"""Extract one accommodation from an ordinary HTTP response, without inventing rates.

Generic structured prices are useful evidence, but are not date/occupancy quotes.
Only explicitly scoped offers may populate fields used for price ranking.
"""

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from .normalize import safe_url


LODGING_TYPES = {
    "Hotel", "Motel", "Hostel", "BedAndBreakfast", "LodgingBusiness", "Resort",
    "Campground", "CampingPitch", "VacationRental", "GuestHouse", "Inn",
}
LODGING_WORD = re.compile(r"\b(?:hotel|motel|hostel|inn|resort|lodge|campground|guest\s*house|bed\s*(?:and|&)\s*breakfast|b\s*&\s*b)\b", re.I)


def _text(value):
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None


def _types(node):
    value = node.get("@type", [])
    return {str(item).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            for item in (value if isinstance(value, list) else [value])}


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.json_blocks = []
        self.titles, self.headings, self.addresses, self.phones = [], [], [], []
        self.capture = None
        self.buffer = []
        self.script_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"meta", "link", "img", "input", "br", "hr", "source", "wbr", "area", "base", "embed", "param", "track", "col"}:
            if tag == "br" and self.capture:
                self.buffer.append(", " if self.capture == "address" else " ")
            return
        self.stack.append(tag)
        if tag == "script":
            self.script_depth += 1
            if attrs.get("type", "").split(";", 1)[0].strip().lower() == "application/ld+json" and self.capture is None:
                self.capture, self.buffer = "json", []
            return
        if self.script_depth or set(self.stack) & {"footer", "nav", "aside"}:
            return
        if tag in {"title", "h1", "address"} and self.capture is None:
            self.capture, self.buffer = tag, []
        if tag == "a" and attrs.get("href", "").lower().startswith("tel:"):
            self.phones.append(attrs["href"])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1] == tag:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        expected = "script" if self.capture == "json" else self.capture
        if tag == expected:
            value = "".join(self.buffer)
            target = {"json": self.json_blocks, "title": self.titles,
                      "h1": self.headings, "address": self.addresses}[self.capture]
            target.append(value if self.capture == "json" else _text(value))
            self.capture, self.buffer = None, []
        if tag == "script":
            self.script_depth = max(0, self.script_depth - 1)
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]


def _address(value):
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, dict):
        return None
    country = value.get("addressCountry")
    if isinstance(country, dict):
        country = country.get("name") or country.get("@id")
    if _text(country) and country.upper() == "CA":
        country = "Canada"
    region = value.get("addressRegion")
    if isinstance(region, dict):
        region = region.get("name")
    regional = " ".join(filter(None, [_text(region), _text(value.get("postalCode"))]))
    return ", ".join(filter(None, [_text(value.get("streetAddress")),
                                    _text(value.get("addressLocality")), regional, _text(country)])) or None


def _primary(nodes):
    candidates = [node for node in nodes if _types(node) & LODGING_TYPES and _text(node.get("name"))]
    by_id = {node["@id"]: node for node in candidates if isinstance(node.get("@id"), str)}
    # WebPage.mainEntity and site-root mainEntity are explicit ownership evidence.
    selected = []
    for node in nodes:
        if not (_types(node) & {"WebPage", "ItemPage"}) and "@type" in node:
            continue
        entity = node.get("mainEntity")
        for item in entity if isinstance(entity, list) else [entity]:
            if isinstance(item, dict):
                reference = item.get("@id")
                item = by_id.get(reference, item) if isinstance(reference, str) else item
                if item in candidates:
                    selected.append(item)
            elif isinstance(item, str) and item in by_id:
                selected.append(by_id[item])
    pool = selected or candidates
    unique = {}
    for item in pool:
        reference = item.get("@id")
        identity = reference if isinstance(reference, str) and reference else (_text(item.get("name")), _address(item.get("address")))
        # Repeated graph references with only an @id do not overwrite richer nodes.
        if identity not in unique or len(item) > len(unique[identity]):
            unique[identity] = item
    return next(iter(unique.values())) if len(unique) == 1 else None, bool(candidates)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        number = Decimal(str(value))
        if number.is_finite() and 0 < number < 10000000:
            result = float(number.quantize(Decimal("0.01")))
            return result if result > 0 else None
    except (InvalidOperation, ValueError, OverflowError):
        pass
    return None


def _date(value):
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T)", value):
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


def _find(mapping, keys):
    return next((mapping[key] for key in keys if key in mapping), None)


def _offer_price(offer):
    """Return (amount, basis, currency) only when the price basis is explicit."""
    total = offer.get("totalPrice")
    currency = offer.get("priceCurrency")
    if isinstance(total, dict):
        currency = total.get("priceCurrency") or total.get("currency") or currency
        total = total.get("value", total.get("price"))
    if _number(total) is not None:
        return _number(total), "stay", currency
    specifications = offer.get("priceSpecification", [])
    if isinstance(specifications, dict):
        specifications = [specifications]
    for spec in specifications if isinstance(specifications, list) else []:
        if not isinstance(spec, dict):
            continue
        unit = _text(spec.get("unitText")) or ""
        if re.fullmatch(r"(?:per\s+)?night", unit, re.I):
            amount = _number(spec.get("price"))
            if amount is not None:
                return amount, "night", spec.get("priceCurrency") or currency
    return None, None, currency


def _prices(node, request, source_url, result):
    offers = node.get("offers", [])
    offers = offers if isinstance(offers, list) else [offers]
    indicative = []
    if node.get("priceRange") is not None:
        indicative.append({"text": str(node["priceRange"]), "reason": "unscoped_price_range", "source_url": source_url})
    validated = []
    for offer in offers:
        if not isinstance(offer, dict):
            continue
        amount, basis, currency = _offer_price(offer)
        currency = currency.upper() if isinstance(currency, str) else None
        check_in = _date(_find(offer, ("checkIn", "checkinDate", "checkInDate", "checkinTime", "arrivalDate")))
        check_out = _date(_find(offer, ("checkOut", "checkoutDate", "checkOutDate", "checkoutTime", "departureDate")))
        adults = _find(offer, ("numAdults", "numberOfAdults", "adults"))
        if isinstance(adults, str) and re.fullmatch(r"\d+", adults):
            adults = int(adults)
        children = _find(offer, ("numChildren", "numberOfChildren", "children"))
        no_children = children is None or type(children) is int and children == 0 or children == "0"
        scope_ok = (check_in is not None and check_in == request.get("check_in")
                    and check_out is not None and check_out == request.get("check_out")
                    and type(adults) is int and adults == request.get("guests") and no_children
                    and isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency))
        if scope_ok and amount is not None and "AggregateOffer" not in _types(offer):
            # Explicitly unavailable room offers are not useful ranking quotes.
            availability = str(offer.get("availability", "")).rsplit("/", 1)[-1]
            if availability not in {"OutOfStock", "SoldOut", "Discontinued"}:
                validated.append((amount, basis, currency, offer))
        evidence = {key: offer[key] for key in ("price", "totalPrice", "lowPrice", "highPrice", "priceCurrency", "priceSpecification", "validFrom", "validThrough", "checkIn", "checkOut", "numAdults", "availability") if key in offer}
        usable = scope_ok and amount is not None and "AggregateOffer" not in _types(offer) and str(offer.get("availability", "")).rsplit("/", 1)[-1] not in {"OutOfStock", "SoldOut", "Discontinued"}
        if evidence and not usable:
            indicative.append({"evidence": evidence, "reason": "not_a_matching_stay_quote", "source_url": source_url})
    if indicative:
        result["_indicative_price_evidence"] = indicative
    if not validated:
        return
    # Compare only quotes with the same currency and price basis. Mixed offers
    # are ambiguous; leave them unranked rather than comparing nights to stays.
    if len({(item[1], item[2]) for item in validated}) != 1:
        result.setdefault("_extraction_warnings", []).append("ambiguous_offer_price_basis_or_currency")
        return
    amount, basis, currency, offer = min(validated, key=lambda item: item[0])
    result["total_rate" if basis == "stay" else "rate_per_night"] = {
        "extracted_lowest": amount, "lowest": f"{currency} {amount:.2f}",
    }
    result["currency"] = currency
    result["num_guests"] = request["guests"]
    result["_price_scope"] = {key: request[key] for key in ("check_in", "check_out", "guests")}
    result["_price_scope"]["currency"] = currency
    unit = str(offer.get("unitText", "")).lower()
    if re.fullmatch(r"(?:per\s+)?(?:room|bed)", unit):
        result["_price_unit"] = "bed" if "bed" in unit else "room"
    room = offer.get("itemOffered")
    if isinstance(room, dict) and _types(room) & {"HotelRoom", "Room", "Suite"}:
        result["_price_unit"] = "room"
        result["room_type"] = _text(room.get("name")) or _text(room.get("accommodationCategory"))
        if room.get("bed") is not None:
            result["bed_type"] = room["bed"]
    vat = offer.get("valueAddedTaxIncluded")
    if isinstance(vat, bool):
        result["_tax_included"] = vat
    # VAT inclusion says nothing about mandatory resort/cleaning/service fees.
    result["_tax_fee_status"] = "unknown"


def _fallback(page):
    headings = list(dict.fromkeys(value for value in page.headings if value and LODGING_WORD.search(value)))
    titles = list(dict.fromkeys(value for value in page.titles if value))
    if len(headings) > 1:
        return None
    title = titles[0] if len(titles) == 1 else None
    name = headings[0] if headings else None
    if not name and title:
        name = next((part.strip() for part in re.split(r"\s+[|–—-]\s+|\s*\|\s*", title)
                     if LODGING_WORD.search(part)), None)
    if not name or re.search(r"\b(?:hotels|motels|hostels|resorts|best|cheapest|search|results|top\s+\d+)\b", name, re.I):
        return None
    addresses = list(dict.fromkeys(value for value in page.addresses if value))
    phones = list(dict.fromkeys(page.phones))
    # Multiple distinct contacts are not reliably attributable to one property.
    if len(addresses) > 1 or len(phones) > 1:
        return None
    return {"name": name, "address": addresses[0] if addresses else None,
            "telephone": phones[0][4:] if phones else None,
            "@type": "LodgingBusiness"}


def extract_property(html: str, source_url: str, request: dict) -> dict | None:
    """Return one coherent raw property, or None when identity is ambiguous.

    ``request`` is used to verify explicit quote scope, never to fill evidence.
    A successful extraction is a discovered lead, not confirmed availability.
    """
    source_url = safe_url(source_url)
    if not isinstance(html, str) or not source_url:
        return None
    page = _Page()
    try:
        page.feed(html)
        page.close()
    except (ValueError, RecursionError):
        return None
    nodes = []
    for block in page.json_blocks:
        try:
            nodes.extend(_walk(json.loads(block)))
        except (ValueError, TypeError, RecursionError):
            continue
    node, had_candidates = _primary(nodes)
    if node is None:
        if had_candidates:
            return None
        node = _fallback(page)
    if node is None:
        return None
    parts = urlsplit(source_url)
    canonical = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))
    phone = _text(node.get("telephone"))
    if phone and phone.lower().startswith("tel:"):
        phone = phone[4:]
    result = {
        "name": _text(node.get("name")), "address": _address(node.get("address")),
        "phone": phone, "phone_link": "tel:" + phone if phone else None,
        "link": (safe_url(urljoin(source_url, node.get("url"))) if isinstance(node.get("url"), str) else None) or source_url,
        "type": next(iter(sorted(_types(node) & LODGING_TYPES)), "LodgingBusiness"),
        "property_token": "web:" + hashlib.sha256(canonical.encode()).hexdigest()[:24],
        "_source_url": source_url, "_fetched_at": datetime.now(timezone.utc).isoformat(),
        "_provider_observed_at": None, "discovery_source": "official_website",
        "availability_status": "unknown",
    }
    if node.get("bed") is not None:
        result["bed_type"] = node["bed"]
    if node.get("accommodationCategory") is not None:
        result["room_type"] = node["accommodationCategory"]
    _prices(node, request, source_url, result)
    return result
