"""Bounded, key-free searches of OTA public pages.

These adapters never call undocumented/private inventory APIs. A rendered search
shell or bot challenge is a source failure, not evidence of zero availability.
Only a matching, explicitly scoped offer can supply a sortable price.
"""

from datetime import date
from html.parser import HTMLParser
import hashlib
import json
import re
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from .normalize import safe_url
from .providers import ProviderError
from .web_extract import LODGING_TYPES, extract_property
from .web_http import PublicWebClient


PLATFORM_NAMES = ("booking", "priceline", "expedia", "hotels", "agoda")
_HOSTS = {"booking": ("booking.com",), "priceline": ("priceline.com",),
          "expedia": ("expedia.ca", "expedia.com"), "hotels": ("hotels.com",), "agoda": ("agoda.com",)}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _Node:
    def __init__(self, tag="root", attrs=None):
        self.tag, self.attrs, self.children = tag, dict(attrs or []), []

    def text(self):
        if self.tag in {"script", "style", "noscript"}:
            return ""
        return " ".join(" ".join(child.text() if isinstance(child, _Node) else child
                                 for child in self.children).split())

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, _Node):
                yield from child.walk()


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node()
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        child = _Node(tag, attrs)
        self.stack[-1].children.append(child)
        if tag not in _VOID:
            self.stack.append(child)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _type(node):
    types = node.get("@type", [])
    return {str(value).rsplit("/", 1)[-1] for value in (types if isinstance(types, list) else [types])}


def _listing_url(value, source_url, platform):
    if not isinstance(value, str):
        return None
    url = safe_url(urljoin(source_url, value))
    if not url:
        return None
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not _allowed_host(host, platform):
        return None
    if platform == "booking" and not re.match(r"^/hotel/[a-z]{2}/[^/]+\.html$", parts.path):
        return None
    if platform == "priceline" and not re.search(r"/(?:relax/(?:at|hotel)/|hotel-deals/|relax-ui/details)", parts.path):
        return None
    if platform == "expedia" and not re.search(r"\.h\d+\.Hotel-Information$", parts.path, re.I):
        return None
    if platform == "hotels" and not re.match(r"^/(?:ho\d+|[a-z]{2}/ho\d+)/", parts.path, re.I):
        return None
    if platform == "agoda" and not re.search(r"/[^/]+/hotel/[^/]+", parts.path):
        return None
    # Preserve the provider's own dated booking URL, never attach assumed dates.
    return url


def _identity(url, platform):
    parts = urlsplit(url)
    if platform == "booking":
        return parts.path.removeprefix("/hotel/").removesuffix(".html")
    if platform == "expedia":
        return re.search(r"\.h(\d+)\.Hotel-Information$", parts.path, re.I).group(1)
    if platform == "hotels":
        return re.search(r"/ho(\d+)/", parts.path, re.I).group(1)
    match = re.search(r"/(?:at|hotel)/(\d+)|/h(\d+)/", parts.path)
    if match:
        return next(value for value in match.groups() if value)
    return hashlib.sha256((parts.netloc.lower() + parts.path + parts.query).encode()).hexdigest()[:24]


def _allowed_host(host, platform):
    return any(host == allowed or host.endswith("." + allowed) for allowed in _HOSTS[platform])


def _input_scope(nodes, request):
    """Read actual HTML control values, never the URL we requested."""
    values = {}
    for node in nodes:
        if node.tag == "input" and node.attrs.get("name") and node.attrs.get("value"):
            values.setdefault(node.attrs["name"], set()).add(node.attrs["value"])
    expected = {"checkin": request["check_in"], "checkout": request["check_out"],
                "group_adults": str(request["guests"])}
    return all(values.get(key) == {value} for key, value in expected.items()) and values.get("group_children", {"0"}) == {"0"}


