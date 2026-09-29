"""Hittar och verifierar bolagets hemsida utan sökmotorskrapning.

Kandidatdomäner gissas från företagsnamnet (acme.se, acme.com, acme.io ...).
En träff godkänns bara om sidan nämner organisationsnumret ("orgnr") eller
tydligt nämner företagsnamnet ("namn"). Alla anrop går via PoliteClient och
följer därmed robots.txt, fördröjningar och blocklistan.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .polite import BlockedError, PoliteClient, is_blocked_domain

log = logging.getLogger(__name__)

TLDS = ["se", "com", "io", "ai", "app", "tech", "co"]
LEGAL_SUFFIXES = re.compile(
    r"\b(aktiebolag|ab|publ|hb|kb|sweden|sverige|nordic|scandinavia|group|"
    r"holding|i\s+\w+)\b\.?", re.I)
GENERIC_WORDS = {"software", "technologies", "technology", "tech", "solutions",
                 "systems", "digital", "labs", "it", "data", "the"}
PARKED_RE = re.compile(
    r"domain (?:is )?for sale|domänen (?:är )?till salu|parked|"
    r"this domain|webbhotell|under construction|kommer snart", re.I)
PRICING_LINK_RE = re.compile(r"pricing|priser|prices|plans|abonnemang|pris", re.I)


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def name_tokens(name: str) -> list[str]:
    cleaned = LEGAL_SUFFIXES.sub(" ", name.replace("&", " och "))
    cleaned = re.sub(r"\(.*?\)", " ", cleaned)
    return [t for t in re.split(r"[^a-z0-9]+", _ascii(cleaned).lower()) if t]


def candidate_domains(name: str) -> list[str]:
    tokens = name_tokens(name)
    if not tokens:
        return []
    core = [t for t in tokens if t not in GENERIC_WORDS] or tokens
    slugs = ["".join(tokens), "-".join(tokens), "".join(core)]
    out = []
    for slug in dict.fromkeys(s for s in slugs if len(s) >= 3):
        for tld in TLDS:
            out.append(f"{slug}.{tld}")
    return out


def verify(html: str, name: str, orgnr: str) -> str:
    """'orgnr', 'namn' eller '' beroende på hur säkert sidan hör till bolaget."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    if PARKED_RE.search(text[:2000]) and len(text) < 3000:
        return ""
    compact = re.sub(r"[\s\-]", "", text)
    if orgnr and orgnr in compact:
        return "orgnr"
    core = [t for t in name_tokens(name) if t not in GENERIC_WORDS]
    phrase = " ".join(core)
    if phrase and len(phrase) >= 4 and phrase in _ascii(text).lower():
        return "namn"
    return ""


class WebsiteFinder:
    def __init__(self, http: PoliteClient, max_candidates: int = 8):
        self.http = http
        self.max_candidates = max_candidates

    def _get(self, url: str) -> requests.Response | None:
        try:
            resp = self.http.get_page(url)
        except (BlockedError, requests.RequestException) as exc:
            log.debug("Hoppar över %s: %s", url, exc)
            return None
        if resp.status_code != 200 or is_blocked_domain(resp.url):
            return None
        if "html" not in resp.headers.get("Content-Type", "html"):
            return None
        return resp

    def find(self, name: str, orgnr: str, known_url: str = ""):
        """Returnerar (url, säkerhet, html) eller ('', '', '')."""
        if known_url:
            url = known_url if "://" in known_url else f"https://{known_url}"
            resp = self._get(url)
            if resp is not None:
                return resp.url, verify(resp.text, name, orgnr) or "känd", resp.text
        for domain in candidate_domains(name)[: self.max_candidates]:
            resp = self._get(f"https://{domain}/")
            if resp is None:
                continue
            conf = verify(resp.text, name, orgnr)
            if conf:
                return resp.url, conf, resp.text
        return "", "", ""

    def pricing_page(self, base_url: str, html: str) -> str:
        """Hämtar en eventuell pris-/planer-sida på samma domän (max 1 anrop)."""
        soup = BeautifulSoup(html, "html.parser")
        host = urlsplit(base_url).hostname
        for a in soup.find_all("a", href=True):
            href = urljoin(base_url, a["href"])
            if urlsplit(href).hostname != host:
                continue
            if PRICING_LINK_RE.search(a["href"]) or PRICING_LINK_RE.search(a.get_text()):
                resp = self._get(href)
                return resp.text if resp is not None else ""
        return ""
