"""Klient för SCB:s företagsregister-API (Företagsregistret, nv0101).

API:t kräver ett klientcertifikat från SCB (ansök via scb.se → Företagsregistret
→ "API för företagsregistret"). SCB levererar certifikatet som .pfx; konvertera
till PEM så att requests kan använda det:

    openssl pkcs12 -in scb.pfx -out scb.pem -nodes

Namnen på kategorier och koder (t.ex. "Storleksklass") kan ändras av SCB. Kör
``python -m saasleads scb-kategorier`` för att lista vad API:t faktiskt
använder och justera ``--scb-*``-flaggorna vid behov.
"""
from __future__ import annotations

import logging
import re

from .models import Lead, normalize_orgnr
from .polite import PoliteClient

log = logging.getLogger(__name__)

BASE_URL = "https://privateapi.scb.se/nv0101/v1/sokpavar/api"
MAX_ROWS = 2000  # SCB returnerar högst så här många rader per anrop

# SCB:s storleksklasser för anställda (juridiska enheter). 1 = 1–4, 2 = 5–9,
# 3 = 10–19, 4 = 20–49. SCB har ingen klass som börjar på 2 anställda, så vi
# hämtar 1–49 och filtrerar sedan på medelantal anställda från årsredovisningen.
DEFAULT_SIZE_CLASSES = ["1", "2", "3", "4"]
DEFAULT_SNI = ["58290", "62010"]
# Län (säte) används för att dela upp sökningar som ger fler än 2000 träffar.
LAN_CODES = ["01", "03", "04", "05", "06", "07", "08", "09", "10", "12", "13",
             "14", "17", "18", "19", "20", "21", "22", "23", "24", "25"]


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9åäö]", "", key.lower())


def pick(row: dict, *candidates: str) -> str:
    """Hämtar första fältet vars normaliserade nyckel matchar en kandidat."""
    normed = {_norm_key(k): v for k, v in row.items()}
    for cand in candidates:
        v = normed.get(_norm_key(cand))
        if v not in (None, ""):
            return str(v).strip()
    return ""


def row_to_lead(row: dict) -> Lead | None:
    orgnr = normalize_orgnr(pick(row, "OrgNr", "PeOrgNr", "Organisationsnummer"))
    name = pick(row, "Företagsnamn", "Foretagsnamn", "Namn")
    if len(orgnr) != 10 or not name:
        return None
    sni = [c for c in (
        pick(row, "Bransch_1, kod", "Bransch_1kod", "Bransch1kod"),
        pick(row, "Bransch_2, kod", "Bransch_2kod"),
        pick(row, "Bransch_3, kod", "Bransch_3kod"),
    ) if c]
    return Lead(
        orgnr=orgnr,
        name=name,
        city=pick(row, "PostOrt", "Postort", "BesöksPostOrt", "Säteskommun"),
        sni_codes=sni,
        size_class=pick(row, "Storleksklass", "Stkl", "Storleksklass, kod"),
        website=pick(row, "Webbadress", "Hemsida", "Url", "Webbplats"),
        sources=["SCB"],
    )


class SCBClient:
    def __init__(self, http: PoliteClient, cert_path: str,
                 base_url: str = BASE_URL,
                 sni_category: str = "Bransch",
                 size_category: str = "Storleksklass",
                 lan_category: str = "Säteslän"):
        self.http = http
        self.cert = cert_path
        self.base_url = base_url.rstrip("/")
        self.sni_category = sni_category
        self.size_category = size_category
        self.lan_category = lan_category

    def _call(self, method: str, path: str, body: dict | None = None):
        resp = self.http.request(method, f"{self.base_url}/{path}",
                                 json=body, cert=self.cert)
        resp.raise_for_status()
        return resp.json()

    def categories(self):
        return self._call("GET", "Je/KategorierMedKodtabeller")

    def _query(self, sni: str, size_classes: list[str],
               lan: str | None = None) -> dict:
        kategorier = [
            {"Kategori": self.sni_category, "Kod": [sni], "BranschNiva": 5},
            {"Kategori": self.size_category, "Kod": list(size_classes)},
        ]
        if lan:
            kategorier.append({"Kategori": self.lan_category, "Kod": [lan]})
        return {
            "Företagsstatus": "1",       # verksamma
            "Registreringsstatus": "1",  # registrerade
            "variabler": [],
            "Kategorier": kategorier,
        }

    def count(self, query: dict) -> int:
        data = self._call("POST", "Je/RaknaForetag", query)
        if isinstance(data, dict):
            data = next(iter(data.values()), 0)
        return int(data)

    def fetch(self, query: dict) -> list[dict]:
        data = self._call("POST", "Je/HamtaForetag", query)
        return data if isinstance(data, list) else data.get("Foretag", [])

    def iter_companies(self, sni_codes=DEFAULT_SNI,
                       size_classes=DEFAULT_SIZE_CLASSES):
        """Ger Lead-objekt; delar upp per län om en sökning är för stor."""
        seen: set[str] = set()
        for sni in sni_codes:
            for size in size_classes:
                q = self._query(sni, [size])
                n = self.count(q)
                log.info("SCB: SNI %s, storleksklass %s -> %d företag",
                         sni, size, n)
                queries = [q] if n <= MAX_ROWS else [
                    self._query(sni, [size], lan) for lan in LAN_CODES]
                for sub in queries:
                    rows = self.fetch(sub)
                    if len(rows) >= MAX_ROWS:
                        log.warning("SCB-sökningen gav %d rader (max) – "
                                    "resultatet kan vara avkortat", len(rows))
                    for row in rows:
                        lead = row_to_lead(row)
                        if lead and lead.orgnr not in seen:
                            seen.add(lead.orgnr)
                            yield lead

    def search_by_name(self, name: str) -> list[Lead]:
        q = {"Företagsstatus": "1", "Registreringsstatus": "1",
             "variabler": [{"Varde1": name, "Varde2": "",
                            "Operator": "Innehaller", "Variabel": "Namn"}],
             "Kategorier": []}
        return [l for l in map(row_to_lead, self.fetch(q)) if l]
