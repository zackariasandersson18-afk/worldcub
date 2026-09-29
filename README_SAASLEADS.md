# saasleads – leads bland svenska SaaS-bolag (2–50 anställda)

Verktyget hämtar bolag från officiella källor, berikar dem med VD/grundare
och antal anställda, filtrerar bort sådant som inte ser ut som SaaS och sparar
allt i `leads.xlsx`.

## Flöde

1. **SCB:s företagsregister** (API) – verksamma aktiebolag med SNI 58.290 och
   62.010, storleksklass 1–4 (1–49 anställda; SCB saknar en klass som börjar
   på 2, så gränsen 2–50 kontrolleras i steg 2).
2. **Bolagsverket, värdefulla datamängder** (API, kostnadsfritt) – postort,
   SNI, verksamhetsbeskrivning och den senaste **digitala årsredovisningen**.
   Ur årsredovisningen (iXBRL) läses *medelantal anställda* och undertecknarna.
   VD väljs i första hand; saknar bolaget VD används styrelseordförande eller
   ensam ledamot (i småbolag oftast grundaren). Kolumnen *Roll* visar vilket.
3. **Hemsida** – domäner gissas från namnet (`acme.se`, `acme.com`, `acme.io` …)
   och godkänns bara om sidan visar organisationsnumret eller tydligt
   företagsnamnet. Ingen sökmotorskrapning.
4. **SaaS-filter** – startsidan (och en ev. pris-sida) poängsätts på ord som
   *prenumeration, pricing, plattform, gratis provperiod, boka demo, kr/mån,
   logga in*; *konsult, bemanning, byrå, timpris* ger minuspoäng.
   Tröskel: `--saas-threshold` (standard 4).
5. **Hitta.se-länk** – en färdig söklänk (namn + ort) för manuell uppslagning.
6. Valfritt **Seedtable** (`--seedtable`), matchas mot SCB via namnsökning.

Excel-filen har flikarna **Leads**, **Att granska** (ingen verifierad hemsida
hittad eller fel vid hämtning), **Bortfiltrerade** (med anledning) och **Om**.

## Regler för insamling

* Hitta, Merinfo, Ratsit, Allabolag, Eniro, Proff m.fl. står på en blocklista
  i `saasleads/polite.py` och anropas aldrig – Hitta används bara som länk.
* robots.txt följs för alla webbsidor (inkl. `Crawl-delay`); går robots.txt
  inte att läsa pga. serverfel hoppas sajten över.
* Fördröjning: minst 3 s mellan anrop till samma webbplats, 1 s mellan
  API-anrop (`--web-delay`, `--api-delay`).
* VD-namn är personuppgifter. B2B-kontakt kan ske med berättigat intresse
  enligt GDPR, men informera vid första kontakt och respektera avregistrering.

## Kom igång

Behörigheter (båda kostnadsfria):

* **SCB**: ansök om API-åtkomst till företagsregistret på scb.se. Du får ett
  `.pfx`-certifikat; konvertera till PEM:
  `openssl pkcs12 -in scb.pfx -out scb.pem -nodes`
* **Bolagsverket**: registrera en applikation på
  https://portal.api.bolagsverket.se (värdefulla datamängder) och hämta
  client id/secret.

```bash
pip install -r requirements.txt
export SCB_CERT=/sökväg/scb.pem
export BOLAGSVERKET_CLIENT_ID=...
export BOLAGSVERKET_CLIENT_SECRET=...

# kontrollera SCB:s kategori-namn/koder (justera --scb-* flaggor vid behov)
python -m saasleads scb-kategorier > scb_kategorier.json

# testkörning på 20 slumpade bolag
python -m saasleads run --limit 20 --shuffle --out leads.xlsx

# hela listan (återanvänder cachen från testet i .saasleads_cache/)
python -m saasleads run --out leads.xlsx
```

Om SCB har gått över till SNI 2025 för dina uttag, ange de nya koderna, t.ex.
`--sni 58290 62100`.
