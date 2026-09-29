# videoleads – leadverktyg för video-ads till SaaS-företag

Hittar SaaS-företag som troligen behöver en video-ad. Tar fram underlag så att du
kan göra en video i förväg, och ger dig färdiga meddelanden: *"Vi har redan gjort
en video till er – vill ni se den?"*

**Verktyget skickar aldrig något själv och loggar aldrig in någonstans.** Du
skickar alla meddelanden manuellt.

```
fetch → enrich → people → shortlist → brief → (du gör videon) → video → write → today
      → (de svarar ja) → followup med video + mötesförfrågan → möte
```

## Status

| Steg | Kommando | Klart |
|---|---|---|
| 1. Hitta SaaS-företag | `fetch`, `import`, `list` | ✅ |
| 2. Analysera hemsidan | `enrich` | – |
| 3. Beslutsfattare | `people` | – |
| 4. Välj vilka som får video | `shortlist` | – |
| 5. Produktionsunderlag | `brief <id>` | – |
| 6. Koppla videon | `video <id> <url>` | – |
| 7. Meddelanden | `write` | – |
| 8. Daglig lista | `today` | – |
| 9. Status / GDPR | `mark`, `delete` | – |

## Installation

```bash
cd videoleads
pip install -r requirements.txt
cp .env.example .env      # fyll i dina nycklar
```

Fyll i ditt namn, företag och din bokningslänk i `config.yaml`.

`.env`, `leads.db` och mapparna `data/`, `output/`, `briefs/` och `screenshots/`
ligger i `.gitignore` och checkas aldrig in.

## Användning (steg 1)

```bash
python main.py fetch                        # alla aktiva källor
python main.py fetch --source producthunt   # bara en källa
python main.py import mina_leads.csv        # manuell import
python main.py list                         # visa sparade leads
python main.py list --status ny --limit 20
```

Dubbletter hoppas över på domän, org.nr och källans eget id, så samma företag
sparas bara en gång oavsett källa.

### Källa: Product Hunt

1. Skapa ett konto och en app på <https://www.producthunt.com/v2/oauth/applications>.
2. Klicka "Create Token" (developer token) och lägg den i `.env` som `PRODUCTHUNT_TOKEN`.

Hämtar produkter som lanserats de senaste `days_back` dagarna (default 30) i
topics `saas`, `productivity`, `developer-tools`, `marketing`.

> ⚠️ **Villkor:** Product Hunts API får som standard *inte* användas kommersiellt
> utan deras godkännande. Leadgenerering för försäljning räknas troligen som
> kommersiellt. Mejla Product Hunt (hello@producthunt.com) och fråga innan du
> använder källan skarpt, eller stäng av den i `config.yaml`
> (`sources.producthunt.enabled: false`).

API:et returnerar ofta en omdirigeringslänk (`producthunt.com/r/...`) i stället för
företagets riktiga hemsida. Verktyget följer den bara om Product Hunts
robots.txt tillåter det; annars sparas leadet utan hemsida (Product Hunt-länken
sparas i `source_url`) och du får fylla i domänen själv.

### Källa: Bolagsverket (Värdefulla datamängder, kostnadsfritt)

Vad Bolagsverket faktiskt stöder:

- **API:et** slår bara upp *en* organisation per anrop via organisationsnummer.
  Det går **inte** att söka eller filtrera på SNI-kod.
- **Bulkfilen** (nedladdningsbar, uppdateras veckovis) innehåller alla registrerade
  företag. Det är den vi filtrerar i.
- Varken API:et eller bulkfilen innehåller företagets **hemsida**. Bolagsverket-leads
  saknar därför domän tills du (eller ett senare steg) hittar den.

Så gör du:

1. Ladda ner bulkfilen från Bolagsverkets sida *API:er och öppna data →
   Värdefulla datamängder → Nedladdningsbara filer* och lägg den som
   `data/bolagsverket_bolagsdata.zip` (eller ändra `bulk_file` i `config.yaml`).
2. (Valfritt men rekommenderat) Ansök om inloggning till API:et för värdefulla
   datamängder på bolagsverket.se och lägg `BOLAGSVERKET_CLIENT_ID` och
   `BOLAGSVERKET_CLIENT_SECRET` i `.env`.

Filtrering:

- Bara aktiebolag (`only_aktiebolag: true`) som inte är avregistrerade.
- Har filen en SNI-kolumn filtreras direkt på SNI 62.010 och 58.290.
- Saknas SNI-kolumn filtreras på nyckelord i verksamhetsbeskrivningen
  (programvara, mjukvara, SaaS …). Om API-nycklar finns verifieras sedan varje
  kandidats SNI-kod via API:et – max `max_new_per_run` leads per körning.

> Obs: bulkfilens exakta kolumner och API:ets svarsformat är byggda efter
> Bolagsverkets dokumentation men inte provkörda mot skarpa data än. Kolumnnamn
> hittas automatiskt; om något inte matchar, säg till så justerar vi.

### Manuell import (CSV)

Komma- eller semikolonseparerad. Svenska eller engelska kolumnnamn fungerar:

```csv
företagsnamn;hemsida;beskrivning;lanseringsdatum;org.nr
Acme;acme.se;Fakturering för frilansare;2026-09-01;556123-4567
```

Minst namn eller hemsida krävs.

## Lägga till en ny källa

Skapa `leadtool/sources/min_kalla.py` med en funktion
`fetch(config, is_known=None)` som ger `Lead`-objekt, och registrera den i
`leadtool/sources/__init__.py`.

## Tester

```bash
python -m pytest videoleads/tests
```

## Regler

- Inga automatiska utskick eller inloggningar på någon plattform.
- Inga sidor hämtas från linkedin.com, och robots.txt respekteras alltid.
- Bara företags- och yrkesrelaterad data sparas.
- Deras logotyp och material används bara i videon som visas för dem själva.
- Allt håller sig inom gratiskvoter; verktyget frågar innan något kan kosta pengar.
