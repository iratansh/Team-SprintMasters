# Team SprintMasters — TechTO Hackathon

The CLI searches **across Ontario by default**, using our own HTTP scrapers for
Google Hotels, Airbnb, Booking.com, Priceline, Expedia, Hotels.com and Agoda.
It writes **one JSON file** for the team's phone/orchestration module and
retrieves address/phone details by exact property identity. **No SerpApi key
or paid search quota is required.**

Python **3.11+** is required. There are **no third-party runtime dependencies**,
model calls, browser automation, phone calls or email sends in this module.
Project rules: [agent spec](agent-spec.md), including the team's Ontario-wide scope correction.

## Run a live search

From the repository root, run the following command. No setup, API key or
`.env` file is needed for the default direct provider.

```powershell
python -m hotel_search --province-wide --check-in 2026-11-06 --check-out 2026-11-08 --guests 1 --output hotels.json
```

Omit `--city` to search `Ontario, Canada`; `--province-wide` makes that default
explicit. To restrict a trip to one municipality, add `--city "Kingston"`
instead of `--province-wide`. A JSON request can omit `city` or set it to null.
The output states `request.scope` as `province` or `city` and includes the
actual search location in `coverage.search_location`.
Dates are explicit ISO calendar dates; there are no
relative-date guesses. `--guests` defaults to **one adult**. This version
supports a single accommodation search for adults, with no children or
multi-room allocation. The upstream prompt parser must reject or explicitly
handle unsupported requirements instead of silently dropping them.

Default direct mode queries all seven platforms concurrently. It also preserves
seller-specific offers published on Google Hotels, with Booking/Priceline/etc.
links, prices and explicit `google_hotels_supplier` provenance. A blocked native
search stays marked blocked even when that platform has indirectly discovered offers.
See [platform coverage](docs/platform-coverage.md) for exact live limitations.

The default keeps **all unique properties in the fetched result windows** and
fetches details for each, with up to five requests running concurrently.
To read trip input from a JSON file:

```powershell
python -m hotel_search --request examples/request.json --output hotels.json --events
```

The sample dates are examples, not competition defaults. Use the dates and
occupancy from the actual prompt.

Important options:

| Option | Meaning |
|---|---|
| `--provider direct` | Default: our own public-page scraper, with no search API key. |
| `--provider serpapi` | Optional legacy API integration; requires `SERPAPI_API_KEY`. Never selected automatically. |
| `--direct-source auto` | Search the selected platforms concurrently; Google can fall back to OSM directory leads. |
| `--platforms all` | All seven direct sources (default); or select e.g. `google,airbnb,booking,priceline`. Applies to auto mode. |
| `--direct-source google` | Require direct Google Hotels; report failure rather than fall back. |
| `--direct-source osm` | OpenStreetMap directory leads only; it has no live rates. |
| `--direct-source websites --website-url https://...` | Inspect supplied property websites; repeat `--website-url` for several. |
| `--cache-dir outputs/web-cache` | Cache exact public URLs for five minutes; date/guest changes produce different Google URLs. |
| `--province-wide` | Search Ontario without a municipality restriction; this is the default when city is omitted. |
| `--city Kingston` | Optional restriction to one municipality. Cannot combine with `--province-wide`. |
| `--request path.json` | Structured trip input; use `-` for stdin. Cannot mix with trip flags. |
| `--output path.json` | Atomic UTF-8 JSON output; default `hotels.json`. Use `-` for JSON stdout. |
| `--max-pages 3` | API-provider pagination bound. Direct Google currently reads one public result window and reports this limit. |
| `--limit 5` | Keep only five cheapest headline-price candidates before details. Default zero keeps all fetched. This can omit usable fallback properties. |
| `--shortlist-size 5` | Number of call-ready candidate IDs in `shortlist`; does not discard other results. |
| `--workers 5` | Parallel detail requests, maximum ten. |
| `--timeout 20 --retries 1` | Per-request timeout and bounded retries for transient failures. |
| `--fresh` | Bypass local direct-page cache, or SerpApi's cache when explicitly selected. |
| `--events` | Machine-readable progress events on **stderr**, leaving stdout/file as pure JSON. |
| `--env-file path` | Alternate optional SerpApi key file; direct mode does not read it. |

Direct mode makes bounded public search-page requests plus detail-page requests
for retained candidates, with bounded retries. `--limit` reduces details work.
It uses no paid search API or model calls; paid search requests and LLM tokens
are zero. HTTP attempts and cache hits are recorded. Public websites can still
rate-limit or change layouts; failures are reported without bypassing checks.

The direct parser verifies the **actual dates, adult count and currency returned
by the page** before accepting a price. It reads explicit whole-stay display
totals instead of multiplying rounded nightly prices. Returned monetary values
may themselves be rounded by the website; exact prices still need phone verification.
See [how direct search works](docs/direct-search.md) for evidence and limitations.

