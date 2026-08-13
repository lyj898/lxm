"""Polite HTTP client: robots.txt aware, rate limited, backs off on 429/5xx."""

from __future__ import annotations

import logging
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger(__name__)


class RobotsDisallowed(RuntimeError):
    """The source's robots.txt forbids this path for our User-Agent."""


class TooLarge(RuntimeError):
    """File exceeds the hard size refusal threshold."""


class Retryable(RuntimeError):
    """429 or 5xx - worth another attempt after backoff."""


_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
)


def decode_html(content: bytes, header_charset: str | None = None) -> str:
    """Decode page bytes. Some of these sites serve UTF-16 with a BOM, so a
    blind utf-8 decode yields NUL-interleaved garbage - sniff first."""
    for bom, enc in _BOMS:
        if content.startswith(bom):
            # The explicit -le/-be codecs keep the BOM as a literal U+FEFF,
            # which would land at the very start of the markup.
            return content.decode(enc, errors="replace").lstrip("﻿")
    for enc in (header_charset, "utf-8"):
        if not enc:
            continue
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("cp1252", errors="replace")


@dataclass
class Response:
    url: str
    status: int
    content: bytes
    headers: dict[str, str]
    encoding: str | None = None

    @property
    def text(self) -> str:
        return decode_html(self.content, self.encoding)


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        min_delay: float = 5.0,
        timeout: float = 60.0,
        max_retries: int = 5,
        respect_robots: bool = True,
        max_file_bytes: int = 95 * 1024 * 1024,
    ) -> None:
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self.max_file_bytes = max_file_bytes
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
                "Accept-Language": "en-SG,en;q=0.9",
                # These hosts advertise Keep-Alive: timeout=5 and our polite
                # delay is >=5s, so a pooled socket is always dead by the next
                # request - the first attempt would fail and burn a retry,
                # doubling our real request count. Ask for a fresh connection.
                "Connection": "close",
            }
        )
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self.urls_seen = 0

    # -- robots ---------------------------------------------------------
    def _robots_for(self, url: str) -> robotparser.RobotFileParser | None:
        host = urlparse(url).netloc
        if host in self._robots:
            return self._robots[host]
        robots_url = urljoin(f"{urlparse(url).scheme}://{host}", "/robots.txt")
        rp = robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            self._sleep_for(host)
            resp = self.session.get(robots_url, timeout=self.timeout)
            self._last_request[host] = time.monotonic()
            if resp.status_code >= 400:
                # No robots.txt published -> nothing is disallowed.
                log.info("robots.txt %s -> HTTP %s, treating as allow-all",
                         robots_url, resp.status_code)
                rp = None
            else:
                rp.parse(resp.text.splitlines())
        except requests.RequestException as exc:
            log.warning("robots.txt fetch failed for %s (%s); treating as allow-all",
                        host, exc)
            rp = None
        self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url) or rp.can_fetch("*", url)

    def crawl_delay(self, url: str) -> float:
        rp = self._robots_for(url) if self.respect_robots else None
        if rp is None:
            return self.min_delay
        for agent in (self.user_agent, "*"):
            try:
                delay = rp.crawl_delay(agent)
            except Exception:  # pragma: no cover - parser quirks
                delay = None
            if delay:
                return max(float(delay), self.min_delay)
        return self.min_delay

    # -- rate limiting --------------------------------------------------
    def _sleep_for(self, host: str, delay: float | None = None) -> None:
        last = self._last_request.get(host)
        if last is None:
            return
        wait = (delay if delay is not None else self.min_delay) - (
            time.monotonic() - last
        )
        if wait > 0:
            log.debug("sleeping %.1fs before next request to %s", wait, host)
            time.sleep(wait)

    # -- fetching -------------------------------------------------------
    def get(self, url: str, *, stream_limit: bool = False) -> Response:
        """GET with robots check, per-host delay, and retry/backoff."""
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        host = urlparse(url).netloc
        self._sleep_for(host, self.crawl_delay(url))

        attempt_get = retry(
            reraise=True,
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=self.min_delay, min=self.min_delay, max=120),
            retry=retry_if_exception_type((Retryable, requests.RequestException)),
        )(self._get_once)
        try:
            return attempt_get(url, stream_limit)
        finally:
            self._last_request[host] = time.monotonic()
            self.urls_seen += 1

    def _get_once(self, url: str, stream_limit: bool) -> Response:
        log.info("GET %s", url)
        resp = self.session.get(
            url, timeout=self.timeout, stream=stream_limit, allow_redirects=True
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            log.warning("HTTP %s for %s - backing off", resp.status_code, url)
            raise Retryable(f"HTTP {resp.status_code} for {url}")
        resp.raise_for_status()

        declared = resp.headers.get("Content-Length")
        if declared and int(declared) >= self.max_file_bytes:
            resp.close()
            raise TooLarge(f"{url} declares {declared} bytes")

        if not stream_limit:
            content = resp.content
        else:
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(1 << 16):
                total += len(chunk)
                if total >= self.max_file_bytes:
                    resp.close()
                    raise TooLarge(f"{url} exceeded {self.max_file_bytes} bytes")
                chunks.append(chunk)
            content = b"".join(chunks)

        return Response(
            url=resp.url,
            status=resp.status_code,
            content=content,
            headers=dict(resp.headers),
            encoding=resp.encoding,
        )

    def get_html(self, url: str) -> Response:
        return self.get(url)

    def get_binary(self, url: str) -> Response:
        return self.get(url, stream_limit=True)

    def post_binary(self, url: str, data: dict[str, Any]) -> Response:
        """POST a form and return the body. Some sources (testpapersfree) hand
        out files only through a POST download button."""
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        host = urlparse(url).netloc
        self._sleep_for(host, self.crawl_delay(url))
        try:
            log.info("POST %s", url)
            resp = self.session.post(
                url, data=data, timeout=self.timeout, allow_redirects=True
            )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise Retryable(f"HTTP {resp.status_code} for {url}")
            resp.raise_for_status()
            if len(resp.content) >= self.max_file_bytes:
                raise TooLarge(f"{url} returned {len(resp.content)} bytes")
            return Response(
                url=resp.url,
                status=resp.status_code,
                content=resp.content,
                headers=dict(resp.headers),
                encoding=resp.encoding,
            )
        finally:
            self._last_request[host] = time.monotonic()
            self.urls_seen += 1


def fetcher_from_config(crawl_cfg: dict[str, Any]) -> Fetcher:
    return Fetcher(
        user_agent=crawl_cfg["user_agent"],
        min_delay=float(crawl_cfg["min_delay_seconds"]),
        timeout=float(crawl_cfg["timeout_seconds"]),
        max_retries=int(crawl_cfg["max_retries"]),
        respect_robots=bool(crawl_cfg["respect_robots"]),
        max_file_bytes=int(crawl_cfg["max_file_bytes"]),
    )
