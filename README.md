# wc26bot

Bot for att berakna mest sannolika matchutfall, hitta value bets och
foresla insatsstorlek (Kelly criterion) for fotbollsmatcher, med fokus pa
VM 2026.

## Hur det funkar

1. **Sannolikhetsmodell** (`wc26bot/model.py`): Poisson-baserad modell som
   utgar fran lagens relativa anfalls- och forsvarsstyrka och raknar ut
   sannolikheter for 1X2, over/under 2.5 mal och bada lag gor mal.
2. **Value bet & riskhantering** (`wc26bot/betting.py`): jamfor modellens
   sannolikheter mot bookmakerns odds, raknar edge och expected value (EV),
   och foreslar insats via fraktionerad Kelly criterion (default half-Kelly
   + ett absolut tak per satsning) for att maximera langsiktig vinst samtidigt
   som nedsidan vid enskilda forluster begransas.
3. **Lagdata** (`wc26bot/data/wc26_teams.py`): exempelvarden for VM
   2026-lag. Byt ut mot riktig statistik (mal gjorda/insluppna per match,
   senaste formen, kvalificeringsresultat) for battre precision.

## Anvandning

```bash
python -m wc26bot Argentina Frankrike --odds 2.10 3.40 3.20 --bankroll 1000
```

Flaggor:
- `--odds HEMMA OAVGJORT BORTA`: bookmakerns decimalodds (kravs)
- `--bankroll`: tillganglig bankrulle (default 1000)
- `--kelly-multiplier`: 0.5 = half Kelly (default, lagre risk)
- `--min-edge`: minsta edge for att rakna som value bet (default 2%)

## Tester

```bash
pip install pytest
python -m pytest tests/ -q
```

## Begransningar

Modellen anvander statiska, manuellt satta lagstyrkor -- den tar inte
hansyn till skador, avstangningar, motivation eller form pa matchdagen.
Anvand som ett analysverktyg, inte en garanti. Spela ansvarsfullt.
