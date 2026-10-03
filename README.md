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

---

# tradebot

Tradingbot for crypto (default) byggd efter principen *"Generation is free
now. Validation is the job."* Botens jobb ar att **avvisa** strategier som
inte haller, inte att hitta den snyggaste equity-kurvan.

## Fem roller, tre grindar

| Roll | Modul | Uppgift |
|---|---|---|
| Hypotes | `tradebot/strategies.py`, `prompts.py` | Mekanism, inte monster. Exempel: time-series momentum med namngiven motpart |
| Kod | `tradebot/engine.py` | Vektoriserad backtest: `signal.shift(1)`, avgift + slippage pa turnover |
| Kritiker | `tradebot/critic.py` | Atta-punktslistan (PRESENT/ABSENT + citerade rader) och ett korbart lackagetest |
| Statistiker | `tradebot/stats.py` | Deflated Sharpe for N forsok, walk-forward |
| Risk | `tradebot/sizing.py`, `monitor.py` | Positionsstorlek fran stop, daglig health check med kill-villkor |

En strategi godkanns bara om alla tre grindarna (`tradebot/gates.py`) passerar:

1. **Kritikern hittar inget lackage.** `leakage_test` kor signalen pa
   trunkerad data och pa data dar framtiden bytts ut; om signalen vid tid t
   andras anvander den framtida data. Dessutom far statisk granskning inte
   hitta look-ahead, repainting, saknade kostnader eller fel tidszon.
2. **Deflated Sharpe > 0.95** pa de sammanfogade out-of-sample-avkastningarna.
3. **Walk-forward:** minst 60 % positiva fold, medel-Sharpe > 0 och samsta
   fold >= -2 (justerbart med flaggor).

## Anvandning

```bash
pip install -r requirements.txt

python -m tradebot demo                                  # hela kedjan pa syntetisk data
python -m tradebot run --csv btc_daily.csv --n-trials 25 # egen data (kolumner timestamp,close)
python -m tradebot run --binance BTCUSDT --start 2019-01-01
python -m tradebot size --capital 10000 --entry 60000 --stop 57000
python -m tradebot dsr --sharpe 1.8 --n-trials 80 --n-obs 1095
python -m tradebot health --returns live.csv --sharpe 1.2 --max-dd -25   # exit 2 = HALT
python -m tradebot prompt critic                         # promptarna for LLM-rollerna
```

`--n-trials` ska vara **alla** varianter du provat medan du forskat pa
iden. Varje parameterjustering ar ett forsok. Att ljuga for den har siffran
lurar bara dig sjalv.

Tidsstamplar normaliseras till UTC och satts vid barens **stangning**, sa att
ett varde vid tid t var kant vid t. Binance-data hamtas utan nyckel och bara
stangda barer tas med.

## Avvikelser fran originalkoden i dokumentet

Fem saker i originalkoden ar fixade, eftersom de annars ger fel svar:

- **Deflated Sharpe** raknas pa Sharpe per period, inte arsvis. Originalet
  blandade arsvis Sharpe med antal dagliga observationer, och da gar nastan
  allt over ~2.5 igenom oavsett datamangd. Ett test simulerar brus och
  verifierar troskeln.
- **Enkel avkastning** i stallet for log-avkastning: `position * logreturn`
  blir fel for blankning och havstang.
- **Walk-forward:** testfonstren pa 60 dagar fick `insufficient_data`
  (kravet var 100 observationer), sa `min_obs` ar nu en parameter. Signalen
  far ocksa se historiken fore testfonstret, sa att indikatorerna hinner
  varmas upp. Lackagetestet ser till att den inte ser framat.
- `fee_bps` dras per sida, pa varje enhet turnover.
- **Drawdown** raknar nu startkapitalet som en topp. Annars missades en
  forlust redan i forsta perioden, bade i `metrics` och i `health_check`.

## Handla pa papper eller Binance testnet

Orderlaggning finns for tva konton: ett lokalt **paper**-konto (en JSON-fil,
samma avgifter och slippage som backtesten) och **Binance Spot Testnet**
(lekpengar). Riktiga pengar stods medvetet inte: testnet-brokern vagrar alla
URL:er som inte ar testnet.

```bash
# 1. Validera och spara godkannandet (parametrar + OOS-matt for kill-villkoret)
python -m tradebot run --binance BTCUSDT --n-trials 25 --save-approval approval.json

# 2. Kor en gang per stangd bar, t.ex. via cron strax efter 00:00 UTC
python -m tradebot trade --approval approval.json --broker paper            # dry run
python -m tradebot trade --approval approval.json --broker paper --execute
python -m tradebot trade --approval approval.json --broker testnet --execute
```

For testnet: skapa nycklar pa https://testnet.binance.vision och satt
`BINANCE_TESTNET_API_KEY` och `BINANCE_TESTNET_API_SECRET`.

Varje `trade`-steg (`tradebot/live.py`):

1. **Vagrar** om strategin inte passerat alla tre grindarna. `--ignore-gates`
   finns for att prova flodet pa testnet/paper, med en tydlig varning.
2. **Vagrar** gammal data (senaste bar aldre an 2 dagar) och hoppar over en
   bar som redan hanterats, sa att dubbla cron-korningar inte dubbelhandlar.
3. Kor **health check**. Vid HALT saljs positionen och boten stannar for gott
   (`halted` i state-filen). Kill-villkoret bestams i forvag: 30-perioders
   Sharpe under halften av backtestens, eller drawdown over 1,5 ganger
   backtestens. Live-avkastningen raknas per enhet exponering, sa att den gar
   att jamfora med backtesten som kor 100 %.
4. Raknar signalen pa **stangda** barer (bara long, eftersom det ar ett
   spotkonto).
5. **Positionsstorlek:** stop = pris - 2 x daglig volatilitet. 1 % av kapitalet
   riskeras om stoppen nas, med ett tak pa 20 % av kapitalet.
6. Skickar hogst en marknadsorder. Kvantiteten rundas nedat till borsens
   stepSize, och ordern hoppas over under minNotional. Storleken justeras bara
   om den avviker mer an 25 % fran malet, for att undvika onodig handel.

Signalen beraknas pa riktig marknadsdata (publika api.binance.com). Ordrar
gar till testnet, dar priserna kan skilja sig fran den riktiga marknaden.

## Daglig korning pa demokonto (GitHub Actions)

`.github/workflows/tradebot.yml` kor `scripts/tradebot_daily.sh` varje dag
kl 00:07 UTC, strax efter att dagens bar stangt. Den gor ett `trade`-steg
pa BTCUSDT.

- **Konto:** Binance testnet om repo-hemligheterna
  `BINANCE_TESTNET_API_KEY` och `BINANCE_TESTNET_API_SECRET` finns,
  annars paper-kontot (10 000 USDT i lekpengar).
- **State** (godkannande, planbok, historik, `runs.log`) sparas pa grenen
  `tradebot-state`, en commit per korning.
- **Avisering:** jobbet failar vid HALT eller vagran, och da skickar GitHub
  ett mejl.
- **Manuell korning:** Actions -> "tradebot daily (demo account)" -> Run
  workflow. Valj `revalidate` for att kora om grindarna.
- `IGNORE_GATES: 'true'` star i workflowen eftersom exempelstrategin inte
  klarar grindarna. Det ar bara for demo. Ta bort raden nar du har en
  strategi som klarar dem.

Schemalagda workflows kor bara fran repots default-gren.

## Begransningar

Inget har ar finansiell radgivning. Kor pa testnet lange innan du ens
funderar pa riktiga pengar, och da kravs en egen, granskad kodandring.
