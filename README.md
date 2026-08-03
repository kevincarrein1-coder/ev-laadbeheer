# ⚡ EV-Laadbeheer — BYD Sealion 7 Comfort

Een persoonlijke Streamlit-app om laadsessies van je volledig elektrische
**BYD Sealion 7 Comfort** (bruikbare batterij: **82,5 kWh**) te beheren:
sessies loggen, kosten en verbruik volgen, een live prijskaart van openbare
laadpalen bekijken en facturen in bulk importeren.

Alles zit in één bestand: `app.py`. De data wordt lokaal opgeslagen in een
SQLite-database (`ev_laadbeheer.db`), die automatisch wordt aangemaakt bij de
eerste start.

---

## Installatie

Zorg dat je **Python 3.9+** hebt. Open een terminal in de projectmap en voer uit:

```bash
pip install -r requirements.txt
```

## Starten

```bash
streamlit run app.py
```

De app opent automatisch in je browser (meestal op http://localhost:8501).
Geoptimaliseerd voor een groot tabletscherm: grote KPI-kaarten en brede knoppen.

---

## Functies

**📊 Dashboard**
KPI's voor aantal sessies, totaal geladen kWh en totale kosten. Bij minstens
2 sessies met kilometerstand: gereden km, gemiddeld verbruik (kWh/100km) en
kostprijs per km (ct/km). Plus een kostengrafiek per laadmethode, een filterbare
tabel en CSV/Excel-export.

**🗺️ Live Prijskaart**
Openbare laadpalen via de gratis Open Charge Map API rond Belgische steden
(Brussel, Antwerpen, Gent, Ieper). Prijs als label bij elke stip, kleurcodes
(🟢 Electra · 🟠 Ionity/Fastned/Atlante · 🔵 Publieke AC/Radius · 🔴 Overige DC),
een Google Maps-navigatieknop en een knop om een paal door te sturen naar het
logformulier.

**📝 Laadsessie Loggen**
Formulier met automatische invulling vanaf de kaart, dropdowns voor laadmethode
en type lader, verplichte kilometerstand (met herinnering aan de vorige stand),
start-/eindpercentage-sliders en een live kWh- en kostenberekening met handmatig
aanpasbaar tarief.

**📥 Facturen Importeren**
Upload CSV- of Excel-bestanden om laadsessies in bulk te importeren.

---

## Tarieven instellen (zijbalk)

- **Radius korting** op publieke AC-palen (0–50%, standaard 10%)
- **Basis AC-prijs** per kWh (excl. korting/BTW) — nodig voor de Radius-formule
- **Electra eigen netwerk** (€/kWh, incl. BTW, standaard 0,39)
- **Electra partner roaming** Ionity/Fastned (€/kWh, incl. BTW, standaard 0,59)
- **Smappee werk** standaardtarief (€/kWh, standaard 0,00)

**Radius-prijs incl. 21% BTW** wordt automatisch berekend:

```
(basis_AC_prijs * (1 - korting/100)) * 1.21
```

### Open Charge Map API-sleutel (optioneel)

De kaart werkt zonder sleutel, maar met een gratis sleutel is hij betrouwbaarder.
Vraag er een aan op https://openchargemap.org/site/develop/api en plak hem in het
sleutelveld in de zijbalk.

---

## Bestanden

| Bestand              | Omschrijving                                    |
|----------------------|-------------------------------------------------|
| `app.py`             | De volledige applicatie                         |
| `requirements.txt`   | Python-afhankelijkheden                         |
| `ev_laadbeheer.db`   | SQLite-database (wordt automatisch aangemaakt)  |

---

## Werken met Claude Code

Open een terminal in deze map en start Claude Code:

```bash
claude
```

Verwijs naar bestanden met `@`, bijvoorbeeld:

```
> leg uit wat @app.py doet
> voeg een grafiek toe voor kosten per maand in @app.py
```
