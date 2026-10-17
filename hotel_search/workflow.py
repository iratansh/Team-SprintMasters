"""Bounded online discovery, exact-property enrichment, and a single result document."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import time
import uuid

from .normalize import money, normalize_property, normalized, rank_hotels, safe_url
from .providers import ProviderError


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_echo(payload, request):
    """Reject a quote explicitly returned for different dates, adults or currency."""
    echo = payload.get("search_parameters", {})
    if not isinstance(echo, dict):
        raise ProviderError("Provider returned invalid search parameters.", "invalid_response")
    expected = {"check_in_date": request.check_in, "check_out_date": request.check_out,
                "adults": request.guests, "children": 0, "currency": request.currency}
    for key, value in expected.items():
        if key in echo and str(echo[key]) != str(value):
            raise ProviderError("Provider returned results for different stay parameters.", "query_mismatch")


def valid_property_shape(row):
    if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"].strip():
        return False
    for key in ("property_token", "address", "link", "phone", "phone_link"):
        if key in row and row[key] is not None and not isinstance(row[key], str):
            return False
    return True


def search_hotels(request, provider, *, max_pages=1, limit=0, workers=5,
                  include_vacation_rentals=False, shortlist_size=5, event=None, mode="live"):
    if not 1 <= max_pages <= 10 or not 1 <= workers <= 10 or not 0 <= limit <= 500:
        raise ValueError("invalid search bounds")
    if not 1 <= shortlist_size <= 50:
        raise ValueError("shortlist_size must be from 1 to 50")
    started = time.monotonic()
    started_at = utc_now()
    run_id = str(uuid.uuid4())
    errors = []
    warnings = [
        "Online listings are not confirmed availability; every selected stay must be verified by phone.",
        "Ranks are provisional within price-basis groups; taxes, room/bed unit, and conditions need confirmation.",
        "Search covers fetched provider results only, not every accommodation in the requested area.",
    ]
    if mode == "fixture":
        warnings.insert(0, "SYNTHETIC OFFLINE FIXTURE: not live hotel data; do not call these numbers.")

    def emit(step, status, **data):
        if event:
            event({"run_id": run_id, "step": step, "status": status,
                   "elapsed_ms": round((time.monotonic() - started) * 1000),
                   "tokens": 0, "data": data})

    emit("search", "started", city=request.city, scope=request.scope, search_location=request.search_location)
    rows = {}
    coverage_modes = []
    search_start = time.monotonic()
    for rentals in ([False, True] if include_vacation_rentals else [False]):
        coverage = {"mode": "vacation_rentals" if rentals else "hotels", "pages_fetched": 0,
                    "has_more_pages": False, "stop_reason": "provider_exhausted"}
        seen_tokens = set()
        next_token = None
        for page_index in range(max_pages):
            try:
                payload = provider.search(request.to_dict(), next_page_token=next_token, vacation_rentals=rentals)
                validate_echo(payload, request)
                properties = payload.get("properties")
                if properties is None and payload.get("name"):
                    properties = [payload]
                if not isinstance(properties, list):
                    raise ProviderError("Provider response is missing its properties list.", "invalid_response")
            except ProviderError as exc:
                if exc.code == "no_results":
                    coverage["stop_reason"] = "no_results"
                    coverage["has_more_pages"] = False
                    break
                if exc.code in ("authentication_error", "configuration_error", "query_mismatch"):
                    raise
                errors.append({"step": "search", "code": exc.code, "message": str(exc)})
                coverage["stop_reason"] = "provider_error"
                break
            coverage["pages_fetched"] += 1
            source_window = payload.get("_coverage_warning")
            if isinstance(source_window, str) and source_window:
                warnings.append(source_window)
                coverage["source_result_window"] = source_window
            source_url = safe_url(payload.get("_source_url"))
            fetched_at = payload.get("_fetched_at") or utc_now()
            observed_at = payload.get("_provider_observed_at")
            for row in properties:
                if not valid_property_shape(row):
                    errors.append({"step": "normalize", "code": "invalid_property", "message": "Skipped a property with invalid identity or contact fields."})
                    continue
                token = row.get("property_token")
                identity = token or "|".join((normalized(row["name"]), normalized(row.get("address")), row.get("link") or ""))
                if identity not in rows:
                    rows[identity] = (row, source_url, observed_at, fetched_at, rentals)
            pagination = payload.get("serpapi_pagination", {})
            if not isinstance(pagination, dict):
                pagination = {}
            next_token = pagination.get("next_page_token")
            if next_token is not None and not isinstance(next_token, str):
                errors.append({"step": "search", "code": "invalid_pagination", "message": "Provider returned an invalid page token."})
                coverage["stop_reason"] = "invalid_pagination"
                coverage["has_more_pages"] = True
                break
            coverage["has_more_pages"] = bool(next_token)
            emit("search", "page_received", mode=coverage["mode"], page=page_index + 1, candidates=len(rows))
            if not next_token:
                if source_window:
                    coverage["stop_reason"] = "source_window_limit"
                break
            if next_token in seen_tokens:
                coverage["stop_reason"] = "repeated_page_token"
                warnings.append("Pagination repeated a token; stopped rather than loop indefinitely.")
                break
            seen_tokens.add(next_token)
            if page_index + 1 == max_pages:
                coverage["stop_reason"] = "page_limit"
        coverage_modes.append(coverage)
    search_ms = round((time.monotonic() - search_start) * 1000)
    candidates_seen = len(rows)
    records = list(rows.values())
    if limit:
        def listing_amount(record):
            rates = record[0].get("total_rate", {})
            if not isinstance(rates, dict):
                return float("inf")
            return money(rates.get("extracted_lowest")) or money(rates.get("extracted_before_taxes_fees")) or float("inf")
        records.sort(key=lambda record: (listing_amount(record), normalized(record[0]["name"])))
        records = records[:limit]
    emit("search", "completed", candidates_seen=candidates_seen, candidates_to_enrich=len(records))
    detail_start = time.monotonic()
    details = {}
    detail_errors = {}
    emit("details", "started", count=len(records))

    def fetch_detail(index, record):
        row, _, _, _, rentals = record
        token = row.get("property_token")
        if not isinstance(token, str) or not token:
            return index, None, "missing_property_token"
        try:
            result = provider.details(request.to_dict(), token, vacation_rentals=rentals)
            if not valid_property_shape(result):
                raise ProviderError("Property details have invalid identity or contact fields.", "invalid_property")
            validate_echo(result, request)
            return index, result, None
        except ProviderError as exc:
            return index, None, exc.code

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_detail, i, record) for i, record in enumerate(records)]
        for future in as_completed(futures):
            index, detail, error = future.result()
            if detail:
                details[index] = detail
            if error:
                detail_errors[index] = error
                errors.append({"step": "details", "code": error, "property_name": records[index][0]["name"]})
            emit("details", "property_received" if not error else "property_failed",
                 property_name=records[index][0]["name"], completed=len(details) + len(detail_errors), total=len(records))
    details_ms = round((time.monotonic() - detail_start) * 1000)
    hotels = []
    for index, record in enumerate(records):
        row, source_url, observed_at, fetched_at, rentals = record
        hotel = normalize_property(row, details.get(index), request, source_url=source_url,
                                   observed_at=observed_at, fetched_at=fetched_at,
                                   detail_error=detail_errors.get(index), vacation_rentals=rentals)
        if hotel:
            hotels.append(hotel)
    rank_hotels(hotels)
    if any(h["warnings"] for h in hotels):
        warnings.append("Some property details could not be trusted or retrieved; see per-property warnings.")
    shortlist = [h["candidate_id"] for h in hotels if h["call_ready"]][:shortlist_size]
    enquiry_shortlist = [h["candidate_id"] for h in hotels if h["enquiry_ready"]][:shortlist_size]
    if mode == "fixture":
        # Preserve the simulated readiness for tests, but never hand demo phones to the dialer.
        for hotel in hotels:
            hotel["fixture_would_be_call_ready"] = hotel["call_ready"]
            hotel["call_ready"] = False
            hotel["enquiry_ready"] = False
            hotel["not_call_ready_reasons"].append("synthetic_fixture")
        shortlist = []
        enquiry_shortlist = []
    errors.extend(getattr(provider, "diagnostics", []))
    warnings.extend(getattr(provider, "warnings", []))
    incomplete = len(records) < candidates_seen or any(
        item["has_more_pages"] or item["stop_reason"] in {"provider_error", "invalid_pagination", "source_window_limit"}
        for item in coverage_modes
    )
    if incomplete:
        warnings.append("Search stopped before every provider page/candidate was processed; coverage is partial.")
    status = "partial" if hotels and (errors or incomplete) else "error" if errors else "success" if hotels else "no_results"
    result = {
        "schema_version": "1.0.0", "run_id": run_id, "mode": mode, "status": status,
        "started_at": started_at, "completed_at": utc_now(), "request": request.to_dict(),
        "coverage": {"provider": getattr(provider, "name", "serpapi_google_hotels") if mode == "live" else "synthetic_fixture",
                     "geographic_scope": request.scope, "search_location": request.search_location,
                     "max_pages_per_mode": max_pages, "modes": coverage_modes,
                     "candidate_limit": limit or None, "candidates_seen": candidates_seen,
                     "candidates_returned": len(hotels), "candidate_limit_applied": len(records) < candidates_seen,
                     "all_market_coverage": False,
                     "source_details": getattr(provider, "coverage_summary", {})},
        "ranking": {"method": "full_stay_amount_ascending_within_currency_tax_and_unit_groups",
                    "globally_comparable": False, "shortlist_size": shortlist_size},
        "hotels": hotels, "shortlist": shortlist, "enquiry_shortlist": enquiry_shortlist,
        "metrics": {"elapsed_ms": round((time.monotonic() - started) * 1000),
                    "search_ms": search_ms, "details_ms": details_ms,
                    "http_requests": provider.request_count, "llm_tokens": 0,
                    "cache_hits": getattr(provider, "cache_hits", 0),
                    "estimated_api_cost": provider.estimated_cost},
        "warnings": warnings, "errors": errors,
    }
    emit("result", "completed", result_status=status, hotels=len(hotels), call_ready=len(shortlist))
    return result
