"""HTTP-klient som respekterar robots.txt, fordrojningar och en blocklista.

All natverkstrafik i verktyget gar via ``PoliteClient`` sa att reglerna
galler overallt:

* Domaner i ``BLOCKED_DOMAINS`` (sajter vars villkor forbjuder skrapning,
  t.ex. Hitta, Merinfo, Ratsit, Allabolag) anropas aldrig.
* For webbsidor (``respect_robots=True``) hamtas och foljs robots.txt,
  inklusive ``Crawl-delay``. Saknas robots.txt ar allt tillatet (standard),
  men om den inte gar att lasa av annat skal (5xx, timeout) hoppar vi over
  sajten for sakerhets skull.
* Minsta tid mellan anrop per doman (``min_delay``) och globalt
  (``global_delay``).
"""
from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlsplit

import requests

log = logging.getLogger(__name__)

USER_AGENT = (
    "saasleads/1.0 (+B2B-leadsverktyg; respekterar robots.txt; "
    "kontakt: se README)"
)

# Sajter vars anvandarvillkor forbjuder automatiserad insamling. Vi bygger
# bara söklänkar till Hitta, vi hamtar aldrig nagot harifran.
BLOCKED_DOMAINS = frozenset({
    "hitta.se", "merinfo.se", "ratsit.se", "allabolag.se", "eniro.se",
    "upplysning.se", "proff.se", "bolagsfakta.se", "118100.se", "mrkoll.se",
    "birthday.se", "foretagsfakta.se", "syna.se", "infotorg.se",
    "linkedin.com",
})


class BlockedError(Exception):
    """Anropet stoppades av blocklistan eller robots.txt."""


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_blocked_domain(url: str) -> bool:
    host = _host(url)
    return any(host == d or host.endswith("." + d) for d in BLOCKED_DOMAINS)


class PoliteClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        min_delay: float = 3.0,
        global_delay: float = 1.0,
        timeout: float = 15.0,
        user_agent: str = USER_AGENT,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", user_agent)
        self.user_agent = user_agent
        self.min_delay = min_delay
        self.global_delay = global_delay
        self.timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._last_by_host: dict[str, float] = {}
        self._last_any = float("-inf")
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # -- fordrojning --------------------------------------------------------
    def _wait(self, host: str, extra_delay: float = 0.0) -> None:
        now = self._clock()
        wait = max(
            self._last_any + self.global_delay - now,
            self._last_by_host.get(host, float("-inf"))
            + max(self.min_delay, extra_delay) - now,
            0.0,
        )
        if wait > 0:
            self._sleep(wait)
        now = self._clock()
        self._last_any = now
        self._last_by_host[host] = now

    # -- robots.txt ---------------------------------------------------------
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        """Returnerar parser, eller None om sajten inte ska skrapas alls."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        rp = urllib.robotparser.RobotFileParser()
        robots_url = origin + "/robots.txt"
        self._wait(_host(url))
        try:
            resp = self.session.get(robots_url, timeout=self.timeout)
        except requests.RequestException as exc:
            log.debug("robots.txt ej nabar for %s: %s", origin, exc)
            self._robots[origin] = None
            return None
        if resp.status_code in (401, 403):
            rp.disallow_all = True
        elif 400 <= resp.status_code < 500:
            rp.allow_all = True
        elif resp.status_code >= 500:
            self._robots[origin] = None
            return None
        else:
            rp.parse(resp.text.splitlines())
        self._robots[origin] = rp
        return rp

    def can_fetch(self, url: str) -> bool:
        if is_blocked_domain(url):
            return False
        rp = self._robots_for(url)
        return rp is not None and rp.can_fetch(self.user_agent, url)

    # -- publika anrop ------------------------------------------------------
    def request(self, method: str, url: str, *, respect_robots: bool = False,
                **kwargs) -> requests.Response:
        if is_blocked_domain(url):
            raise BlockedError(f"{_host(url)} star pa blocklistan")
        extra = 0.0
        if respect_robots:
            if not self.can_fetch(url):
                raise BlockedError(f"robots.txt tillater inte {url}")
            rp = self._robots.get(
                f"{urlsplit(url).scheme}://{urlsplit(url).netloc}")
            extra = float((rp and rp.crawl_delay(self.user_agent)) or 0)
        self._wait(_host(url), extra)
        kwargs.setdefault("timeout", self.timeout)
        return self.session.request(method, url, **kwargs)

    def get_page(self, url: str) -> requests.Response:
        """Hamtar en webbsida med robots.txt-kontroll."""
        return self.request("GET", url, respect_robots=True,
                            allow_redirects=True)
