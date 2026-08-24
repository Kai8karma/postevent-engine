#!/usr/bin/env python3
"""Shared UTM-tagging helper.

Canonical `with_utm()` -- one implementation both M2 (`modules/m2-comms/
comms.py`) and M3 (`modules/m3-repurpose/repurpose.py`) import, so the
utm_source/utm_medium/utm_campaign/utm_content shape on a link can never
drift between modules. Extracted 2026-08-24 from repurpose.py's version
(the more general of the two pre-unification copies -- it already took
explicit source/medium; comms.py's copy hardcoded utm_source=webinar/
utm_medium=email, M2's only channel). See docs/data-contract.md §8 for the
parameter table and each module's own call site for how it's used.

Python 3 stdlib only.
"""
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


def with_utm(url: str, utm_source: str, utm_medium: str, campaign: str, content: str) -> str:
    parts = urlparse(url)
    q = dict(parse_qsl(parts.query))
    q.update({
        "utm_source": utm_source,
        "utm_medium": utm_medium,
        "utm_campaign": campaign,
        "utm_content": content,
    })
    return urlunparse(parts._replace(query=urlencode(q)))
