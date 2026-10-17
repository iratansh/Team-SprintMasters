"""Concurrent public-platform discovery and conservative property deduplication."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy

from .normalize import clean_evidence, money, normalized, safe_url
from .providers import ProviderError


PLATFORMS = ("google", "airbnb", "booking", "priceline", "expedia", "hotels", "agoda")


def platform_names(value):
    if isinstance(value, str) and value.strip().lower() == "all":
        return list(PLATFORMS)
    names = value.split(",") if isinstance(value, str) else list(value)
    names = list(dict.fromkeys(name.strip().lower() for name in names if isinstance(name, str) and name.strip()))
    if not names or any(name not in PLATFORMS for name in names):
        raise ValueError("platforms must be a comma-separated selection of " + ", ".join(PLATFORMS))
    return names


def _offer(row, platform, request):
    if isinstance(row.get("_platform_offer"), dict):
        offer = clean_evidence(row["_platform_offer"])
        offer.setdefault("discovery_method", "direct_platform")
        offer.setdefault("provider", {"airbnb": "Airbnb", "booking": "Booking.com", "priceline": "Priceline", "hotels": "Hotels.com", "expedia": "Expedia", "agoda": "Agoda"}.get(platform, platform))
        offer.setdefault("source_url", safe_url(row.get("_source_url")))
        if not offer.get("basis"):
            offer["basis"] = "unknown"
        return offer
    rates = row.get("total_rate", {})
    rates = rates if isinstance(rates, dict) else {}
    amount = money(rates.get("extracted_lowest"))
    return {
        "platform": platform, "provider": platform, "listing_id": row.get("_listing_id"),
        "listing_url": safe_url(row.get("link")) if platform != "google" else None,
        "source_url": safe_url(row.get("_source_url")),
        "discovery_method": "google_hotels_aggregate" if platform == "google" else "direct_platform",
        "amount": amount, "currency": row.get("currency", request["currency"]),
        "basis": "stay" if amount is not None else "unknown", "tax_fee_status": "unknown",
        "check_in": request["check_in"] if amount is not None and platform == "google" else None,
        "check_out": request["check_out"] if amount is not None and platform == "google" else None,
        "guests": request["guests"] if amount is not None and platform == "google" else None,
        "scope_verified": bool(amount is not None and platform == "google"),
        "raw": clean_evidence(row.get("_price_evidence", {})),
    }


def _identity(row):
    # Cross-platform names and approximate rental coordinates alone cannot
    # distinguish branches, adjacent units, or private accommodation listings.
    address = normalized(row.get("address"))
    if address and any(char.isdigit() for char in address) and normalized(row.get("name")):
        return ("property", normalized(row["name"]), address)
    return ("listing", row.get("property_token"))


def _matching_offer(offer, request):
    return (offer.get("scope_verified") is True and offer.get("check_in") == request["check_in"]
            and offer.get("check_out") == request["check_out"] and offer.get("guests") == request["guests"]
            and type(offer.get("guests")) is int and offer.get("currency") == request["currency"])


def merge_properties(rows, request):
    """Deduplicate exact property/address pairs while retaining every source offer."""
    merged = {}
    aliases = {}
    # Keep Google identity for its exact-property contact-detail lookup.
    rows = [row for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)
            and isinstance(row.get("property_token"), str)]
    rows = sorted(rows, key=lambda row: (row.get("_platform") != "google", str(row.get("property_token", ""))))
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("property_token"), str):
            continue
        identity = _identity(row)
        token_key = ("token", row["property_token"])
        key = aliases.get(token_key, identity)
        platform = row.get("_platform", "google")
        offer = _offer(row, platform, request)
        if platform in PLATFORMS and platform != "google" and not _matching_offer(offer, request):
            row = deepcopy(row)
            if row.get("total_rate") or row.get("rate_per_night"):
                row.setdefault("_indicative_price_evidence", []).append({"reason": "unverified_platform_stay_context",
                    "source_url": row.get("_source_url"), "total_rate": row.get("total_rate"), "rate_per_night": row.get("rate_per_night")})
            row.pop("total_rate", None)
            row.pop("rate_per_night", None)
            offer = {**offer, "scope_verified": False, "amount": None, "basis": "unknown"}
        if key not in merged:
            merged[key] = deepcopy(row)
            merged[key]["_platform_offers"] = [offer]
            merged[key]["_platforms"] = [platform]
            aliases[token_key] = key
            continue
        target = merged[key]
        target["_platform_offers"].append(offer)
        if platform not in target["_platforms"]:
            target["_platforms"].append(platform)
        for field in ("phone", "phone_link", "address"):
            if not target.get(field) and row.get(field):
                target[field] = row[field]
        old = target.get("total_rate", {})
        new = row.get("total_rate", {})
        old_amount = money(old.get("extracted_lowest")) if isinstance(old, dict) else None
        new_amount = money(new.get("extracted_lowest")) if isinstance(new, dict) else None
        # Only independently verified, same-currency full-stay offers can replace
        # the quote; its original source and evidence move with it.
        if (_matching_offer(offer, request)
                and offer.get("basis") == "stay" and new_amount is not None
                and money(offer.get("amount")) == new_amount
                and (old_amount is None or new_amount < old_amount)):
            for field in ("total_rate", "rate_per_night", "_price_evidence", "_price_scope"):
                target.pop(field, None)
            for field in ("total_rate", "rate_per_night", "currency", "_source_url", "_fetched_at",
                          "_provider_observed_at", "_price_evidence", "_price_scope"):
                if field in row:
                    target[field] = deepcopy(row[field])
            target["_selected_platform_offer"] = deepcopy(offer)
        aliases[token_key] = key
    return list(merged.values())


def search_platforms(provider, request, *, next_page_token=None, vacation_rentals=False):
    if next_page_token or vacation_rentals:
        # Native platforms are searched once per run. Optional Google rental
        # mode must not repeat seven independent searches or all seed websites.
        if "google" in provider.platforms:
            return provider._search_single_source(request, next_page_token=next_page_token, vacation_rentals=vacation_rentals)
        return {"properties": []}
    selected = provider.platforms
    statuses = {}
    collected = []
    source_url = None

    def run(platform):
        if platform == "google":
            return provider._search_single_source(request)
        if platform == "airbnb":
            from .airbnb_source import AirbnbSource
            return AirbnbSource(client=provider.client).search(request)
        from .ota_sources import OtaSource
        return OtaSource(platform, client=provider.client, timeout=provider.timeout).search(request)

    with ThreadPoolExecutor(max_workers=min(4, len(selected))) as pool:
        futures = {pool.submit(run, platform): platform for platform in selected}
        for future in as_completed(futures):
            platform = futures[future]
            try:
                result = future.result()
                if not isinstance(result, dict) or not isinstance(result.get("properties"), list):
                    raise ProviderError("Platform returned an invalid result shape.", "invalid_response")
                rows = result["properties"]
                direct_count = 0
                for row in rows:
                    if isinstance(row, dict):
                        origin = provider._origins.get(row.get("property_token"))
                        if platform == "google" and origin in {"osm", "website"}:
                            row["_platform"] = "openstreetmap" if origin == "osm" else "official_website"
                        else:
                            row["_platform"] = platform
                            provider._origins[row.get("property_token")] = "google" if platform == "google" else platform
                            direct_count += 1
                        collected.append(row)
                source_url = source_url or safe_url(result.get("_source_url"))
                statuses[platform] = {"platform": platform, "direct_search_status": "success" if direct_count else "fallback" if rows else "no_results",
                                      "direct_candidates": direct_count, "supplier_offers": 0,
                                      "search_url": safe_url(result.get("_source_url")),
                                      "discovery_methods": ["direct_platform"] if platform != "google" else ["google_hotels_direct"]}
            except ProviderError as exc:
                provider._record_error(platform, exc)
                status = "blocked" if exc.code in {"source_blocked", "website_blocked", "rate_limit"} else "unavailable"
                statuses[platform] = {"platform": platform, "direct_search_status": status,
                                      "direct_candidates": 0, "supplier_offers": 0,
                                      "error_code": exc.code, "message": str(exc), "discovery_methods": []}
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, ImportError):
                exc = ProviderError("Platform page could not be parsed reliably.", "parser_error")
                provider._record_error(platform, exc)
                statuses[platform] = {"platform": platform, "direct_search_status": "unavailable",
                                      "direct_candidates": 0, "supplier_offers": 0,
                                      "error_code": exc.code, "message": str(exc), "discovery_methods": []}
    if "google" not in selected:
        for url in provider.seed_urls:
            try:
                row = provider._website(url, request)
                if row:
                    row["_platform"] = "official_website"
                    collected.append(row)
                    provider._origins[row["property_token"]] = "website"
            except ProviderError as exc:
                provider._record_error("official_website", exc)
    properties = merge_properties(collected, request)
    for row in properties:
        provider._records[row["property_token"]] = deepcopy(row)
    provider.coverage_summary["platform_status"] = [statuses[name] for name in selected]
    provider.coverage_summary["requested_platforms"] = list(selected)
    provider.coverage_summary["sources_used"] = list(dict.fromkeys(
        list(provider.coverage_summary["sources_used"]) + [name for name in selected if statuses[name]["direct_candidates"]]
    ))
    return {"properties": properties, "_source_url": source_url,
            "_coverage_warning": "Bounded public result windows across requested platforms; inspect direct-search and supplier-offer statuses. Not exhaustive."}
