# Direct web search

The default provider is our own Python HTTP client and parser. It does not call
SerpApi, use a fixed hotel list, require a model/API key, or automate a browser.
All hotel data comes from fetched public pages or the optional OSM directory.

Auto mode now queries Google, Airbnb, Booking, Priceline, Expedia, Hotels.com
and Agoda concurrently. It also extracts seller offers from Google hotel
detail pages. Native-platform failures and indirect offer provenance are
separate. See [platform coverage](platform-coverage.md) for supported paths,
observed blocking and the JSON handoff fields.

## Search and evidence

1. Encode the requested dates, one-room adult party, CAD preference and
   lowest-price sorting in the public Google Hotels page's `ts` state parameter.
2. Fetch the HTML and parse embedded property records. Check the page's adult
   echo and each priced property's actual dates, adult count and currency.
   Ordinary `checkin`/`adults` URL arguments alone are insufficient: the site
   can ignore them and silently display its defaults.
3. Extract explicit nightly and whole-stay amounts. Never calculate a full-stay
   price by multiplying rounded average nightly labels. Preserve the displayed
   price strings, observed stay context, source URL and retrieval time in
   `online_price_evidence` and the selected quote's raw evidence.
4. Fetch each exact entity's public detail page for its address, phone and site.
   Detail pages enrich contacts; the original priced-page provenance is kept.
5. Where contacts are missing, inspect the property's linked official site and
   accept enrichment only when the property identity matches.
6. Normalize, apply Ontario/city checks, group comparable price bases, sort,
   and write one result document with a priced shortlist and enquiry-only queue.

The state encoding and embedded record layout are **undocumented website
formats**, derived from observed public pages. Parser checks fail closed when
the returned stay or layout cannot be verified. CAPTCHA/session gates and blocked
pages become source errors; the program does not bypass them. It never books,
holds, pays, calls a hotel or sends an email.

Google display totals may be rounded. Tax/fee treatment stays conservatively
unknown in the normalized comparison field, with base totals and exact displayed
labels preserved for the caller. No price is considered phone-confirmed.

## Fallbacks and supplied websites

In `--direct-source auto`, an unavailable Google source can fall back to one
bounded [OpenStreetMap Overpass](https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL)
directory query. It selects accommodations in Ontario's boundary that have a
phone and website. OSM has **no date-specific rates or inventory**, and address
tags may be incomplete or stale. Such results may be contact-ready enquiries
but are not priced shortlist entries. Public Overpass servers can also fail.
OSM data attribution: [OpenStreetMap contributors, ODbL](https://www.openstreetmap.org/copyright).

For known properties, bypass discovery and inspect their sites:

```powershell
python -m hotel_search --check-in 2026-11-06 --check-out 2026-11-08 --guests 2 --direct-source websites --website-url https://property.example --output hotels.json
```

Replace the example URL with an actual property's site. Website extraction uses
[hotel/accommodation structured data](https://schema.org/docs/hotels.html)
and explicit HTML contacts. A structured offer must identify the actual stay
dates, requested adult count, currency and price basis to become a dated quote.
`validFrom`/`validThrough` are offer-validity dates and are not treated as stay
dates. Generic `priceRange`, “from” amounts and unscoped offers remain indicative.

## Coverage, caching and integration

The direct Google source currently reads its first public result window; it
does not implement further-page RPCs. `source_window_limit` and partial status
are deliberate. The returned candidates are ranked locally, without claiming
to have found every hotel or the cheapest in the province. Optional rental
search requests a separate public result window and has the same limitations.

Exact URLs are cached for five minutes in `outputs/web-cache`; date/guest/CAD
state forms part of the Google URL. `--fresh` forces a new fetch. Cached evidence
retains its original fetch timestamp. Metrics report HTTP attempts/cache hits,
zero paid search requests, and zero LLM tokens for the built-in extractor.

Use the ordinary `shortlist` for candidates with a valid online full-stay price.
`enquiry_shortlist` also permits contact-ready leads with unknown rates, but a
phone quote alone cannot prove match-or-lower against an absent online baseline.
The downstream orchestrator must keep comparison unknown in that case.

The SerpApi implementation remains available through explicit `--provider
serpapi`; it is never chosen automatically or used to rescue a direct failure.
