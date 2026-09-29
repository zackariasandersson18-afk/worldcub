from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


def normalize_orgnr(value: str) -> str:
    """'16556123-4567' / '556123-4567' -> '5561234567'."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 12 and digits.startswith("16"):
        digits = digits[2:]
    return digits


def format_orgnr(orgnr: str) -> str:
    d = normalize_orgnr(orgnr)
    return f"{d[:6]}-{d[6:]}" if len(d) == 10 else d


@dataclass
class Lead:
    orgnr: str
    name: str
    city: str = ""
    sni_codes: list[str] = field(default_factory=list)
    size_class: str = ""              # SCB:s storleksklass, t.ex. "5-9 anställda"
    employees: int | None = None      # medelantal anställda från årsredovisning
    employees_source: str = ""
    business_description: str = ""
    website: str = ""
    website_confidence: str = ""      # "orgnr", "namn", "seedtable", ""
    ceo_first_name: str = ""
    ceo_last_name: str = ""
    ceo_role: str = ""                # t.ex. "Verkställande direktör"
    ceo_source: str = ""
    saas_score: int = 0
    saas_keywords: list[str] = field(default_factory=list)
    is_saas: bool | None = None
    status: str = ""                  # "lead", "filtrerad" eller "granska"
    reason: str = ""
    sources: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.orgnr = normalize_orgnr(self.orgnr)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Lead":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})
