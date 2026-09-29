"""Leadkällor. Varje källa är en modul med en funktion `fetch(config) -> Iterable[Lead]`.

Lägg till en ny källa genom att skapa en modul här och registrera den i SOURCES.
"""
from . import bolagsverket, producthunt

SOURCES = {
    "producthunt": producthunt.fetch,
    "bolagsverket": bolagsverket.fetch,
}
