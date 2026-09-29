"""Bolagsverket – "Värdefulla datamängder" (kostnadsfritt).

Vad Bolagsverket faktiskt erbjuder (se README för detaljer):
  * API:et slår upp EN organisation per anrop via organisationsnummer.
    Det går INTE att söka/filtrera på SNI-kod i API:et.
  * Bulkfilen (nedladdningsbar, uppdateras veckovis) innehåller alla
    registrerade företag: org.nr, namn, företagsform, verksamhetsbeskrivning m.m.

Därför gör vi så här:
  1. Läs bulkfilen lokalt och filtrera på företagsform (AB) och SNI-kod om
     filen har en SNI-kolumn – annars på nyckelord i verksamhetsbeskrivningen.
  2. Om API-nycklar finns i .env: verifiera SNI-koden via API:et för kandidaterna
     (max `max_new_per_run` anrop per körning).

Bolagsverket har ingen hemsida för företagen, så `website` blir tom här.
"""
from __future__ import annotations

import csv
import io
import os
import re
import zipfile
from pathlib import Path

import requests

from ..config import resolve_path
from ..db import Lead

TOKEN_URL = "https://portal.api.bolagsverket.se/oauth2/token"
API_BASE = "https://gw.api.bolagsverket.se/vardefulla-datamangder/v1"
SCOPE = "vardefulla-datamangder:read"

# Används när bulkfilen saknar SNI-kolumn.
DEFAULT_KEYWORDS = [
    "programvar", "mjukvar", "dataprogram", "saas", "software", "molntjänst",
    "webbtjänst", "digital plattform", "applikation", "it-tjänst", "systemutveckling",
    "utveckling av programvara", "förlagsverksamhet avseende",
]

_COLUMN_ALIASES = {
    "org_nr": ["organisationsidentitet", "organisationsnummer", "orgnr", "identitetsbeteckning"],
    "name": ["organisationsnamn", "namn", "företagsnamn"],
    "form": ["organisationsform", "företagsform", "juridisk form"],
    "description": ["verksamhetsbeskrivning", "verksamhet"],
    "sni": ["sni", "snikod", "sni-kod", "naringsgren", "näringsgren"],
    "deregistered": ["avregistreringsdatum"],
    "registered": ["registreringsdatum"],
}


class BolagsverketError(RuntimeError):
    pass


def normalize_org_nr(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith(("16", "19", "20")):
        digits = digits[2:]
    return digits if len(digits) == 10 else None


def _find_columns(header: list[str]) -> dict[str, int]:
    lowered = [h.strip().lower() for h in header]
    cols = {}
    for key, aliases in _COLUMN_ALIASES.items():
        for i, h in enumerate(lowered):
            if any(h == a or h.startswith(a) for a in aliases):
                cols[key] = i
                break
    return cols


def _open_bulk(path: Path) -> io.TextIOBase:
    if not path.exists():
        raise BolagsverketError(
            f"Hittar inte bulkfilen {path}. Ladda ner den från Bolagsverket "
            "(se README) och ange sökvägen i config.yaml -> sources.bolagsverket.bulk_file."
        )
    if path.suffix.lower() == ".zip":
        zf = zipfile.ZipFile(path)
        inner = next(n for n in zf.namelist() if n.lower().endswith((".txt", ".csv")))
        return io.TextIOWrapper(zf.open(inner), encoding="utf-8-sig", newline="")
    return path.open(encoding="utf-8-sig", newline="")


def _sni_codes(raw: str) -> set[str]:
    return set(re.findall(r"\d{5}", (raw or "").replace(".", "")))


def iter_candidates(path: Path, sni_codes: list[str], only_ab: bool,
                    keywords: list[str] | None = None):
    """Läser bulkfilen och ger (org_nr, namn, beskrivning, sni_känd) för troliga SaaS-bolag."""
    wanted = {c.replace(".", "") for c in sni_codes}
    keywords = [k.lower() for k in (keywords or DEFAULT_KEYWORDS)]
    with _open_bulk(path) as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        reader = csv.reader(fh, delimiter=delimiter)
        cols = _find_columns(next(reader))
        if "org_nr" not in cols or "name" not in cols:
            raise BolagsverketError("Bulkfilen saknar kolumner för org.nr och namn.")
        for row in reader:
            get = lambda k: row[cols[k]].strip() if k in cols and cols[k] < len(row) else ""
            if get("deregistered"):
                continue
            form = get("form").upper()
            if only_ab and form and not form.startswith("AB") and "AKTIEBOLAG" not in form:
                continue
            org_nr = normalize_org_nr(get("org_nr"))
            if not org_nr:
                continue
            desc = get("description")
            if "sni" in cols:
                if not (_sni_codes(get("sni")) & wanted):
                    continue
                yield org_nr, get("name"), desc, True
            elif any(k in desc.lower() for k in keywords):
                yield org_nr, get("name"), desc, False


def _get_token(timeout: float) -> str | None:
    cid, secret = os.environ.get("BOLAGSVERKET_CLIENT_ID"), os.environ.get("BOLAGSVERKET_CLIENT_SECRET")
    if not (cid and secret):
        return None
    resp = requests.post(
        os.environ.get("BOLAGSVERKET_TOKEN_URL", TOKEN_URL),
        data={"grant_type": "client_credentials", "scope": SCOPE},
        auth=(cid, secret), timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def lookup(org_nr: str, token: str, timeout: float) -> dict | None:
    """Slår upp en organisation. Används även av `enrich` (org.nr och företagsform)."""
    resp = requests.post(
        os.environ.get("BOLAGSVERKET_API_BASE", API_BASE) + "/organisationer",
        json={"identitetsbeteckning": org_nr},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=timeout,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    orgs = resp.json().get("organisationer") or []
    return orgs[0] if orgs else None


def sni_from_api(org: dict) -> set[str]:
    codes = set()
    for item in (org.get("naringsgrenOrganisation") or {}).get("sni") or []:
        codes |= _sni_codes(str(item.get("kod", "")))
    return codes


def fetch(config: dict, is_known=None):
    cfg = config["sources"]["bolagsverket"]
    timeout = config["http"]["timeout"]
    wanted = {c.replace(".", "") for c in cfg["sni_codes"]}
    token = _get_token(timeout)
    limit = int(cfg["max_new_per_run"])
    count = 0
    for org_nr, name, desc, sni_known in iter_candidates(
        resolve_path(cfg["bulk_file"]), cfg["sni_codes"], cfg["only_aktiebolag"],
        cfg.get("keywords"),
    ):
        if count >= limit:
            break
        if is_known and is_known(org_nr=org_nr):
            continue
        if not sni_known and token:
            org = lookup(org_nr, token, timeout)
            if not org or not (sni_from_api(org) & wanted):
                continue
        count += 1
        yield Lead(
            name=name,
            source="bolagsverket",
            source_ref=org_nr,
            org_nr=org_nr,
            country="SE",
            description=(desc or "")[:500] or None,
        )
