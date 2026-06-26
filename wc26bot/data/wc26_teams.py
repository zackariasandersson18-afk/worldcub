"""Exempel pa lagstyrkor for VM 2026-bot. Justera attack/defense med riktiga
malstatistik (t.ex. fran kvalspel och senaste landslagsmatcher) for battre
precision -- varderna nedan ar illustrativa utgangspunkter, inte facit.

attack/defense ar relativa indextal dar 1.0 = genomsnittlig VM-deltagare.
Hogre attack = fler mal gjorda. Lagre defense = farre mal insluppna (defense
anvands som multiplikator pa motstandarens forvantade mal, sa < 1.0 ar bra
forsvar och > 1.0 ar svagare forsvar).
"""
from wc26bot.model import TeamStrength

WC26_TEAMS: dict[str, TeamStrength] = {
    "Argentina": TeamStrength("Argentina", attack=1.45, defense=0.75),
    "Frankrike": TeamStrength("Frankrike", attack=1.50, defense=0.80),
    "Brasilien": TeamStrength("Brasilien", attack=1.55, defense=0.85),
    "England": TeamStrength("England", attack=1.35, defense=0.85),
    "Spanien": TeamStrength("Spanien", attack=1.40, defense=0.80),
    "Portugal": TeamStrength("Portugal", attack=1.30, defense=0.90),
    "Tyskland": TeamStrength("Tyskland", attack=1.30, defense=0.90),
    "Nederlanderna": TeamStrength("Nederlanderna", attack=1.25, defense=0.90),
    "Belgien": TeamStrength("Belgien", attack=1.20, defense=0.95),
    "Italien": TeamStrength("Italien", attack=1.15, defense=0.85),
    "USA": TeamStrength("USA", attack=1.05, defense=1.00),
    "Mexiko": TeamStrength("Mexiko", attack=1.05, defense=1.00),
    "Kanada": TeamStrength("Kanada", attack=0.95, defense=1.05),
    "Marocko": TeamStrength("Marocko", attack=1.05, defense=0.90),
    "Japan": TeamStrength("Japan", attack=1.05, defense=0.95),
    "Sydkorea": TeamStrength("Sydkorea", attack=1.00, defense=1.00),
    "Kroatien": TeamStrength("Kroatien", attack=1.10, defense=0.95),
    "Uruguay": TeamStrength("Uruguay", attack=1.10, defense=0.90),
    "Colombia": TeamStrength("Colombia", attack=1.10, defense=0.95),
    "Senegal": TeamStrength("Senegal", attack=1.00, defense=1.00),
    "Danmark": TeamStrength("Danmark", attack=1.05, defense=0.95),
    "Schweiz": TeamStrength("Schweiz", attack=1.00, defense=1.00),
    "Polen": TeamStrength("Polen", attack=0.95, defense=1.05),
    "Australien": TeamStrength("Australien", attack=0.90, defense=1.05),
}
