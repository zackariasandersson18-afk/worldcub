"""Berikar och filtrerar kandidater till leads."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import saas
from .ixbrl import choose_ceo
from .models import Lead

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, bolagsverket=None, website_finder=None,
                 min_employees: int = 2, max_employees: int = 50,
                 saas_threshold: int = saas.DEFAULT_THRESHOLD,
                 cache_dir: str | None = None):
        self.bv = bolagsverket
        self.web = website_finder
        self.min_employees = min_employees
        self.max_employees = max_employees
        self.saas_threshold = saas_threshold
        self.cache = Path(cache_dir) if cache_dir else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)

    # -- cache: gör att en full körning återanvänder testkörningen -----------
    def _cached(self, orgnr: str) -> Lead | None:
        if self.cache and (p := self.cache / f"{orgnr}.json").exists():
            return Lead.from_dict(json.loads(p.read_text(encoding="utf-8")))
        return None

    def _store(self, lead: Lead) -> None:
        if self.cache:
            (self.cache / f"{lead.orgnr}.json").write_text(
                json.dumps(lead.to_dict(), ensure_ascii=False, indent=1),
                encoding="utf-8")

    # -- steg ------------------------------------------------------------------
    def _enrich_bolagsverket(self, lead: Lead) -> None:
        if not self.bv:
            return
        info = self.bv.organisation(lead.orgnr)
        if info:
            lead.name = lead.name or info["name"]
            lead.city = lead.city or info["city"]
            lead.sni_codes = lead.sni_codes or info["sni_codes"]
            lead.business_description = info["business_description"]
            lead.sources.append("Bolagsverket")
        report = self.bv.latest_annual_report(lead.orgnr)
        if not report:
            lead.notes.append("Ingen digital årsredovisning")
            return
        period = f"årsredovisning {report.period_end}".strip()
        if report.employees is not None:
            lead.employees = report.employees
            lead.employees_source = period
        ceo = choose_ceo(report.signers)
        if ceo:
            lead.ceo_first_name, lead.ceo_last_name = ceo.first_name, ceo.last_name
            lead.ceo_role = ceo.role
            lead.ceo_source = period

    def _check_size(self, lead: Lead) -> bool:
        if lead.employees is None:
            lead.notes.append("Antal anställda okänt – bara SCB:s storleksklass")
            return True
        if not self.min_employees <= lead.employees <= self.max_employees:
            lead.status = "filtrerad"
            lead.reason = (f"{lead.employees} anställda (utanför "
                           f"{self.min_employees}–{self.max_employees})")
            return False
        return True

    def _check_saas(self, lead: Lead) -> None:
        html = ""
        if self.web:
            url, conf, html = self.web.find(lead.name, lead.orgnr, lead.website)
            lead.website = url or lead.website
            lead.website_confidence = conf
        texts = [lead.business_description]
        if html:
            texts += [saas.page_text(html)]
            pricing = self.web.pricing_page(lead.website, html)
            if pricing:
                texts.append(saas.page_text(pricing))
        score, hits, is_saas = saas.assess(texts, self.saas_threshold)
        lead.saas_score, lead.saas_keywords, lead.is_saas = score, hits, is_saas
        if not html:
            lead.status = "granska"
            lead.reason = "Hittade ingen verifierad hemsida – bedöm manuellt"
        elif is_saas:
            lead.status = "lead"
        else:
            lead.status = "filtrerad"
            lead.reason = f"Verkar inte vara SaaS (poäng {score})"

    def process(self, lead: Lead) -> Lead:
        cached = self._cached(lead.orgnr)
        if cached:
            return cached
        try:
            self._enrich_bolagsverket(lead)
            if self._check_size(lead):
                self._check_saas(lead)
        except Exception as exc:  # ett trasigt bolag ska inte stoppa körningen
            log.exception("Fel för %s", lead.orgnr)
            lead.status = "granska"
            lead.reason = f"Fel vid hämtning: {exc}"
            return lead  # cacha inte fel, så att nästa körning försöker igen
        self._store(lead)
        return lead