For the optional SerpApi provider only, create `.env` from [.env.example](.env.example)
and set `SERPAPI_API_KEY`. Environment variables take precedence. An ordinary
run never falls back to this paid provider, even if that key exists.

## Test the handoff without a key

```powershell
python -m hotel_search --demo --output outputs/demo-hotels.json --events
```

This uses **invented properties and rates**, makes no network requests, and
sets `mode: "fixture"`. Demo candidates are deliberately not call-ready and
`shortlist` is empty. `fixture_would_be_call_ready` shows simulated eligibility
for integration debugging. Never feed fixture numbers to telephony.

You can supply another explicitly marked synthetic fixture via `--fixture`.
The fixture's scope/dates/guests must match the request. The included Ontario-wide fixture
demonstrates candidates in Ottawa, Kingston and Thunder Bay: a cheaper motel,
another hotel, and a nightly-only hostel whose
missing full-stay price remains unknown.

## Consume the result

```python
import json

with open("hotels.json", encoding="utf-8") as file:
    result = json.load(file)

if result["mode"] != "live" or result["status"] not in {"success", "partial"}:
    raise RuntimeError("No usable live search result")

by_id = {hotel["candidate_id"]: hotel for hotel in result["hotels"]}
call_queue = [by_id[candidate_id] for candidate_id in result["shortlist"]]
```

`hotels` includes all retained candidates, including incomplete or out-of-scope
properties with explicit exclusion reasons. `shortlist` contains up to five
IDs with a dated full-stay price, suitable contact details, matching locality
and no reported eligibility/capacity mismatch. In province mode, any Ontario
municipality (including Toronto) is accepted; another province or an unverified
location is excluded from the calling queue. In city mode, the specified
municipality must also match. **Call-ready means ready for
an enquiry, not confirmed availability or a comparable verified quote.**

`enquiry_shortlist` is a separate contact-ready queue that can include properties
without a reliable online total. Those have `enquiry_ready: true` but may have
`call_ready: false`. Use them only to collect a quote; a missing online baseline
cannot establish the competition's match-or-lower result. Static site “from”
prices are kept in `indicative_prices` and never treated as date-specific rates.

Online `lowest` prices do not establish whether every tax or fee is included.
The output keeps explicit pre-tax alternatives, raw price evidence, provider
and booking links, requested stay, observed provider timestamps (when given),
and local retrieval times. Nightly-only prices are not multiplied into a
made-up full-stay total. Member-only rates are excluded from the calling
shortlist unless another unrestricted reported offer exists.

The queue is ordered by full-stay amount **within currency/tax/unit groups**.
Different groups are not claimed to be comparable. The phone module must
confirm the room/bed basis, occupancy, tax treatment and conditions before
declaring match-or-lower under the [agent spec](agent-spec.md). Unknown taxes
are never zero. A documented pre-tax total may be used only if the team
permits a like-for-like pre-tax comparison.

Coverage is bounded and source-specific. A returned rate is an online
lead requiring phone confirmation; this module cannot enumerate every
available stay or prove the cheapest price in the entire province. Province mode
uses a region-wide Google Hotels query with lowest-price sorting and sorts the returned candidates locally;
the website's result window is not guaranteed to contain the cheapest in Ontario.
Direct mode reports `source_window_limit` and partial coverage. It does not enumerate
every municipality or implement undocumented pagination. API mode can fetch more
provider pages via `--max-pages`. Out-of-province results are retained for visibility
but excluded from the calling queue.

The [handoff contract](docs/search-output.md) describes all fields and maps
them to ElevenLabs variables. The machine-readable contract is
[schema/hotel-search.schema.json](schema/hotel-search.schema.json).

## Status and failure handling

| Status | Meaning |
|---|---|
| `success` | Search completed without recorded provider/data errors; shortlist can still be empty. |
| `partial` | Candidates survived with errors, unprocessed provider pages, or an applied candidate limit. Inspect errors and coverage. |
| `no_results` | Provider reported no properties for the fetched search; not proof Ontario or the requested city is sold out. |
| `error` | Configuration, input, provider or output failure; do not dial. |

Exit code **0** means a result document was produced (`success`, `partial`, or
`no_results`); **1** means the workflow could not produce usable property
results after provider/data errors; **2** means invalid input/configuration or
a fatal provider/I/O error; **130** means interruption. Check both exit status
and result fields. Failure results overwrite a previous output file when
possible; on output-write failure or interruption, ignore any prior file.
Argparse usage errors do not produce a result file.

## Validation

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Tests use mocked provider responses and explicit synthetic fixtures. They
exercise date validation, price provenance, missing/conditional rates,
property identity, contact normalization, pagination, partial failures,
HTTP retries, secret redaction, output replacement, and the CLI handoff.
The direct source is also checked with bounded live search runs; these cannot
guarantee future page layouts, availability or rates.

Optional API references: [SerpApi Google Hotels search](https://serpapi.com/google-hotels-api)
and [exact-property details](https://serpapi.com/google-hotels-property-details).
