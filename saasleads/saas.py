"""Enkel nyckelordsbaserad bedömning av om ett bolag säljer SaaS."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

# (mönster, vikt, etikett). Varje etikett räknas högst en gång.
POSITIVE = [
    (r"prenumeration\w*|subscription\w*|abonnemang\w*", 2, "prenumeration"),
    (r"pricing|prisplan\w*|priser|price plans?|v[åa]ra planer", 2, "pricing"),
    (r"plattform\w*|platform\w*", 1, "plattform"),
    (r"gratis provperiod|provperiod|free trial|prova gratis|testa gratis|"
     r"try (?:it )?(?:for )?free|start (?:for )?free|kom ig[åa]ng gratis",
     3, "gratis provperiod"),
    (r"\bsaas\b|software as a service|programvara som tj[äa]nst|"
     r"molntj[äa]nst\w*|molnbaserad\w*|cloud[- ]based", 3, "saas/moln"),
    (r"boka (?:en )?demo|book a demo|request a demo|beg[äa]r demo|get a demo",
     2, "boka demo"),
    (r"per m[åa]nad|/\s?m[åa]n\b|kr/m[åa]n|per month|/\s?month|/\s?mo\b|"
     r"per anv[äa]ndare|per user|per seat", 2, "månadspris"),
    (r"logga in|log in|login|sign in|sign up|skapa konto|create account|"
     r"registrera dig", 1, "inloggning"),
    (r"\bapi\b|integrationer|integrations", 1, "api/integrationer"),
]
NEGATIVE = [
    (r"konsult\w*|consulting|consultants?", -3, "konsult"),
    (r"bemanning\w*|rekrytering\w*|staffing|recruitment", -3, "bemanning"),
    (r"webbyr[åa]\w*|digitalbyr[åa]\w*|reklambyr[åa]\w*|\bagency\b", -2, "byrå"),
    (r"uppdragsutveckling|skr[äa]ddarsy\w+|tailor[- ]made|"
     r"custom software development|timpris|per timme", -2, "uppdrag"),
]
_COMPILED = [(re.compile(p, re.I), w, lbl) for p, w, lbl in POSITIVE + NEGATIVE]

DEFAULT_THRESHOLD = 4


def page_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    parts = [soup.get_text(" ", strip=True)]
    for meta in soup.find_all("meta", attrs={"name": re.compile("description", re.I)}):
        parts.append(meta.get("content", ""))
    # Länkar som /pricing säger också något även när texten är en ikon.
    parts += [a.get("href", "") for a in soup.find_all("a")]
    return " ".join(parts)


def score_text(text: str) -> tuple[int, list[str]]:
    score, hits = 0, []
    for rx, weight, label in _COMPILED:
        if rx.search(text):
            score += weight
            hits.append(label if weight > 0 else f"-{label}")
    return score, hits


def assess(texts: list[str], threshold: int = DEFAULT_THRESHOLD):
    """Returnerar (poäng, träffar, is_saas) för sammanslagen text."""
    score, hits = score_text("\n".join(t for t in texts if t))
    return score, hits, score >= threshold
