"""Klient för Bolagsverkets API för värdefulla datamängder (kostnadsfritt).

Registrera en applikation på https://portal.api.bolagsverket.se för att få
client_id och client_secret (OAuth2 client credentials).

Används för:
* grunduppgifter (namn, postort, SNI, verksamhetsbeskrivning)
* senaste digitala årsredovisningen -> medelantal anställda + VD:s namn
"""
from __future__ import annotations

import logging
import time

from .ixbrl import ReportData, extract_xhtml_from_zip, parse_report
from .polite import PoliteClient

log = logging.getLogger(__name__)

TOKEN_URL = "https://portal.api.bolagsverket.se/oauth2/token"
BASE_URL = "https://gw.api.bolagsverket.se/vardefulla-datamangder/v1"
SCOPE = "vardefulla-datamangder:read vardefulla-datamangder:ping"


def _find(obj, key: str):
    """Hittar första värdet för ``key`` var som helst i en nästlad struktur."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find(v, key)
            if r is not None:
                return r
    return None


def _find_all(obj, key: str) -> list:
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            out.extend(_find_all(v, key))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(_find_all(v, key))
    return out


class BolagsverketClient:
    def __init__(self, http: PoliteClient, client_id: str, client_secret: str,
                 base_url: str = BASE_URL, token_url: str = TOKEN_URL):
        self.http = http
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self._token = None
        self._token_expiry = 0.0

    def _auth_header(self) -> dict:
        if not self._token or time.time() > self._token_expiry - 60:
            resp = self.http.request(
                "POST", self.token_url,
                data={"grant_type": "client_credentials", "scope": SCOPE},
                auth=(self.client_id, self.client_secret))
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_expiry = time.time() + float(data.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    def _post(self, path: str, body: dict):
        resp = self.http.request("POST", f"{self.base_url}/{path}", json=body,
                                 headers=self._auth_header())
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def organisation(self, orgnr: str) -> dict:
        """Grunduppgifter i förenklad form."""
        data = self._post("organisationer", {"identitetsbeteckning": orgnr})
        if not data:
            return {}
        org = (data.get("organisationer") or [data])[0]
        name = _find(org, "namn") or ""
        sni = [s.get("kod") for s in (_find(org, "sni") or [])
               if isinstance(s, dict) and s.get("kod")]
        desc = _find(org, "beskrivning") or ""
        city = _find(org, "postort") or ""
        return {"name": name, "city": city, "sni_codes": sni,
                "business_description": desc}

    def latest_annual_report(self, orgnr: str) -> ReportData | None:
        data = self._post("dokumentlista", {"identitetsbeteckning": orgnr})
        docs = [d for d in _find_all(data or {}, "dokument") if d]
        docs = [d for sub in docs for d in (sub if isinstance(sub, list) else [sub])]
        docs = [d for d in docs if isinstance(d, dict) and d.get("dokumentId")]
        if not docs:
            return None
        docs.sort(key=lambda d: d.get("rapporteringsperiodTom", ""), reverse=True)
        doc = docs[0]
        resp = self.http.request(
            "GET", f"{self.base_url}/dokument/{doc['dokumentId']}",
            headers={**self._auth_header(), "Accept": "application/zip"})
        resp.raise_for_status()
        report = parse_report(extract_xhtml_from_zip(resp.content))
        report.period_end = doc.get("rapporteringsperiodTom", "")
        return report
