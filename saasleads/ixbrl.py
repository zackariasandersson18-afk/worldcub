"""Tolkar digitala årsredovisningar (iXBRL) från Bolagsverket.

Ur årsredovisningen hämtas:
* medelantal anställda (``se-gen-base:MedelantaletAnstallda``)
* undertecknare med roll (``...UnderskriftHandlingTilltalsnamn/Efternamn/Roll``),
  varifrån vi väljer VD – eller, om bolaget saknar VD, styrelseordförande /
  ensam styrelseledamot (i småbolag oftast grundaren).
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import OrderedDict
from dataclasses import dataclass

from bs4 import BeautifulSoup

VD_RE = re.compile(r"verkst[äa]llande\s+direkt[öo]r|\bvd\b|\bceo\b", re.I)
CHAIR_RE = re.compile(r"ordf[öo]rande", re.I)
NAME_LINE_RE = re.compile(
    r"^[A-ZÅÄÖÉ][\w'\-éü]+(?:\s+[A-ZÅÄÖÉ][\w'\-éü]+){1,3}$")


@dataclass
class Signer:
    first_name: str
    last_name: str
    role: str

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class ReportData:
    employees: int | None
    signers: list[Signer]
    period_end: str = ""


def extract_xhtml_from_zip(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist()
                 if n.lower().endswith((".xhtml", ".html", ".htm"))]
        if not names:
            raise ValueError("Ingen xhtml-fil i årsredovisningens zip")
        return zf.read(names[0]).decode("utf-8", errors="replace")


def _local(name: str) -> str:
    return name.split(":", 1)[-1]


def _parse_number(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def parse_report(xhtml: str) -> ReportData:
    soup = BeautifulSoup(xhtml, "html.parser")

    # --- medelantal anställda -------------------------------------------
    employees = None
    candidates = [t for t in soup.find_all("ix:nonfraction")
                  if _local(t.get("name", "")) == "MedelantaletAnstallda"]
    # Nuvarande räkenskapsår har normalt contextRef "period0"
    candidates.sort(key=lambda t: t.get("contextref", "") != "period0")
    for tag in candidates:
        employees = _parse_number(tag.get_text())
        if employees is not None:
            if tag.get("sign") == "-":
                employees = -employees
            break

    # --- undertecknare ----------------------------------------------------
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for tag in soup.find_all("ix:nonnumeric"):
        local = _local(tag.get("name", ""))
        if not local.startswith("Underskrift"):
            continue
        for field_ in ("Tilltalsnamn", "Fornamn", "Efternamn", "Roll"):
            if local.endswith(field_):
                stem = local[: -len(field_)]
                key = tag.get("tupleref") or f"{stem}#{len(groups)}"
                grp = groups.setdefault(key, {"stem": stem})
                fld = "first" if field_ in ("Tilltalsnamn", "Fornamn") \
                    else ("last" if field_ == "Efternamn" else "role")
                grp.setdefault(fld, tag.get_text(" ", strip=True))
                break

    signers, seen = [], set()
    for grp in groups.values():
        # Fastställelseintyget är inte en roll i bolaget – hoppa över.
        if "Faststallelseintyg" in grp["stem"] or not grp.get("first"):
            continue
        s = Signer(grp.get("first", ""), grp.get("last", ""),
                   grp.get("role", ""))
        if s.full_name.lower() not in seen:
            seen.add(s.full_name.lower())
            signers.append(s)

    if not signers:
        signers = _signers_from_text(soup.get_text("\n"))
    return ReportData(employees=employees, signers=signers)


def _signers_from_text(text: str) -> list[Signer]:
    """Reserv: 'Anna Andersson' följt av raden 'Verkställande direktör'."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    out = []
    for i, line in enumerate(lines[1:], start=1):
        if VD_RE.search(line) and len(line) < 40 and NAME_LINE_RE.match(lines[i - 1]):
            first, *rest = lines[i - 1].split()
            out.append(Signer(first, " ".join(rest), line))
    return out


def choose_ceo(signers: list[Signer]) -> Signer | None:
    """VD i första hand, annars ordförande, annars första undertecknaren."""
    for s in signers:
        if VD_RE.search(s.role):
            return s
    for s in signers:
        if CHAIR_RE.search(s.role):
            return s
    return signers[0] if signers else None
