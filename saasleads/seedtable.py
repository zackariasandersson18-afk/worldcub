"""Valfri komplettering: publika listor över svenska startups på Seedtable.

Hämtas bara om robots.txt tillåter det. Seedtable saknar organisationsnummer,
så namnen matchas mot SCB:s register (namnsökning) i pipelinen.
"""
from __future__ import annotations

import json
import logging

import requests
from bs4 import BeautifulSoup

from .polite import BlockedError, PoliteClient

log = logging.getLogger(__name__)

DEFAULT_URLS = [
    "https://www.seedtable.com/best-startups-in-sweden",
    "https://www.seedtable.com/best-saas-startups",
]


def _walk(obj, out: list):
    if isinstance(obj, dict):
        name = obj.get("name")
        site = obj.get("website") or obj.get("websiteUrl") or obj.get("homepage")
        country = str(obj.get("country") or obj.get("location") or "")
        if isinstance(name, str) and isinstance(site, str):
            out.append({"name": name, "website": site, "location": country})
        for v in obj.values():
            _walk(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, out)


def parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for script in soup.find_all("script", type="application/json"):
        try:
            _walk(json.loads(script.string or ""), out)
        except json.JSONDecodeError:
            continue
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            _walk(json.loads(script.string or ""), out)
        except json.JSONDecodeError:
            continue
    uniq = {c["name"].strip().lower(): c for c in out}
    return list(uniq.values())


def fetch_companies(http: PoliteClient, urls=DEFAULT_URLS,
                    sweden_only: bool = True) -> list[dict]:
    found: list[dict] = []
    for url in urls:
        try:
            resp = http.get_page(url)
            resp.raise_for_status()
        except (BlockedError, requests.RequestException) as exc:
            log.warning("Seedtable hoppas över (%s): %s", url, exc)
            continue
        for c in parse_page(resp.text):
            loc = c["location"].lower()
            in_sweden = ("swed" in loc or "sverige" in loc
                         or (not loc and "sweden" in url))
            if not sweden_only or in_sweden:
                found.append(c)
    log.info("Seedtable: %d bolag", len(found))
    return found
