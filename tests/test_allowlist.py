"""Allowlist unit tests — G-06 default-deny egress gate.

The allowlist is the enforcement point for the egress sandbox: a URI whose host
is not (a suffix of) a configured domain must never reach the network layer.
"""

import pytest

from app.services.allowlist import Allowlist, AllowlistDeniedError


def test_exact_allowlisted_domain_is_allowed() -> None:
    allowlist = Allowlist(["retail.example.com"])
    assert allowlist.allows("retail.example.com")


def test_subdomains_of_allowlisted_domain_are_allowed() -> None:
    allowlist = Allowlist(["retail.example.com"])
    assert allowlist.allows("news.retail.example.com")
    assert allowlist.allows("a.b.retail.example.com")


def test_unrelated_domains_are_denied() -> None:
    allowlist = Allowlist(["retail.example.com"])
    assert not allowlist.allows("example.com")
    assert not allowlist.allows("evil.net")
    assert not allowlist.allows("retail.example.com.evil.net")
    assert not allowlist.allows("evil-retail.example.com")
    assert not allowlist.allows("notretail.example.com.evil.io")


def test_wildcard_entries_are_normalized_to_suffix_rules() -> None:
    allowlist = Allowlist(["*.retail.example.com"])
    assert allowlist.allows("retail.example.com")
    assert allowlist.allows("news.retail.example.com")


def test_check_raises_allowlist_denied_for_non_allowlisted_uri() -> None:
    allowlist = Allowlist(["retail.example.com"])
    with pytest.raises(AllowlistDeniedError):
        allowlist.check("https://evil.example.net/phish")


def test_check_accepts_allowlisted_uri_with_port_and_path() -> None:
    allowlist = Allowlist(["retail.example.com"])
    uri = "https://retail.example.com:8443/reports/quarterly?page=2"
    assert allowlist.check(uri) == uri


def test_empty_allowlist_denies_everything() -> None:
    allowlist = Allowlist([])
    assert not allowlist.allows("retail.example.com")
    with pytest.raises(AllowlistDeniedError):
        allowlist.check("https://retail.example.com/report")


def test_from_settings_parses_comma_separated_domains(fake_settings) -> None:
    allowlist = Allowlist.from_settings(fake_settings)
    assert allowlist.allows("retail.example.com")
    assert allowlist.allows("retailtech.example.com")
    assert not allowlist.allows("example.com")
