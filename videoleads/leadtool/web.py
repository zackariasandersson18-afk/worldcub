"""HTTP-hjälpare: domännormalisering och robots.txt-kontroll."""
from __future__ import annotations

from functools import lru_cache
from urllib import robotparser
from urllib.parse import urlparse

import requests

# Sidor vi aldrig hämtar, oavsett robots.txt (se reglerna i README).
BLOCKED_HOSTS = ("linkedin.com",)


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if "://" not in url:
        url = "https://" + url
    return url


def domain_of(url: str | None) -> str | None:
    url = normalize_url(url)
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def is_blocked_host(url: str) -> bool:
    host = domain_of(url) or ""
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


@lru_cache(maxsize=512)
def _robots_for(origin: str, user_agent: str, timeout: float):
    rp = robotparser.RobotFileParser()
    try:
        resp = requests.get(origin + "/robots.txt", timeout=timeout,
                            headers={"User-Agent": user_agent})
    except requests.RequestException:
        return None  # okänt -> behandlas som "får ej" av can_fetch
    if resp.status_code in (401, 403):
        rp.disallow_all = True
    elif resp.status_code >= 400:
        rp.allow_all = True
    else:
        rp.parse(resp.text.splitlines())
    return rp


def can_fetch(url: str, user_agent: str, timeout: float = 10) -> bool:
    """True om robots.txt tillåter att vi hämtar url. Osäkert svar = nej."""
    if is_blocked_host(url):
        return False
    parsed = urlparse(normalize_url(url))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    rp = _robots_for(origin, user_agent, timeout)
    if rp is None:
        return False
    return rp.can_fetch(user_agent, url)
