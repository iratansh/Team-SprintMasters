# Search JSON handoff

The CLI writes one UTF-8 JSON document with `schema_version: "1.0.0"`. Its schema is [hotel-search.schema.json](../schema/hotel-search.schema.json), using JSON Schema draft 2020-12. Unknown fields are allowed for compatible extensions. Consumers must check the version, `mode`, and `status` before using candidates.

Search uses our own direct public-page scraper by default, with an optional explicitly selected SerpApi provider. Google records are enriched by exact property identity; OSM or supplied websites can provide fallback leads. By default the query spans Ontario; a supplied city restricts it to that municipality. It cannot enumerate every available stay in the province. Even a dated online quote is **not confirmed availability**; the phone module must establish availability and the final price for the requested dates and occupancy. This module does not make calls or reservations.

## Run result

| Field | Meaning |
| --- | --- |
| `mode` | `live` uses SerpApi; `fixture` uses synthetic offline input. |
| `status` | `success`, `partial`, `no_results`, or `error`; see below. |
| `request` | Ontario/Canada, CAD, dates, nights, adult `guests`, optional `city` (null for all Ontario), and derived `scope` (`province` or `city`). Can be null on input failure. |
| `hotels` | All normalized candidates retained from the fetched pages, including candidates unsuitable for calls. |
| `shortlist` | Ordered candidate IDs, not hotel objects. Resolve each ID against `hotels[].candidate_id`. |
| `enquiry_shortlist` | Optional ordered contact-ready IDs, including leads without a reliable online total. These cannot establish match-or-lower until an online comparison baseline exists. |
| `coverage` | `geographic_scope`, actual `search_location`, fetched pages, remaining-page indicator, stop reasons, and any candidate limit. `all_market_coverage` is always false. |
| `hotels[].platforms` | Platforms associated with the property, including seller offers obtained indirectly. |
| `hotels[].platform_offers` | Coherent per-platform listing links, amounts, scope and acquisition provenance. |
| `ranking` | Ordering policy and shortlist size; `globally_comparable` is false. |
| `metrics` | Milliseconds per stage, total HTTP attempts, LLM tokens, and estimated API cost. Null cost means unknown, not free. |
| `warnings`, `errors` | Run-level limitations and failures; also inspect each hotel's warnings and reasons. |
| `run_id`, `started_at`, `completed_at` | Run correlation and UTC timestamps. Smaller error documents may omit run ID and start time. |

`success` means candidates were returned without a recorded failure. It does not mean the whole market was searched or that a room is available. `partial` means usable result data survived but the run has failures or incomplete coverage. Inspect coverage even for successful results: `page_limit`, remaining pages, or an applied candidate limit constrain what was searched. `no_results` has empty hotel and shortlist arrays. `error` has empty candidate arrays and one or more error entries.

The workflow can return a full error document after provider failures. The CLI also emits a smaller error document on configuration, input, or fatal provider failure:

```json
{
  "schema_version": "1.0.0",
  "mode": "live",
  "status": "error",
  "completed_at": "2026-10-17T15:30:00+00:00",
  "request": null,
  "hotels": [],
  "shortlist": [],
  "errors": [{"code": "configuration_error", "message": "A SerpApi API key is required."}]
}
```

CLI exit code is 0 for non-error results (including partial and no results), 1 for workflow errors, 2 for caught input/configuration/fatal errors, and 130 for interruption. After interruption, ignore any prior output file. JSON goes to the output file or stdout with `--output -`; diagnostics and optional `--events` JSON lines go to stderr.

## Candidate and price semantics

`candidate_id` is a property identity hash, stable for the same property token, or a normalized name/address fallback. `rank` is its one-based position in the returned array. `property_name`, `address`, `website`, `phone`, and `stay` provide the call context. Missing facts remain null or explicitly unknown.

`phone` is the normalized North American destination, such as `+16135550101`. `phone_extension` is separate: do not append it to the E.164 destination. The dialer must decide whether to enter it using DTMF after connecting. `phone_raw` preserves the source representation. A valid number is not proof that it is reachable or belongs to the property.

`call_ready` means the current checks found a usable contact number, address/locality, full-stay quote, and acceptable quote conditions. `enquiry_ready` checks the contact and location without requiring a full-stay online price. Neither is availability confirmation, a guarantee of room type, or permission to book. Inspect `not_call_ready_reasons`; using enquiry-only leads requires the caller to preserve unknown price-comparison status.

`discovery_source`, `contact_source_url`, `online_price_evidence` and `indicative_prices`
retain direct-search provenance. Indicative prices never populate `online_quote.amount`
unless independent actual stay/occupancy evidence establishes a dated full-stay quote.
`coverage.provider` is `direct_web`, `serpapi_google_hotels`, or `synthetic_fixture`.
Direct result-window limits are recorded as `source_window_limit`; increasing
`--max-pages` does not paginate the direct source. `metrics.cache_hits` is additive.

`coverage.source_details.platform_status` distinguishes native search status and
candidate counts from `supplier_offers` obtained through Google. A native source
may be blocked while indirect offers remain available. Check each
`platform_offers[].discovery_method`, `scope_verified`, `currency`, dates, adults
and price basis before comparing quotes. See [platform coverage](platform-coverage.md).

For `scope: "province"`, every Ontario municipality is eligible, including Toronto.
For `scope: "city"`, the municipality must also match. Out-of-province addresses
have `location_status: "outside_requested_province"` in province mode; uncertain
locations remain `unknown` and are not call-ready. An Ontario-named street in
another province is not evidence of Ontario membership. Region-wide coverage is
bounded by provider indexing and page limits, not an exhaustive municipal sweep.

