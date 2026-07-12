"""``metadata.cert_info`` — TLS certificate details for HTTPS responses,
extracted from CDP ``securityDetails``. ``None`` for plain HTTP / data: URLs.

Mirrors blasthttp's ``CertInfo`` (common_name / sans / emails / issuer /
not_before / not_after / fingerprint_sha256).
"""

from __future__ import annotations

from datetime import datetime

import onyxweb


def test_cert_info_present_for_https() -> None:
    r = onyxweb.fetch("https://example.com")
    ci = r.metadata.cert_info
    assert ci is not None
    assert isinstance(ci.common_name, str)
    assert isinstance(ci.sans, list) and len(ci.sans) >= 1
    assert any("example" in s.lower() for s in ci.sans)
    assert ci.issuer  # non-empty issuer
    # not_before / not_after are ISO-8601 and ordered.
    nb = datetime.fromisoformat(ci.not_before)
    na = datetime.fromisoformat(ci.not_after)
    assert nb < na
    assert isinstance(ci.emails, list)
    # We don't fabricate a fingerprint (CDP securityDetails doesn't expose it).
    assert ci.fingerprint_sha256 is None


def test_cert_info_none_for_data_url() -> None:
    r = onyxweb.fetch("data:text/html,<html><body>x</body></html>")
    assert r.metadata.cert_info is None
