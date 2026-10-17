"""Small cached HTTP client for public web pages; no paid search service."""

from datetime import datetime, timezone
import gzip
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import re
import socket
import threading
import time
import zlib
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .providers import ProviderError


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)


def public_url(url):
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        if parsed.port not in (None, 80, 443):
            raise ValueError
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError
    except (ValueError, TypeError, OSError):
        raise ProviderError("Website URL is not a reachable public HTTP(S) destination.", "invalid_url") from None
    return url


class _PublicRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PublicWebClient:
    """Cache exact URLs, including dates/guests when present in the URL."""

    def __init__(self, timeout=20, retries=1, cache_dir="outputs/web-cache", fresh=False, cache_ttl=300):
        self.timeout = timeout
        self.retries = retries
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.fresh = fresh
        self.cache_ttl = cache_ttl
        self._count_lock = threading.Lock()
        self._request_count = 0
        self._cache_hits = 0

    @property
    def request_count(self):
        with self._count_lock:
            return self._request_count

    @property
    def cache_hits(self):
        with self._count_lock:
            return self._cache_hits

    def get(self, url):
        public_url(url)
        path = self.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json") if self.cache_dir else None
        if path and not self.fresh:
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                age = time.time() - cached["cached_at"]
                if 0 <= age < self.cache_ttl and cached["url_requested"] == url:
                    public_url(cached["url"])
                    if not isinstance(cached.get("text"), str):
                        raise ValueError
                    with self._count_lock:
                        self._cache_hits += 1
                    return {**cached, "from_cache": True}
            except (OSError, KeyError, TypeError, ValueError, ProviderError):
                pass
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5", "Accept-Language": "en-CA,en;q=0.9", "Accept-Encoding": "gzip"})
        for attempt in range(self.retries + 1):
            with self._count_lock:
                self._request_count += 1
            try:
                opener = build_opener(_PublicRedirect())
                with opener.open(request, timeout=self.timeout) as response:
                    content_type = response.headers.get("Content-Type", "")
                    payload = response.read(8 * 1024 * 1024 + 1)
                    if len(payload) > 8 * 1024 * 1024:
                        raise ProviderError("Website response exceeded the 8 MiB limit.", "response_too_large")
                    if response.headers.get("Content-Encoding", "").lower() == "gzip":
                        try:
                            with gzip.GzipFile(fileobj=io.BytesIO(payload)) as expanded:
                                payload = expanded.read(16 * 1024 * 1024 + 1)
                        except (OSError, EOFError, zlib.error):
                            raise ProviderError("Website returned invalid compressed data.", "invalid_response") from None
                        if len(payload) > 16 * 1024 * 1024:
                            raise ProviderError("Expanded website response exceeded the limit.", "response_too_large")
                    if content_type and not any(kind in content_type.lower() for kind in ("text/", "json", "xml")):
                        raise ProviderError("Website did not return a text page.", "unsupported_content")
                    charset = re.search(r"charset=([\w-]+)", content_type, re.I)
                    encoding = charset.group(1) if charset else "utf-8"
                    try:
                        html = payload.decode(encoding, errors="replace")
                    except LookupError:
                        html = payload.decode("utf-8", errors="replace")
                    final_url = response.geturl()
                    public_url(final_url)
                result = {"text": html, "url": final_url, "url_requested": url,
                          "fetched_at": datetime.now(timezone.utc).isoformat(), "observed_at": None,
                          "content_type": content_type, "from_cache": False, "cached_at": time.time()}
                if path:
                    try:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        pass
                return result
            except HTTPError as exc:
                status = exc.code
                exc.close()
                if (status == 429 or 500 <= status <= 599) and attempt < self.retries:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                raise ProviderError(f"Website returned HTTP {status}.", "rate_limit" if status == 429 else "website_blocked" if status in (401, 403) else "http_error") from None
            except (URLError, OSError, TimeoutError) as exc:
                timed_out = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
                if timed_out and attempt < self.retries:
                    time.sleep(0.3 * (attempt + 1))
                    continue
                raise ProviderError("Website request timed out." if timed_out else "Could not fetch the public website.", "timeout" if timed_out else "network_error") from None
        raise ProviderError("Website request failed.")
