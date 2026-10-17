"""Conservative normalization: a search listing is never a phone confirmation."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import math
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def normalized(value):
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKD", value or "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", value.encode("ascii", "ignore").decode().lower()).split())


def money(value):
    """Only trust extracted numeric fields, never parse a '$... from' snippet."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 < amount < Decimal("10000000"):
            return None
        rounded = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(rounded) if rounded > 0 else None
    except (InvalidOperation, ValueError):
        return None


def safe_url(value):
    value = text(value)
    if not value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname or parts.username:
            return None
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in {"api_key", "apikey", "access_token", "authorization"}]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except ValueError:
        return None


def clean_evidence(value):
    if isinstance(value, dict):
        return {k: clean_evidence(v) for k, v in value.items()
                if k.lower() not in {"api_key", "apikey", "authorization", "access_token"}}
    if isinstance(value, list):
        return [clean_evidence(v) for v in value]
    if isinstance(value, str) and value.startswith(("https://", "http://")):
        return safe_url(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def normalize_phone(phone_link, phone):
    raw = text(phone_link) or text(phone)
    if not raw:
        return None, None
    raw = re.sub(r"^tel:", "", raw, flags=re.I)
    match = re.search(r"(?:;ext=|\bext\.?\s*|\bx\s*)(\d+)\s*$", raw, re.I)
    extension = match.group(1) if match else None
    if match:
        raw = raw[:match.start()]
    if re.search(r"[A-Za-z]", raw):
        return None, extension
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10 and not raw.lstrip().startswith("+"):
        digits = "1" + digits
    if not re.fullmatch(r"1[2-9]\d{2}[2-9]\d{6}", digits):
        return None, extension
    return "+" + digits, extension


def _province_status(address):
    """Read province as an address component, never an Ontario-named street."""
    segments = [segment.strip() for segment in address.split(",")]
    if normalized(segments[-1]) in {"us", "usa", "united states", "united states of america"}:
        return "outside_requested_province"
    provinces = r"ON|Ontario|QC|Quebec|BC|British Columbia|AB|Alberta|MB|Manitoba|SK|Saskatchewan|NB|New Brunswick|NS|Nova Scotia|PE|Prince Edward Island|NL|Newfoundland(?: and Labrador)?|NT|Northwest Territories|NU|Nunavut|YT|Yukon"
    found = []
    region_segment = segments[-2] if normalized(segments[-1]) == "canada" and len(segments) > 1 else segments[-1]
    match = re.fullmatch(rf"({provinces})(?:\s+[A-Z]\d[A-Z]\s?\d[A-Z]\d)?", region_segment, re.I)
    if match:
        found.append(normalized(match.group(1)))
    # Also accept the common unseparated 'Kingston ON K7L 1A1' locality format.
    if not found:
        match = re.search(rf"\b({provinces})\s+[A-Z]\d[A-Z]\s?\d[A-Z]\d\s*(?:,\s*Canada)?\s*$", address, re.I)
        if match:
            found.append(normalized(match.group(1)))
    if any(province not in {"on", "ontario"} for province in found):
        return "outside_requested_province"
    return "matched" if found else "unknown"


def location_status(address, request):
    """Check Ontario membership; apply locality restrictions only in city mode."""
    if not text(address):
        return "unknown"
    province = _province_status(address)
    if request.scope == "province":
        return province
    segments = [normalized(s) for s in address.split(",")]
    city_matches = normalized(request.city) in segments
    province_matches = province == "matched"
    if city_matches and province_matches:
        return "matched"
    if province_matches and not city_matches and len(segments) >= 3:
        return "outside_requested_city"
    return "unknown"


def details_match(listing, details):
    if not isinstance(details, dict) or not text(details.get("name")):
        return False
    if details.get("property_token") and listing.get("property_token") and details["property_token"] != listing["property_token"]:
        return False
    left_address, right_address = text(listing.get("address")), text(details.get("address"))
    if left_address and right_address and normalized(left_address) != normalized(right_address):
        return False
    left, right = listing.get("gps_coordinates"), details.get("gps_coordinates")
    if isinstance(left, dict) and isinstance(right, dict):
        try:
            coords = [float(point[key]) for point in (left, right) for key in ("latitude", "longitude")]
            if not all(math.isfinite(n) for n in coords):
                return False
            lat1, lng1, lat2, lng2 = map(math.radians, coords)
            a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
            distance_km = 6371 * 2 * math.asin(math.sqrt(min(1, a)))
            if distance_km > 0.5:
                return False
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
    if text(details.get("property_token")) and text(listing.get("property_token")):
        return True
    return normalized(details.get("name")) == normalized(listing.get("name"))


def _conditions(raw):
    result = []
    for key in ("discount_remarks", "conditions", "rate_name", "rate_type"):
        values = raw.get(key, [])
        if isinstance(values, str):
            values = [values]
        if isinstance(values, list):
            result.extend(v for v in values if isinstance(v, str))
    for key in ("rooms", "rates"):
        children = raw.get(key, [])
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    result.extend(_conditions(child))
    return list(dict.fromkeys(result))


def _price_unit(raw):
    rooms = raw.get("rooms", [])
    labels = [text(raw.get("name")) or ""]
    if isinstance(rooms, list):
        labels.extend(text(room.get("name")) or "" for room in rooms if isinstance(room, dict))
    units = set()
    for label in labels:
        if re.search(r"\bbed in\b|\bdormitory\b", label, re.I):
            units.add("bed")
        elif re.search(r"\broom\b", label, re.I):
            units.add("room")
    return next(iter(units)) if len(units) == 1 else "unknown"


def _quote(raw, request, source_url, observed_at, fetched_at, *, aggregate=False):
    selected_offer = raw.get("_selected_platform_offer") or raw.get("_platform_offer") or {}
    if not isinstance(selected_offer, dict):
        selected_offer = {}
    platform = selected_offer.get("platform") or raw.get("_platform")
    aggregate_provider = (text(selected_offer.get("provider")) or text(platform)) if platform not in (None, "google") else "Google Hotels aggregate"
    aggregate_booking_url = safe_url(selected_offer.get("listing_url") or selected_offer.get("booking_url")) if platform not in (None, "google") else None
    total = raw.get("total_rate") if isinstance(raw.get("total_rate"), dict) else {}
    nightly = raw.get("rate_per_night") if isinstance(raw.get("rate_per_night"), dict) else {}
    lowest = money(total.get("extracted_lowest"))
    pre_tax = money(total.get("extracted_before_taxes_fees"))
    amount = lowest if lowest is not None else pre_tax
    remarks = _conditions(raw)
    restricted = bool(re.search(r"member|loyalty|sign.?in|app.only|mobile.only|reward|resident|aaa|aarp|military", " ".join(remarks), re.I))
    occupancy = raw.get("num_guests")
    if isinstance(occupancy, str) and re.fullmatch(r"\d{1,3}", occupancy):
        occupancy = int(occupancy)
    # SerpApi documents num_guests as capacity, not the requested adult count.
    occupancy_matches = occupancy is None or type(occupancy) is int and occupancy >= request.guests
    if type(occupancy) is not int:
        occupancy = None
    raw_currency = raw.get("currency")
    currency_matches = raw_currency is None or raw_currency == request.currency
    return {
        "amount": amount,
        "currency": text(raw_currency) or request.currency,
        "basis": "stay" if amount is not None else "unknown",
        # A provider's 'lowest' total does not prove every mandatory fee is included.
        "tax_fee_status": "excluded" if lowest is None and pre_tax is not None else "unknown",
        "before_taxes_fees_amount": pre_tax,
        "nightly_amount": money(nightly.get("extracted_lowest")),
        "nightly_before_taxes_fees_amount": money(nightly.get("extracted_before_taxes_fees")),
        "price_unit": raw.get("_price_unit") if raw.get("_price_unit") in {"room", "bed"} else _price_unit(raw) if not aggregate else "unknown",
        "num_guests": occupancy,
        "num_guests_is_capacity": True,
        "occupancy_matches": occupancy_matches,
        "currency_matches": currency_matches,
        "rate_eligibility": "restricted" if restricted else "no_restrictions_reported",
        "conditions": remarks,
        "provider": text(raw.get("source")) or (aggregate_provider if aggregate else None),
        "booking_url": safe_url(raw.get("link")) if not aggregate else aggregate_booking_url,
        "source_url": source_url,
        "observed_at": observed_at,
        "retrieved_at": fetched_at,
        "raw": clean_evidence({k: raw[k] for k in (
            "source", "link", "rate_per_night", "total_rate", "num_guests", "currency",
            "discount_remarks", "rooms", "free_cancellation", "free_cancellation_until_date", "_price_evidence", "_price_scope", "_selected_platform_offer", "_platform_offer"
        ) if k in raw}),
    }


def normalize_property(listing, details, request, *, source_url, observed_at, fetched_at,
                       detail_error=None, vacation_rentals=False):
    warnings = []
    listing_source_url, listing_observed_at, listing_fetched_at = source_url, observed_at, fetched_at
    detail_ok = bool(details) and details_match(listing, details)
    if details and not detail_ok:
        warnings.append("property_details_identity_mismatch")
    data = details if detail_ok else listing
    warnings.extend(value for value in data.get("_extraction_warnings", []) if isinstance(value, str))
    name = text(data.get("name")) or text(listing.get("name"))
    if not name:
        return None
    token = text(listing.get("property_token"))
    address = text(data.get("address"))
    phone, extension = normalize_phone(data.get("phone_link"), data.get("phone"))
    source_url = safe_url(data.get("_source_url")) or source_url
    observed_at = data.get("_provider_observed_at") or observed_at
    fetched_at = data.get("_fetched_at") or fetched_at
    offers = []
    for key in ("prices", "featured_prices"):
        rows = data.get(key, [])
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    offers.append(_quote(row, request, source_url, observed_at, fetched_at))
    usable = [o for o in offers if o["amount"] is not None and o["occupancy_matches"] and o["currency_matches"]]
    unrestricted = [o for o in usable if o["rate_eligibility"] != "restricted"]
    # Prefer a coherent named offer; never join a price from A to a booking link from B.
    pool = unrestricted or usable
    if pool:
        quote = min(pool, key=lambda o: (o["tax_fee_status"] == "excluded", o["amount"]))
    else:
        quote = _quote(data, request, source_url, observed_at, fetched_at, aggregate=True)
        # An aggregate can hide membership or occupancy conditions in its offers.
        if offers and not unrestricted:
            quote["rate_eligibility"] = "not_established"
    if detail_ok and quote["amount"] is None and not offers:
        # Contact-only details must not erase the search page's dated quote or provenance.
        fallback = normalize_property(listing, None, request, source_url=listing_source_url,
                                      observed_at=listing_observed_at, fetched_at=listing_fetched_at)
        if fallback and fallback["online_quote"]["amount"] is not None:
            quote = fallback["online_quote"]
            offers = fallback["online_offers"]
            warnings.append("price_from_search_listing")
    locality = location_status(address, request)
    if (locality == "unknown" and address and data.get("_location_verified") is True
            and data.get("province") == "Ontario" and request.scope == "province"):
        locality = "matched"
    if detail_error:
        warnings.append("property_details_unavailable")
    reasons = []
    if not phone:
        reasons.append("missing_or_invalid_phone")
    if not address:
        reasons.append("missing_address")
    if locality != "matched":
        reasons.append(locality if locality != "unknown" else "location_unverified")
    if details and not detail_ok:
        reasons.append("property_identity_unverified")
    if quote["amount"] is None:
        reasons.append("missing_full_stay_price")
    if quote["rate_eligibility"] in ("restricted", "not_established"):
        reasons.append("rate_eligibility_unverified")
    if not quote["occupancy_matches"]:
        reasons.append("occupancy_mismatch")
    if not quote["currency_matches"]:
        reasons.append("currency_mismatch")
    identity = token or "|".join((normalized(name), normalized(address)))
    candidate_id = "hotel_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    enquiry_blockers = {"missing_or_invalid_phone", "missing_address", "location_unverified",
                        "outside_requested_city", "outside_requested_province", "property_identity_unverified"}
    return {
        "candidate_id": candidate_id,
        "rank": None,
        "property_name": name,
        "property_type": text(data.get("type")) or ("vacation rental" if vacation_rentals else "unknown"),
        "address": address,
        "phone": phone,
        "phone_raw": text(data.get("phone")) or text(data.get("phone_link")),
        "phone_extension": extension,
        "website": safe_url(data.get("link")),
        "property_token": token,
        "location_status": locality,
        "stay": {k: request.to_dict()[k] for k in ("check_in", "check_out", "nights", "guests")},
        "online_quote": quote,
        "online_offers": offers,
        "availability_status": "online_rate_found" if quote["amount"] is not None else "unknown",
        "phone_verification_required": True,
        "call_ready": not reasons,
        "enquiry_ready": not any(reason in enquiry_blockers for reason in reasons),
        "not_call_ready_reasons": reasons,
        "source_url": source_url,
        "observed_at": observed_at,
        "retrieved_at": fetched_at,
        "warnings": warnings,
        "discovery_source": text(data.get("discovery_source")) or text(listing.get("discovery_source")),
        "contact_source_url": safe_url(data.get("_contact_source_url")) or source_url,
        "indicative_prices": clean_evidence(data.get("_indicative_price_evidence", [])),
        "online_price_evidence": clean_evidence(data.get("_price_evidence", listing.get("_price_evidence"))),
        "platforms": sorted(set(listing.get("_platforms", []) + data.get("_platforms", []))),
        "platform_offers": clean_evidence(data.get("_platform_offers", listing.get("_platform_offers", []))),
    }


def rank_hotels(hotels):
    """Group unlike tax bases; rank by full-stay amount within each group."""
    def group(h):
        q = h["online_quote"]
        if q["amount"] is None or not q["currency_matches"]:
            return "unpriced_or_currency_mismatch"
        return f'{q["currency"]}:stay:{q["tax_fee_status"]}:{q["price_unit"]}'

    groups = sorted(set(group(h) for h in hotels), key=lambda g: (g.startswith("unpriced"), ":excluded:" in g, g))
    group_order = {key: i for i, key in enumerate(groups)}
    hotels.sort(key=lambda h: (group_order[group(h)], h["online_quote"]["amount"] or float("inf"), h["property_name"].casefold(), h["candidate_id"]))
    for index, hotel in enumerate(hotels, 1):
        hotel["rank"] = index
        hotel["ranking_group"] = group(hotel)
    return hotels