def _card_scope(url, text, request):
    params = parse_qs(urlsplit(url).query)
    expected = {"checkin": request["check_in"], "checkout": request["check_out"],
                "group_adults": str(request["guests"])}
    if not all(params.get(key) == [value] for key, value in expected.items()):
        return False
    if params.get("group_children", ["0"]) != ["0"]:
        return False
    nights = (date.fromisoformat(request["check_out"]) - date.fromisoformat(request["check_in"])).days
    # The returned card must independently echo occupancy and stay duration.
    # Dated query parameters alone could merely be propagated search context.
    return bool(re.search(rf"(?<!\d){nights}\s+nights?\b", text, re.I)
                and re.search(rf"(?<!\d){request['guests']}\s+adults?\b", text, re.I)
                and not re.search(r"\b[1-9]\d*\s+child(?:ren)?\b", text, re.I))


def _card_scope_conflicts(url, text, request):
    params = parse_qs(urlsplit(url).query)
    expected = {"checkin": request["check_in"], "checkout": request["check_out"], "group_adults": str(request["guests"])}
    if any(key in params and params[key] != [value] for key, value in expected.items()):
        return True
    nights = (date.fromisoformat(request["check_out"]) - date.fromisoformat(request["check_in"])).days
    observed_nights = {int(value) for value in re.findall(r"(?<!\d)(\d+)\s+nights?\b", text, re.I)}
    observed_adults = {int(value) for value in re.findall(r"(?<!\d)(\d+)\s+adults?\b", text, re.I)}
    return (bool(observed_nights - {nights}) or bool(observed_adults - {request["guests"]})
            or params.get("group_children", ["0"]) != ["0"]
            or bool(re.search(r"\b[1-9]\d*\s+child(?:ren)?\b|\bper\s+night\b", text, re.I)))


