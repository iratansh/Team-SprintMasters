"""Run with python -m hotel_search; diagnostics and events go to stderr."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

from .fixture import FixtureProvider
from .direct import DirectWebProvider
from .models import SearchRequest
from .providers import ProviderError, SerpApiProvider
from .workflow import search_hotels, utc_now


def load_env(path):
    """Read literal KEY=value lines without executing or expanding shell content."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        key = key.strip()
        if key != "SERPAPI_API_KEY" or not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def read_json(path):
    if path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_result(result, destination):
    document = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if destination == "-":
        sys.stdout.write(document)
        return
    path = Path(destination).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(document)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def parser():
    result = argparse.ArgumentParser(description="Search Ontario stays and write sourced hotel candidates as JSON.")
    scope = result.add_mutually_exclusive_group()
    scope.add_argument("--city", help="optional municipality filter; omit to search all Ontario")
    scope.add_argument("--province-wide", action="store_true", help="explicit Ontario-wide search (the default)")
    result.add_argument("--check-in", help="YYYY-MM-DD")
    result.add_argument("--check-out", help="YYYY-MM-DD, departure day")
    result.add_argument("--guests", type=int, help="adult guests; defaults to 1")
    result.add_argument("--request", help="parsed trip JSON file, or - for stdin")
    result.add_argument("--output", "-o", default="hotels.json", help="output file, or - for JSON stdout")
    result.add_argument("--env-file", default=".env", help="local key file, default .env")
    result.add_argument("--provider", choices=("direct", "serpapi"), default="direct", help="direct web search (default), or opt-in SerpApi")
    result.add_argument("--direct-source", choices=("auto", "google", "osm", "websites"), default="auto", help="direct discovery source; auto tries Google Hotels then OSM")
    result.add_argument("--platforms", default="google,airbnb,booking,priceline,expedia,hotels,agoda", help="comma-separated direct platforms in auto mode; default searches all seven")
    result.add_argument("--website-url", action="append", default=[], help="optional property website to inspect; repeat for multiple properties")
    result.add_argument("--cache-dir", default="outputs/web-cache", help="direct HTTP cache directory; --fresh bypasses cache")
    result.add_argument("--max-pages", type=int, default=1, help="pages per search mode, 1..10 (default 1)")
    result.add_argument("--limit", type=int, default=0, help="cap candidates before details; 0 keeps all fetched (default)")
    result.add_argument("--shortlist-size", type=int, default=5)
    result.add_argument("--workers", type=int, default=5, help="parallel detail requests, 1..10")
    result.add_argument("--timeout", type=float, default=20, help="per-HTTP-request timeout seconds, 1..120")
    result.add_argument("--retries", type=int, default=1, help="HTTP retries, 0..2")
    result.add_argument("--fresh", action="store_true", help="bypass the provider's cache")
    result.add_argument("--include-vacation-rentals", action="store_true", help="also run the separate rental search")
    fixture = result.add_mutually_exclusive_group()
    fixture.add_argument("--demo", action="store_true", help="use the bundled synthetic fixture, no API calls")
    fixture.add_argument("--fixture", help="explicit synthetic fixture JSON file, no API calls")
    result.add_argument("--events", action="store_true", help="emit structured progress events as JSON lines on stderr")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    request = None
    try:
        if not (1 <= args.max_pages <= 10 and 0 <= args.limit <= 500 and 1 <= args.workers <= 10
                and 1 <= args.shortlist_size <= 50 and 1 <= args.timeout <= 120 and 0 <= args.retries <= 2):
            raise ValueError("invalid limits: check --help for supported ranges")
        fixture = None
        if args.demo or args.fixture:
            fixture_path = args.fixture or str(Path(__file__).resolve().parent.parent / "examples" / "search.fixture.json")
            fixture = read_json(fixture_path)
        explicit = args.province_wide or any(value is not None for value in (args.city, args.check_in, args.check_out, args.guests))
        if args.request and explicit:
            raise ValueError("use --request or trip flags, not both")
        if args.request:
            request = SearchRequest.from_dict(read_json(args.request))
        elif fixture and (not explicit or args.province_wide and not any(
                value is not None for value in (args.check_in, args.check_out, args.guests))):
            request = SearchRequest.from_dict(fixture["request"])
            if args.province_wide and request.city is not None:
                raise ValueError("--province-wide requires an Ontario-wide fixture")
        else:
            request = SearchRequest(args.city, args.check_in, args.check_out, args.guests if args.guests is not None else 1)
        if fixture:
            provider = FixtureProvider(fixture, request)
        elif args.provider == "direct":
            provider = DirectWebProvider(timeout=args.timeout, retries=args.retries, fresh=args.fresh,
                                         cache_dir=args.cache_dir, source=args.direct_source,
                                         seed_urls=args.website_url, max_properties=args.limit or 60,
                                         platforms=args.platforms if args.direct_source == "auto" else None)
        else:
            load_env(Path(args.env_file))
            key = os.environ.get("SERPAPI_API_KEY", "")
            if not key.strip():
                raise ProviderError("Set SERPAPI_API_KEY in .env or your environment; see README.md. Use --demo for offline integration.", "configuration_error")
            provider = SerpApiProvider(key, timeout=args.timeout, retries=args.retries, fresh=args.fresh)

        def event(item):
            if args.events:
                print(json.dumps(item, ensure_ascii=True, allow_nan=False), file=sys.stderr, flush=True)

        result = search_hotels(request, provider, max_pages=args.max_pages, limit=args.limit, workers=args.workers,
                               include_vacation_rentals=args.include_vacation_rentals,
                               shortlist_size=args.shortlist_size, event=event,
                               mode="fixture" if fixture else "live")
        write_result(result, args.output)
        if not args.events:
            print(f'{result["status"]}: {len(result["hotels"])} properties, {len(result["shortlist"])} priced and {len(result.get("enquiry_shortlist", []))} contact-ready candidates. Output: {args.output}', file=sys.stderr)
        return 1 if result["status"] == "error" else 0
    except (ValueError, OSError, ProviderError, KeyError, TypeError) as exc:
        # Never echo arbitrary provider bodies, request URLs, environment values or raw input.
        code = exc.code if isinstance(exc, ProviderError) else "invalid_input" if isinstance(exc, (ValueError, KeyError, TypeError)) else "io_error"
        message = str(exc) if isinstance(exc, (ProviderError, ValueError)) and not isinstance(exc, json.JSONDecodeError) else "Could not read or write the requested JSON file."
        result = {"schema_version": "1.0.0", "mode": "fixture" if args.demo or args.fixture else "live",
                  "status": "error", "completed_at": utc_now(), "request": request.to_dict() if request else None,
                  "hotels": [], "shortlist": [], "errors": [{"code": code, "message": message}]}
        try:
            write_result(result, args.output)
        except (OSError, ValueError):
            pass
        print(f"{code}: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Search interrupted; no complete result was written. Ignore any previous output file.", file=sys.stderr)
        return 130
