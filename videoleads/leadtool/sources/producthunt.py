"""Product Hunt API v2 (GraphQL).

Kräver en developer token: https://www.producthunt.com/v2/oauth/applications
-> skapa en app -> "Create Token". Lägg den i .env som PRODUCTHUNT_TOKEN.

OBS: Product Hunts API-villkor tillåter som standard inte kommersiell användning
utan deras godkännande. Se README.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import requests

from ..db import Lead
from ..web import can_fetch, domain_of

API_URL = "https://api.producthunt.com/v2/api/graphql"

QUERY = """
query($topic: String, $postedAfter: DateTime, $after: String) {
  posts(first: 20, topic: $topic, postedAfter: $postedAfter, after: $after, order: NEWEST) {
    pageInfo { hasNextPage endCursor }
    edges { node { id name tagline description url website createdAt featuredAt } }
  }
}
"""


class ProductHuntError(RuntimeError):
    pass


def _post(token: str, variables: dict, timeout: float) -> dict:
    resp = requests.post(
        API_URL,
        json={"query": QUERY, "variables": variables},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=timeout,
    )
    if resp.status_code == 429:
        raise ProductHuntError("Product Hunt: rate limit nådd, försök igen om 15 minuter.")
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise ProductHuntError(f"Product Hunt: {data['errors']}")
    return data["data"]["posts"]


def resolve_website(url: str | None, user_agent: str, timeout: float) -> str | None:
    """Product Hunt returnerar ofta en omdirigeringslänk (producthunt.com/r/...).
    Vi följer den bara om robots.txt tillåter det, och läser då bara Location-headern."""
    if not url:
        return None
    if domain_of(url) != "producthunt.com":
        return url
    if not can_fetch(url, user_agent, timeout):
        return None
    try:
        resp = requests.head(url, allow_redirects=False, timeout=timeout,
                             headers={"User-Agent": user_agent})
    except requests.RequestException:
        return None
    location = resp.headers.get("Location")
    if location and domain_of(location) != "producthunt.com":
        return location.split("?")[0]
    return None


def fetch(config: dict, is_known=None):
    cfg = config["sources"]["producthunt"]
    token = os.environ.get("PRODUCTHUNT_TOKEN")
    if not token:
        raise ProductHuntError("PRODUCTHUNT_TOKEN saknas i .env")
    http = config["http"]
    since = datetime.now(timezone.utc) - timedelta(days=int(cfg["days_back"]))
    seen: set[str] = set()

    for topic in cfg["topics"]:
        after = None
        for _ in range(int(cfg["max_pages_per_topic"])):
            posts = _post(token, {"topic": topic, "postedAfter": since.isoformat(),
                                  "after": after}, http["timeout"])
            for edge in posts["edges"]:
                node = edge["node"]
                if node["id"] in seen:
                    continue
                seen.add(node["id"])
                if is_known and is_known(source="producthunt", source_ref=str(node["id"])):
                    continue
                website = resolve_website(node.get("website"), http["user_agent"], http["timeout"])
                launched = (node.get("featuredAt") or node.get("createdAt") or "")[:10] or None
                desc = node.get("tagline") or ""
                if node.get("description"):
                    desc = f"{desc} – {node['description']}" if desc else node["description"]
                yield Lead(
                    name=node["name"],
                    source="producthunt",
                    source_ref=str(node["id"]),
                    source_url=node.get("url"),
                    website=website,
                    domain=domain_of(website),
                    launch_date=launched,
                    description=desc[:500] or None,
                )
            if not posts["pageInfo"]["hasNextPage"]:
                break
            after = posts["pageInfo"]["endCursor"]
