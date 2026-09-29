"""Laddar config.yaml och .env."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULTS = {
    "market": {
        "countries": ["SE", "NO", "DK", "FI"],
        "include_global_launches": True,
    },
    "videos_per_day": 5,
    "my_name": "",
    "my_company": "",
    "booking_link": "",
    "link_in_first_message": False,
    "database": "leads.db",
    "sources": {
        "producthunt": {
            "enabled": True,
            "days_back": 30,
            "topics": ["saas", "productivity", "developer-tools", "marketing"],
            "max_pages_per_topic": 3,
        },
        "bolagsverket": {
            "enabled": True,
            "bulk_file": "data/bolagsverket_bolagsdata.txt",
            "sni_codes": ["62010", "58290"],
            "only_aktiebolag": True,
            "max_new_per_run": 200,
        },
    },
    "http": {
        "user_agent": "videoleads/0.1 (+kontakt via config)",
        "timeout": 15,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_env(path: Path | None = None) -> None:
    """Minimal .env-läsare (KEY=VALUE per rad). Skriver aldrig över befintliga variabler."""
    path = path or BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: Path | None = None) -> dict:
    path = path or BASE_DIR / "config.yaml"
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _deep_merge(DEFAULTS, data)


def resolve_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else BASE_DIR / path
