"""Kommandorad.

    python -m saasleads run --limit 20            # testkörning
    python -m saasleads run                       # hela listan
    python -m saasleads scb-kategorier            # visa SCB:s kategorier/koder

Miljövariabler:
    SCB_CERT                     sökväg till SCB-certifikat i PEM-format
    BOLAGSVERKET_CLIENT_ID       från portal.api.bolagsverket.se
    BOLAGSVERKET_CLIENT_SECRET
"""
from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import random
import sys

from . import saas, scb, seedtable
from .bolagsverket import BolagsverketClient
from .excel import write_workbook
from .models import Lead
from .pipeline import Pipeline
from .polite import PoliteClient
from .website import WebsiteFinder

log = logging.getLogger("saasleads")


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"Miljövariabeln {name} saknas – se README_SAASLEADS.md")
    return value


def _scb_client(args, http) -> scb.SCBClient:
    return scb.SCBClient(http, _require_env("SCB_CERT"),
                         sni_category=args.scb_sni_category,
                         size_category=args.scb_size_category,
                         lan_category=args.scb_lan_category)


def cmd_categories(args) -> None:
    http = PoliteClient(min_delay=1.0)
    print(json.dumps(_scb_client(args, http).categories(),
                     ensure_ascii=False, indent=1))


def _candidates(args, api_http, web_http, scb_client):
    seen: set[str] = set()
    for lead in scb_client.iter_companies(args.sni, args.size_classes):
        seen.add(lead.orgnr)
        yield lead
    if args.seedtable:
        for company in seedtable.fetch_companies(web_http):
            matches = scb_client.search_by_name(company["name"])
            if len(matches) != 1:  # tvetydigt eller okänt namn -> hoppa över
                continue
            lead = matches[0]
            if lead.orgnr not in seen:
                seen.add(lead.orgnr)
                lead.website = company["website"]
                lead.sources.append("Seedtable")
                yield lead


def cmd_run(args) -> None:
    api_http = PoliteClient(min_delay=args.api_delay, global_delay=0.2)
    web_http = PoliteClient(min_delay=args.web_delay, global_delay=1.0)
    scb_client = _scb_client(args, api_http)
    bv = BolagsverketClient(api_http, _require_env("BOLAGSVERKET_CLIENT_ID"),
                            _require_env("BOLAGSVERKET_CLIENT_SECRET"))
    pipeline = Pipeline(bv, WebsiteFinder(web_http),
                        min_employees=args.min_employees,
                        max_employees=args.max_employees,
                        saas_threshold=args.saas_threshold,
                        cache_dir=args.cache_dir)

    candidates = _candidates(args, api_http, web_http, scb_client)
    if args.shuffle:
        candidates = list(candidates)
        random.Random(args.seed).shuffle(candidates)
    if args.limit:
        candidates = itertools.islice(candidates, args.limit)

    results: list[Lead] = []
    for i, lead in enumerate(candidates, start=1):
        lead = pipeline.process(lead)
        results.append(lead)
        log.info("[%d] %s (%s): %s %s", i, lead.name, lead.orgnr,
                 lead.status, lead.reason)
        if i % 25 == 0:  # spara löpande vid långa körningar
            write_workbook(args.out, results, _meta(args))

    write_workbook(args.out, results, _meta(args))
    counts = {s: sum(l.status == s for l in results)
              for s in ("lead", "granska", "filtrerad")}
    print(f"Klar: {len(results)} bolag -> {args.out} "
          f"({counts['lead']} leads, {counts['granska']} att granska, "
          f"{counts['filtrerad']} bortfiltrerade)")


def _meta(args) -> dict:
    return {
        "Källor": "SCB företagsregister, Bolagsverket (värdefulla datamängder, "
                  "digitala årsredovisningar)" + (", Seedtable" if args.seedtable else ""),
        "SNI": ", ".join(args.sni),
        "SCB storleksklasser": ", ".join(args.size_classes),
        "Anställda": f"{args.min_employees}–{args.max_employees}",
        "SaaS-tröskel": args.saas_threshold,
        "Antal bolag (gräns)": args.limit or "alla",
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="saasleads", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--scb-sni-category", default="Bransch")
    parser.add_argument("--scb-size-category", default="Storleksklass")
    parser.add_argument("--scb-lan-category", default="Säteslän")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scb-kategorier", help="lista SCB:s kategorier och koder")

    run = sub.add_parser("run", help="hämta, berika och filtrera leads")
    run.add_argument("--limit", type=int, default=0,
                     help="antal bolag att behandla (0 = alla)")
    run.add_argument("--out", default="leads.xlsx")
    run.add_argument("--sni", nargs="+", default=scb.DEFAULT_SNI)
    run.add_argument("--size-classes", nargs="+", default=scb.DEFAULT_SIZE_CLASSES)
    run.add_argument("--min-employees", type=int, default=2)
    run.add_argument("--max-employees", type=int, default=50)
    run.add_argument("--saas-threshold", type=int, default=saas.DEFAULT_THRESHOLD)
    run.add_argument("--seedtable", action="store_true",
                     help="komplettera med Seedtable (om robots.txt tillåter)")
    run.add_argument("--shuffle", action="store_true",
                     help="slumpa ordningen (bra för ett representativt test)")
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--api-delay", type=float, default=1.0,
                     help="sekunder mellan anrop till SCB/Bolagsverket")
    run.add_argument("--web-delay", type=float, default=3.0,
                     help="minsta sekunder mellan anrop till samma webbplats")
    run.add_argument("--cache-dir", default=".saasleads_cache")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    {"scb-kategorier": cmd_categories, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
