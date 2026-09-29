"""SQLite-lagring av leads. Dubbletter stoppas på domän, org.nr och källreferens."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATUSES = [
    "ny", "video_klar", "kontaktad", "svarat_ja", "video_skickad",
    "möte_bokat", "möte_klart", "kund", "nej",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    website      TEXT,
    domain       TEXT,
    source       TEXT NOT NULL,
    source_ref   TEXT,
    source_url   TEXT,
    launch_date  TEXT,
    description  TEXT,
    country      TEXT,
    org_nr       TEXT,
    status       TEXT NOT NULL DEFAULT 'ny',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_domain ON leads(domain) WHERE domain IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_org_nr ON leads(org_nr) WHERE org_nr IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_source_ref ON leads(source, source_ref) WHERE source_ref IS NOT NULL;
"""


@dataclass
class Lead:
    name: str
    source: str
    website: str | None = None
    domain: str | None = None
    source_ref: str | None = None
    source_url: str | None = None
    launch_date: str | None = None
    description: str | None = None
    country: str | None = None
    org_nr: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def is_duplicate(conn: sqlite3.Connection, lead: Lead) -> bool:
    checks = []
    if lead.domain:
        checks.append(("domain = ?", (lead.domain,)))
    if lead.org_nr:
        checks.append(("org_nr = ?", (lead.org_nr,)))
    if lead.source_ref:
        checks.append(("source = ? AND source_ref = ?", (lead.source, lead.source_ref)))
    for where, params in checks:
        if conn.execute(f"SELECT 1 FROM leads WHERE {where}", params).fetchone():
            return True
    return False


def known_checker(conn: sqlite3.Connection):
    """Returnerar en funktion som källor kan använda för att hoppa över kända leads
    innan de gör dyra anrop (t.ex. `is_known(org_nr="5561234567")`)."""
    def is_known(source: str = "", source_ref=None, domain=None, org_nr=None) -> bool:
        return is_duplicate(conn, Lead(name="", source=source, source_ref=source_ref,
                                       domain=domain, org_nr=org_nr))
    return is_known


def insert_lead(conn: sqlite3.Connection, lead: Lead) -> int | None:
    """Sparar leadet. Returnerar id, eller None om det var en dubblett."""
    if is_duplicate(conn, lead):
        return None
    now = _now()
    cur = conn.execute(
        """INSERT INTO leads (name, website, domain, source, source_ref, source_url, launch_date,
                              description, country, org_nr, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ny', ?, ?)""",
        (lead.name, lead.website, lead.domain, lead.source, lead.source_ref, lead.source_url,
         lead.launch_date, lead.description, lead.country, lead.org_nr, now, now),
    )
    conn.commit()
    return cur.lastrowid


def save_all(conn: sqlite3.Connection, leads) -> tuple[int, int]:
    """Sparar en ström av leads. Returnerar (nya, dubbletter)."""
    new = dupes = 0
    for lead in leads:
        if insert_lead(conn, lead) is None:
            dupes += 1
        else:
            new += 1
    return new, dupes


def list_leads(conn: sqlite3.Connection, status: str | None = None, limit: int = 50):
    if status:
        return conn.execute(
            "SELECT * FROM leads WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
