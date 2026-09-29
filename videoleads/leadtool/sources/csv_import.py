"""Manuell import: `python main.py import fil.csv`.

Kolumnnamn på svenska eller engelska fungerar, t.ex.
    företagsnamn/name, hemsida/website, beskrivning/description,
    lanseringsdatum/launch_date, org_nr, land/country
Bara namn och/eller hemsida krävs.
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..db import Lead
from ..web import domain_of, normalize_url
from .bolagsverket import normalize_org_nr

ALIASES = {
    "name": ["name", "företagsnamn", "foretagsnamn", "företag", "company", "namn"],
    "website": ["website", "hemsida", "url", "webbplats", "domain", "domän"],
    "description": ["description", "beskrivning", "desc"],
    "launch_date": ["launch_date", "lanseringsdatum", "lanserad", "launched"],
    "org_nr": ["org_nr", "orgnr", "organisationsnummer", "org.nr"],
    "country": ["country", "land"],
}


def _map_header(fields: list[str]) -> dict[str, str]:
    mapping = {}
    for f in fields:
        key = f.strip().lower()
        for target, aliases in ALIASES.items():
            if key in aliases and target not in mapping:
                mapping[target] = f
    return mapping


def read_csv(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(fh, delimiter=delimiter)
        mapping = _map_header(reader.fieldnames or [])
        if "name" not in mapping and "website" not in mapping:
            raise ValueError("CSV-filen behöver minst en kolumn för namn eller hemsida.")
        for row in reader:
            get = lambda k: (row.get(mapping[k]) or "").strip() if k in mapping else ""
            website = normalize_url(get("website"))
            domain = domain_of(website)
            name = get("name") or domain
            if not name:
                continue
            yield Lead(
                name=name,
                source="import",
                website=website,
                domain=domain,
                description=get("description") or None,
                launch_date=get("launch_date") or None,
                org_nr=normalize_org_nr(get("org_nr")),
                country=(get("country") or None),
            )
