# Platform coverage

Direct mode searches seven public sources by default: `google`, `airbnb`,
`booking`, `priceline`, `expedia`, `hotels`, and `agoda`. The searches run
concurrently and produce one result file. They use no paid search API.

```powershell
python -m hotel_search --check-in 2026-11-06 --check-out 2026-11-08 --guests 1 --platforms all --output hotels.json
```

To select sources, use `--platforms google,airbnb,booking,priceline`.
`--direct-source google` retains the single-source path for fast debugging;
the platform list applies only to `--direct-source auto`, the default.

## What is actually obtained

There are two separate acquisition methods:

- **Native platform search**: fetch a platform's own public search page. Google
  and Airbnb currently return usable embedded inventory. Booking cards and
  published OTA JSON-LD are parsed when exposed. A challenge, HTTP block, or
  JavaScript-only shell produces an explicit source status rather than zero
  availability or fabricated rows.
- **Google supplier offers**: read seller-specific offers on an exact property's
  public Google Hotels detail page. This yields Booking.com, Priceline, Expedia,
  Hotels.com, Agoda, Hostelworld and other supplier links/quotes where published.
  These are labeled `discovery_method: "google_hotels_supplier"`; they do not
  imply the supplier's native search succeeded.

Live verification on October 17, 2026 used Ontario, November 6–8, one adult:
36 priced properties were retained: 18 Google and 18 Airbnb. Seller links/offers
were also found across Booking, Priceline, Expedia, Hotels.com, Agoda,
Hostelworld and other platforms. Fourteen properties had usable public contacts.
These are a smoke test, not a promise of current availability or future coverage.

The native Booking page returned a challenge; Priceline and Agoda exposed no
readable server-side listings; Expedia and Hotels.com blocked public requests.
The program records those failures while retaining successful sources and
indirect seller offers. It does not bypass access checks or use private APIs.

Airbnb search exposes listing IDs, public URLs, approximate map locations and
stay-scoped prices. Its search data in these tests did not publish exact street
addresses or host phones. Those candidates remain in `hotels` but are not
call-ready. The program does not obtain contact information by booking.
See Airbnb's [contact guidance](https://www.airbnb.com/help/article/147) and
[reservation details](https://www.airbnb.com/help/article/4116).

## Output and verification

Each property has `platforms` and `platform_offers`. An offer records its source
URL, listing/booking URL, platform, amount, currency, basis, actual dates/adults,
and `scope_verified`. Invalid/missing scope leaves amounts unknown. A nightly
quote remains `basis: "night"`; it is not multiplied into a full-stay total.

`coverage.source_details.platform_status` records for each platform:

| Field | Meaning |
|---|---|
| `direct_search_status` | `success`, `no_results`, `blocked`, `unavailable`, `fallback`, or `not_requested`. |
| `direct_candidates` | Native search rows, distinct from indirect offers. |
| `supplier_offers` | Seller offer records found through Google; includes unpriced links when scope is uncertain. |
| `discovery_methods` | The acquisition methods actually used. |
| `error_code`, `message` | Source failure details when applicable. |

Supplier-only platforms can appear with `not_requested` because they were
discovered incidentally rather than queried directly. Counts are offer records,
not unique physical hotels. The existing `shortlist` remains a property-level
calling queue; missing contact or location evidence blocks calling.

Deduplication uses exact listing IDs or matching property names and full
addresses, not names/approximate coordinates alone. A cheaper verified native
quote keeps its seller, booking URL and price source together when merged
with Google's property identity for contact lookup.

All sources have bounded result windows. The program cannot enumerate most or
all properties on every platform, and a failed native search is not evidence
that no stays exist there. Inspect partial status and per-source coverage.
