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
   2026-lag. Kan ersattas med riktig statistik via football-data.org
   (se `wc26bot/football_data_api.py`).
4. **Riktiga API:er**:
   - `wc26bot/odds_api.py` hamtar live 1X2-odds fran [The Odds API](https://the-odds-api.com).
   - `wc26bot/football_data_api.py` hamtar lagets senaste matcher fran
     [football-data.org](https://www.football-data.org) och raknar fram
     attack/defense-index automatiskt fran riktiga mal gjorda/insluppna.

## Installation

```bash
pip install -r requirements.txt
cp .env.example .env   # fyll i dina API-nycklar, kor sedan `export $(cat .env | xargs)`
```

API-nycklar (bada gratis att skaffa):
- `ODDS_API_KEY` fran [the-odds-api.com](https://the-odds-api.com)
- `FOOTBALL_DATA_API_KEY` fran [football-data.org](https://www.football-data.org)

## Anvandning

Med manuellt angivna odds:
```bash
python -m wc26bot Argentina Frankrike --odds 2.10 3.40 3.20 --bankroll 1000
```

Med riktiga odds fran The Odds API (kraver `ODDS_API_KEY`):
```bash
export ODDS_API_KEY=din-nyckel
python -m wc26bot Argentina Frankrike --live-odds --bankroll 1000
```

Flaggor:
- `--odds HEMMA OAVGJORT BORTA` eller `--live-odds`: en av dem kravs
- `--bankroll`: tillganglig bankrulle (default 1000)
- `--kelly-multiplier`: 0.5 = half Kelly (default, lagre risk)
- `--min-edge`: minsta edge for att rakna som value bet (default 2%)

### Hamta lagstyrka fran riktig matchdata

```python
from wc26bot.football_data_api import team_strength_from_matches

# team_id hittas via football-data.org:s /teams-endpoint
strength = team_strength_from_matches(team_id=4621, team_name="Argentina")
```

## Tester

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

API-anropen ar mockade i testerna, sa ingen riktig nyckel kravs for att kora testsviten.

## Begransningar

Modellen anvander statiska, manuellt satta lagstyrkor -- den tar inte
hansyn till skador, avstangningar, motivation eller form pa matchdagen.
Anvand som ett analysverktyg, inte en garanti. Spela ansvarsfullt.
