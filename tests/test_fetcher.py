"""Fetcher unit tests — G-06 allowlist gate + per-connector rate limits.

Every test injects an in-process httpx transport so no real network egress can
happen (G-06 egress sandbox; no egress approval granted this session).
"""

import httpx
import pytest

from app.services.allowlist import Allowlist, AllowlistDeniedError
from app.services.fetcher import Fetcher, FetchError
from tests.conftest import AdvancingSleep, FakeClock, FakeTransport, sample_html_bytes


def _make_fetcher(
    clock: FakeClock,
    transport: FakeTransport,
    *,
    min_interval: float = 0.0,
    timeout: float = 5.0,
) -> Fetcher:
    client = httpx.AsyncClient(transport=transport, timeout=timeout)
    return Fetcher(
        allowlist=Allowlist(["retail.example.com"]),
        client=client,
        clock=clock,
        sleep_fn=AdvancingSleep(clock),
        min_interval_seconds=min_interval,
        timeout_seconds=timeout,
    )


async def test_non_allowlisted_domain_refused_before_any_network_io() -> None:
    """G-06 default-deny: deny BEFORE the transport is touched (zero calls)."""
    clock = FakeClock()
    transport = FakeTransport(clock)
    fetcher = _make_fetcher(clock, transport)
    with pytest.raises(AllowlistDeniedError):
        await fetcher.fetch("https://evil.example.net/phish")
    assert transport.calls == []


async def test_allowlisted_domain_returns_content_metadata() -> None:
    clock = FakeClock()
    transport = FakeTransport(clock)
    payload = sample_html_bytes()
    transport.respond(
        "https://retail.example.com/report",
        payload,
        "text/html; charset=utf-8",
    )
    fetcher = _make_fetcher(clock, transport)
    result = await fetcher.fetch("https://retail.example.com/report", connector="url")
    assert result.uri == "https://retail.example.com/report"
    assert result.content == payload
    assert result.content_type == "text/html; charset=utf-8"
    assert result.fetched_at is not None
    assert transport.calls == [(clock().timestamp(), "https://retail.example.com/report")]


async def test_per_connector_rate_limit_enforced_via_injectable_clock() -> None:
    """Two calls on the same connector must not be back-to-back (min interval)."""
    clock = FakeClock()
    transport = FakeTransport(clock)
    transport.respond("https://retail.example.com/a", b"a", "text/plain")
    transport.respond("https://retail.example.com/b", b"b", "text/plain")
    fetcher = _make_fetcher(clock, transport, min_interval=5.0)
    await fetcher.fetch("https://retail.example.com/a", connector="url")
    await fetcher.fetch("https://retail.example.com/b", connector="url")
    timestamps = [ts for ts, _ in transport.calls]
    assert len(timestamps) == 2
    assert timestamps[1] - timestamps[0] >= 5.0


async def test_rate_limit_is_per_connector_not_global() -> None:
    """Different connectors (url vs rss) do not share the rate limiter."""
    clock = FakeClock()
    transport = FakeTransport(clock)
    transport.respond("https://retail.example.com/a", b"a", "text/plain")
    transport.respond("https://retail.example.com/b", b"b", "text/plain")
    fetcher = _make_fetcher(clock, transport, min_interval=60.0)
    await fetcher.fetch("https://retail.example.com/a", connector="url")
    await fetcher.fetch("https://retail.example.com/b", connector="rss")
    timestamps = [ts for ts, _ in transport.calls]
    assert timestamps[1] - timestamps[0] < 1.0


async def test_non_200_response_raises_fetch_error() -> None:
    clock = FakeClock()
    transport = FakeTransport(clock)
    transport.respond("https://retail.example.com/missing", b"", "text/plain", status_code=404)
    fetcher = _make_fetcher(clock, transport)
    with pytest.raises(FetchError):
        await fetcher.fetch("https://retail.example.com/missing", connector="url")


async def test_redirect_target_is_not_followed() -> None:
    """follow_redirects=False: a 302 must never be followed off the allowlist."""
    clock = FakeClock()
    transport = FakeTransport(clock)
    transport.respond(
        "https://retail.example.com/start",
        b"",
        "text/html",
        status_code=302,
    )
    # No canned response for the Location target -> if the client followed the
    # redirect it would hit an unknown URL and record a second transport call.
    fetcher = _make_fetcher(clock, transport)
    with pytest.raises(FetchError):
        await fetcher.fetch("https://retail.example.com/start", connector="url")
    assert len(transport.calls) == 1
    assert transport.calls[0][1] == "https://retail.example.com/start"