class OtaSource:
    """Search an OTA via one public HTTP request.

    Booking card parsing and schema.org lodging/offer extraction are supported.
    Other platforms use generic schema.org extraction where published; they
    often send a client-rendered shell or access block, reported explicitly.
    The client may be shared with other providers for caching and request counts.
    """

    estimated_cost = 0.0

    def __init__(self, platform, client=None, timeout=20, max_results=30):
        if platform not in PLATFORM_NAMES:
            raise ProviderError("Unsupported OTA platform.", "configuration_error")
        if type(max_results) is not int or not 1 <= max_results <= 100:
            raise ProviderError("OTA max_results must be between 1 and 100.", "configuration_error")
        self.platform = platform
        self.client = client or PublicWebClient(timeout=timeout, retries=0)
        self.max_results = max_results

    def search_url(self, request):
        location = ", ".join(value for value in (request.get("city"), request.get("province", "Ontario"), request.get("country", "Canada")) if value)
        if self.platform == "booking":
            return "https://www.booking.com/searchresults.html?" + urlencode({
                "ss": location, "checkin": request["check_in"], "checkout": request["check_out"],
                "group_adults": request["guests"], "group_children": 0, "no_rooms": 1,
                "selected_currency": request.get("currency", "CAD"), "order": "price", "lang": "en-gb",
            })
        if self.platform in {"expedia", "hotels"}:
            host = "www.expedia.ca" if self.platform == "expedia" else "ca.hotels.com"
            return f"https://{host}/Hotel-Search?" + urlencode({
                "destination": location, "startDate": request["check_in"], "endDate": request["check_out"],
                "adults": request["guests"], "rooms": request["guests"], "sort": "PRICE_LOW_TO_HIGH",
                "currency": request.get("currency", "CAD"),
            })
        if self.platform == "agoda":
            city = request.get("city")
            path = ("city/" + quote(re.sub(r"\s+", "-", city.lower()), safe="-") + "-on-ca.html"
                    if city else "region/ontario-province-ca.html")
            return "https://www.agoda.com/en-ca/" + path + "?" + urlencode({
                "checkIn": request["check_in"], "checkOut": request["check_out"],
                "rooms": 1, "adults": request["guests"], "children": 0,
                "currencyCode": request.get("currency", "CAD"), "sort": "priceLowToHigh",
            })
        # This is Priceline's public navigation route. Its normal public redirect
        # converts it to /relax-ui/listings; no internal GraphQL calls are made.
        location_path = quote(location.replace(", ", "-"), safe="-")
        return (f"https://www.priceline.com/relax/in/{location_path}"
                f"/from/{request['check_in'].replace('-', '')}"
                f"/to/{request['check_out'].replace('-', '')}/rooms/1/adults/{request['guests']}"
                + "?" + urlencode({"currency": request.get("currency", "CAD"), "sort": "PRICE"}))

    def search(self, request):
        try:
            response = self.client.get(self.search_url(request))
        except ProviderError as exc:
            if exc.code in {"website_blocked", "rate_limit", "authentication_error"}:
                raise ProviderError(f"{self.platform} blocked the public search request.", "source_blocked") from None
            raise
        html, source_url = response.get("text"), response.get("url")
        if not isinstance(html, str) or not isinstance(source_url, str):
            raise ProviderError(f"{self.platform} returned an invalid page.", "invalid_response")
        host = (urlsplit(source_url).hostname or "").lower()
        if not _allowed_host(host, self.platform):
            raise ProviderError(f"{self.platform} redirected outside its public website.", "source_blocked")
        if re.search(r"AwsWafIntegration|/__challenge_|challenge-container|verify (?:that )?you(?:'re| are) (?:a human|not a robot)|access denied|captcha-container|cf-chl-|px-captcha", html, re.I):
            raise ProviderError(f"{self.platform} returned a bot challenge or access block.", "source_blocked")
        page = _Page()
        try:
            page.feed(html)
        except (ValueError, RecursionError):
            raise ProviderError(f"{self.platform} search markup could not be read.", "layout_changed") from None
        nodes = list(page.root.walk())
        results = self._structured(nodes, source_url, request)
        if self.platform == "booking":
            results.extend(self._booking_cards(nodes, source_url, request))
        unique = {}
        for row in results:
            identity = row["property_token"]
            # Prefer the richer scoped structured offer over an unpriced card.
            if identity not in unique or ("total_rate" in row and "total_rate" not in unique[identity]):
                unique[identity] = row
        properties = list(unique.values())[:self.max_results]
        for row in properties:
            row["_fetched_at"] = response.get("fetched_at") or row.get("_fetched_at")
            row["_provider_observed_at"] = response.get("observed_at")
        if not properties:
            # A bare shell is not an empty inventory response. Even explicit
            # no-results pages are not useful unless the stay was echoed.
            if self.platform == "booking" and _input_scope(nodes, request) and re.search(r"no (?:properties|results|accommodations) (?:found|available|match)", page.root.text(), re.I):
                return {"properties": [], "_source_url": source_url, "_fetched_at": response.get("fetched_at"),
                        "_coverage_warning": f"{self.platform}: no results on the returned, date-scoped first search page."}
            raise ProviderError(f"{self.platform} returned no readable lodging results; the page may require JavaScript or its layout changed.", "layout_changed")
        return {"properties": properties, "_source_url": source_url, "_fetched_at": response.get("fetched_at"),
                "_coverage_warning": f"{self.platform}: bounded first-page public search only; unverified prices stay unknown. Public-page access and coverage can vary."}

    def _decorate(self, row, url):
        identity = _identity(url, self.platform)
        row.update({"property_token": f"{self.platform}:{identity}", "link": url,
                    "discovery_source": self.platform, "_platform": self.platform,
                    "_listing_id": identity})
        price = row.get("total_rate", {}).get("extracted_lowest")
        scope = row.get("_price_scope", {})
        row["_platform_offer"] = {"platform": self.platform, "listing_id": identity,
                                  "listing_url": url, "source_url": row["_source_url"],
                                  "amount": price, "currency": row.get("currency"),
                                  "basis": "stay" if price is not None else None,
                                  "check_in": scope.get("check_in"), "check_out": scope.get("check_out"),
                                  "guests": scope.get("guests"), "scope_verified": price is not None,
                                  "raw_labels": row.get("_price_evidence", row.get("_indicative_price_evidence", []))}
        return row

    def _structured(self, nodes, source_url, request):
        results = []
        for node in nodes:
            if node.tag != "script" or node.attrs.get("type", "").lower() != "application/ld+json":
                continue
            try:
                data = json.loads("".join(value for value in node.children if isinstance(value, str)))
            except (ValueError, TypeError, RecursionError):
                continue
            for entity in _walk(data):
                if not _type(entity) & LODGING_TYPES:
                    continue
                url = _listing_url(entity.get("url") or entity.get("@id"), source_url, self.platform)
                if not url:
                    continue
                payload = '<script type="application/ld+json">' + json.dumps(entity).replace("</", "<\\/") + '</script>'
                row = extract_property(payload, source_url, request)
                if row:
                    offers = entity.get("offers", [])
                    offers = offers if isinstance(offers, list) else [offers]
                    row["_price_evidence"] = [{key: value for key, value in value.items() if key in {
                        "price", "totalPrice", "priceCurrency", "priceSpecification", "checkIn", "checkOut", "numAdults", "availability"
                    }} for value in offers if isinstance(value, dict)]
                    # A scoped nightly quote can be useful evidence, but this
                    # adapter exposes only explicit full-stay totals for ranking.
                    if "rate_per_night" in row:
                        row.setdefault("_indicative_price_evidence", []).append({"evidence": row.pop("rate_per_night"), "reason": "nightly_not_explicit_full_stay_total"})
                    results.append(self._decorate(row, url))
        return results

    def _booking_cards(self, nodes, source_url, request):
        results = []
        input_scope = _input_scope(nodes, request)
        for card in nodes:
            if card.attrs.get("data-testid") != "property-card":
                continue
            fields = list(card.walk())
            title = next((node.text() for node in fields if node.attrs.get("data-testid") == "title" and node.text()), None)
            link = next((_listing_url(node.attrs.get("href"), source_url, self.platform)
                         for node in fields if node.tag == "a" and _listing_url(node.attrs.get("href"), source_url, self.platform)), None)
            if not title or not link:
                continue
            address = next((node.text() for node in fields if node.attrs.get("data-testid") == "address"), None)
            phones = {node.attrs["href"][4:] for node in fields if node.tag == "a" and node.attrs.get("href", "").lower().startswith("tel:")}
            row = {"name": title, "address": address, "phone": next(iter(phones)) if len(phones) == 1 else None,
                   "_source_url": source_url, "availability_status": "unknown", "type": "LodgingBusiness"}
            labels = [node.text() for node in fields if node.attrs.get("data-testid") == "price-and-discounted-price"]
            price_text = labels[0] if len(labels) == 1 else ""
            if price_text:
                row["_price_evidence"] = [{"price_label": price_text, "card_text": card.text()[:1200]}]
            # Currency must be visible; a URL selected_currency parameter does
            # not prove the page actually rendered CAD.
            match = re.fullmatch(r"(?:CAD|C\$|CA\$)\s*([\d,]+(?:\.\d{1,2})?)", price_text)
            card_text = card.text()
            scope_ok = input_scope or _card_scope(link, card_text, request)
            total_label = bool(re.search(r"\btotal\b|\b\d+\s+nights?\b", card_text, re.I))
            if match and scope_ok and total_label and not _card_scope_conflicts(link, card_text, request):
                amount = float(match.group(1).replace(",", ""))
                if 0 < amount < 10000000:
                    row.update({"total_rate": {"extracted_lowest": amount, "lowest": price_text}, "currency": "CAD",
                                "num_guests": request["guests"], "_price_scope": {key: request[key] for key in ("check_in", "check_out", "guests", "currency")},
                                "_tax_fee_status": "included" if re.search(r"includes taxes and (?:charges|fees)", card_text, re.I) else "unknown"})
            if price_text and "total_rate" not in row:
                row["_indicative_price_evidence"] = [{"text": price_text, "reason": "not_a_verified_full_stay_quote", "source_url": source_url}]
            results.append(self._decorate(row, link))
        return results
