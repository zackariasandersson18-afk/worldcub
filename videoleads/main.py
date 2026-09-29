"""Leadverktyg för video-ads till SaaS-företag.

Användning:
    python main.py fetch [--source producthunt|bolagsverket]
    python main.py import fil.csv
    python main.py list [--status ny] [--limit 50]

Verktyget skickar ALDRIG några meddelanden och loggar aldrig in någonstans.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from leadtool import db
from leadtool.config import load_config, load_env, resolve_path
from leadtool.sources import SOURCES
from leadtool.sources.csv_import import read_csv


def _connect(config):
    return db.connect(resolve_path(config["database"]))


def cmd_fetch(args, config) -> int:
    conn = _connect(config)
    names = [args.source] if args.source else [
        n for n in SOURCES if config["sources"].get(n, {}).get("enabled", True)
    ]
    errors = 0
    for name in names:
        print(f"→ {name} …")
        try:
            new, dupes = db.save_all(conn, SOURCES[name](config, db.known_checker(conn)))
        except Exception as exc:  # en trasig källa ska inte stoppa de andra
            errors += 1
            print(f"  ✗ {name}: {exc}")
            continue
        print(f"  ✓ {new} nya, {dupes} dubbletter")
    return 1 if errors == len(names) else 0


def cmd_import(args, config) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"Hittar inte {path}")
        return 1
    conn = _connect(config)
    new, dupes = db.save_all(conn, read_csv(path))
    print(f"Importerade {new} nya leads ({dupes} dubbletter hoppades över).")
    return 0


def cmd_list(args, config) -> int:
    conn = _connect(config)
    rows = db.list_leads(conn, args.status, args.limit)
    if not rows:
        print("Inga leads ännu. Kör `python main.py fetch` eller `import`.")
        return 0
    for r in rows:
        site = r["domain"] or r["org_nr"] or "–"
        launched = r["launch_date"] or ""
        print(f"{r['id']:>4}  {r['status']:<13} {r['source']:<12} {launched:<10}  "
              f"{r['name'][:30]:<30}  {site}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="main.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fetch", help="hämta SaaS-företag från källorna")
    f.add_argument("--source", choices=sorted(SOURCES))
    f.set_defaults(func=cmd_fetch)

    i = sub.add_parser("import", help="importera leads från en CSV-fil")
    i.add_argument("file")
    i.set_defaults(func=cmd_import)

    ls = sub.add_parser("list", help="visa sparade leads")
    ls.add_argument("--status", choices=db.STATUSES)
    ls.add_argument("--limit", type=int, default=50)
    ls.set_defaults(func=cmd_list)
    return p


def main(argv=None) -> int:
    load_env()
    config = load_config()
    args = build_parser().parse_args(argv)
    return args.func(args, config)


if __name__ == "__main__":
    sys.exit(main())
