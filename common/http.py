"""Polite HTTP: one identifying User-Agent, timeouts, retries with backoff, and pauses between downloads."""

import logging
import time

import httpx

from common import config

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
DOWNLOAD_PAUSE_SECONDS = 1.0


def client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """A shared client; tests pass an httpx.MockTransport."""
    return httpx.Client(
        headers={"User-Agent": config.user_agent()},
        timeout=30.0,
        follow_redirects=True,
        transport=transport,
    )


def request(
    http: httpx.Client, method: str, url: str, *, attempts: int = 3, backoff: float = 2.0, **kwargs
) -> httpx.Response:
    """Sends a request, retrying timeouts, connection errors and 429/5xx with exponential backoff.

    Returns the last response (callers check the status); raises the last error if every attempt failed
    without a response.
    """
    for attempt in range(1, attempts + 1):
        try:
            response = http.request(method, url, **kwargs)
            if response.status_code not in RETRY_STATUSES or attempt == attempts:
                return response
            reason = f"HTTP {response.status_code}"
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == attempts:
                raise
            reason = type(e).__name__
        delay = backoff ** attempt
        log.warning("%s %s failed (%s); retrying in %.0fs", method, url, reason, delay)
        time.sleep(delay)
    raise AssertionError("unreachable")


def pause(seconds: float = DOWNLOAD_PAUSE_SECONDS) -> None:
    time.sleep(seconds)