| `online_quote` field | How to use it |
| --- | --- |
| `amount`, `currency`, `basis` | Selected full-stay amount; `basis` is `stay` when a full-stay numeric quote exists, otherwise `unknown` and amount is null. |
| `tax_fee_status` | `unknown` means the provider's lowest total does not establish inclusion of all mandatory taxes/fees. `excluded` explicitly marks a before-tax fallback. Never say “all-in” for either. |
| `before_taxes_fees_amount` | Separately reported full-stay amount before taxes/fees; it may differ from `amount`. |
| `nightly_amount`, `nightly_before_taxes_fees_amount` | Source nightly values for context; never silently multiply these to invent a full-stay total. |
| `price_unit` | Room-versus-bed unit remains `unknown` unless explicitly established. Ask during the call. |
| `num_guests`, `num_guests_is_capacity`, `occupancy_matches` | Provider guest capacity when supplied, the optional capacity marker, and whether capacity accommodates the requested adults. The dated/adult search context comes from the query; absent capacity is not an independent occupancy confirmation. |
| `currency_matches` | Whether the quote matches requested CAD. |
| `rate_eligibility`, `conditions` | Restrictions, including detected member/app/resident rates. “No restrictions reported” is not proof of unrestricted eligibility. |
| `provider`, `booking_url` | The selected offer's provider and URL. An aggregate quote can have neither a named seller nor a booking URL. |
| `source_url`, `observed_at`, `retrieved_at` | Discovery/evidence URL, provider observation time if available, and retrieval time. Observation time is provider text; retrieval time is ISO 8601. |
| `raw` | Restricted source evidence for diagnosis, not new verified facts. |

`online_offers` retains individual offers. The chosen quote keeps its own price, restrictions, provider, and link together. Do not take the price from one offer and attach another offer's booking URL.

Ranking sorts full-stay amounts **within** currency, tax-status, and unit groups. `ranking_group` makes that grouping explicit, such as `CAD:stay:unknown:unknown`. Unpriced/currency-mismatch candidates go into their own group. Array order across groups is a provisional call policy, not proof that rank 1 is the cheapest comparable final stay. In particular, an excluded-tax price and an unknown-tax price are not comparable all-in totals.

If the orchestrator explicitly chooses a before-tax comparison policy, use `before_taxes_fees_amount` and `nightly_before_taxes_fees_amount` as those explicitly labelled alternatives. Never silently substitute them for `amount` or claim taxes are included. If the needed basis is absent, leave the comparison unknown. A nightly before-tax amount alone still does not establish a full-stay total.

## Live and offline integration

`--demo` and `--fixture` make no network requests. Their results have `mode: "fixture"`, empty priced/enquiry shortlists, `call_ready: false`, `enquiry_ready: false`, and the reason `synthetic_fixture`. `fixture_would_be_call_ready` preserves simulated readiness for integration checks. These invented properties and numbers must never be sent to a real dialer. Live direct search requires no key; optional SerpApi mode requires `SERPAPI_API_KEY`. No API key belongs in the output JSON.

The schema checks shapes and basic bounds, including known statuses and positive prices. Use a validator with format checking enabled for calendar dates and timestamps. The application must also check relational constraints: checkout after check-in, correct nights, matching request/stay, unique candidate IDs, shortlist references, and call eligibility. JSON Schema cannot establish current availability or price truth.

## Mapping a shortlisted candidate to voice context

This mapping only prepares dynamic variables for the caller teammate. It initiates no call. `guest_name` must come from trusted application configuration or the original request; the search file does not contain one. If unavailable, configure a voice prompt that does not require or claim a guest name.

```python
import json

result = json.loads(open("hotels.json", encoding="utf-8").read())
if result["mode"] != "live" or result["status"] not in {"success", "partial"}:
    raise ValueError("No live candidate handoff")
by_id = {hotel["candidate_id"]: hotel for hotel in result["hotels"]}
if not result["shortlist"]:
    raise ValueError("No candidate ready for the caller")
hotel = by_id[result["shortlist"][0]]
if not hotel["call_ready"]:
    raise ValueError("Candidate failed call-readiness checks")

quote = hotel["online_quote"]
listed_price = (
    f"{quote['currency']} {quote['amount']:.2f} for the entire stay; "
    f"taxes/fees {quote['tax_fee_status']}; "
    f"room/bed unit {quote['price_unit']}; online quote, unconfirmed"
)
variables = {
    "hotel_name": hotel["property_name"],
    "check_in": hotel["stay"]["check_in"],
    "check_out": hotel["stay"]["check_out"],
    "nights": hotel["stay"]["nights"],
    "guests": hotel["stay"]["guests"],
    "listed_price": listed_price,
}
# app_config is supplied by your application, never generated from a hotel listing.
guest_name = app_config.get("guest_name")
if isinstance(guest_name, str) and guest_name.strip():
    variables["guest_name"] = guest_name.strip()
conversation_initiation_client_data = {"dynamic_variables": variables}
destination = hotel["phone"]
extension_to_handle_after_connection = hotel["phone_extension"]
```

Configure those exact variable names on the voice agent, including `guests`. The phone module owns call limits, IVR/hold handling, quote verification, transcript capture, and fallback to the next shortlist ID. Keep online quotes distinct from phone-confirmed outcomes and obey the project prohibition on real bookings and payment details.
