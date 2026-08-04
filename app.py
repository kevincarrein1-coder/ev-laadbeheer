# -*- coding: utf-8 -*-
"""
EV-Laadbeheer voor BYD Sealion 7 Comfort
Één-bestands Streamlit applicatie.

Starten:
    pip install streamlit pandas plotly requests openpyxl
    streamlit run app.py
"""

import re
from datetime import date, datetime
from io import BytesIO

import pandas as pd
import plotly.express as px
import requests
import sqlalchemy as sa
import streamlit as st

# Optioneel: browser-geolocatie ("gebruik mijn locatie"). Werkt alleen als het
# pakket streamlit-geolocation geinstalleerd is (zie requirements.txt).
try:
    from streamlit_geolocation import streamlit_geolocation
    HEEFT_GEOLOCATIE = True
except Exception:
    HEEFT_GEOLOCATIE = False

# ---------------------------------------------------------------------------
# VASTE AUTO-INSTELLINGEN
# ---------------------------------------------------------------------------
BATTERIJ_CAPACITEIT = 82.5          # kWh - exacte bruikbare capaciteit BYD Sealion 7 Comfort
DB_BESTAND = "ev_laadbeheer.db"

# Belgische steden voor de live prijskaart (lat, lon)
STEDEN = {
    "Brussel":   (50.8503, 4.3517),
    "Antwerpen": (51.2194, 4.4025),
    "Gent":      (51.0543, 3.7174),
    "Ieper":     (50.8514, 2.8853),
}

st.set_page_config(page_title="EV-Laadbeheer BYD Sealion 7",
                   page_icon="⚡", layout="wide")


# ---------------------------------------------------------------------------
# DATABASE (SQLAlchemy: lokaal SQLite, online Supabase/Postgres)
# ---------------------------------------------------------------------------
# De app zoekt eerst een cloud-databaseverbinding in st.secrets:
#   [database]
#   url = "postgresql://user:pass@host:5432/postgres"
# Wordt die niet gevonden (bijv. lokaal), dan valt hij terug op een lokaal
# SQLite-bestand, zodat de app overal blijft draaien.

def _bepaal_db_url():
    try:
        url = st.secrets["database"]["url"]
        if url:
            return str(url)
    except Exception:
        pass
    return f"sqlite:///{DB_BESTAND}"


@st.cache_resource
def get_engine():
    return sa.create_engine(_bepaal_db_url(), pool_pre_ping=True)


engine = get_engine()
metadata = sa.MetaData()
laadsessies = sa.Table(
    "laadsessies", metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("datum", sa.String),
    sa.Column("locatienaam", sa.String),
    sa.Column("adres", sa.String),
    sa.Column("kwh", sa.Float),
    sa.Column("methode", sa.String),
    sa.Column("kosten", sa.Float),
    sa.Column("type_lader", sa.String),
    sa.Column("km_stand", sa.Integer),
)


def init_db():
    """Maakt de tabel automatisch aan bij de allereerste opstart."""
    metadata.create_all(engine)


def voeg_sessie_toe(datum, locatienaam, adres, kwh, methode, kosten, type_lader, km_stand):
    with engine.begin() as conn:
        conn.execute(laadsessies.insert().values(
            datum=datum, locatienaam=locatienaam, adres=adres, kwh=kwh,
            methode=methode, kosten=kosten, type_lader=type_lader,
            km_stand=km_stand,
        ))


def laad_sessies():
    query = sa.select(laadsessies).order_by(laadsessies.c.datum.asc(),
                                            laadsessies.c.id.asc())
    with engine.connect() as conn:
        return pd.read_sql(query, conn)


def laatste_km_stand():
    query = (sa.select(laadsessies.c.km_stand)
             .where(laadsessies.c.km_stand.isnot(None))
             .order_by(laadsessies.c.id.desc()).limit(1))
    with engine.connect() as conn:
        rij = conn.execute(query).fetchone()
    return int(rij[0]) if rij and rij[0] is not None else 0


init_db()

# Sessie-state initialiseren (voor doorsturen paal van kaart -> logformulier)
st.session_state.setdefault("geselecteerde_locatie", "")
st.session_state.setdefault("geselecteerd_adres", "")
st.session_state.setdefault("geselecteerd_tarief", None)
st.session_state.setdefault("geselecteerde_categorie", "")


# ---------------------------------------------------------------------------
# ZIJBALK: FLEXIBELE TARIEVEN & BTW-LOGICA
# ---------------------------------------------------------------------------
st.sidebar.title("⚙️ Tariefinstellingen")
st.sidebar.caption("BYD Sealion 7 Comfort · batterij 82,5 kWh")

radius_korting = st.sidebar.slider(
    "Radius korting op publieke AC-palen (%)", 0, 50, 10,
    help="Korting op het basistarief van publieke AC-palen via Radius.")

electra_eigen = st.sidebar.number_input(
    "Electra eigen netwerk (€/kWh, incl. BTW)", min_value=0.0,
    value=0.39, step=0.01, format="%.2f")

electra_partner = st.sidebar.number_input(
    "Electra partner roaming Ionity/Fastned (€/kWh, incl. BTW)", min_value=0.0,
    value=0.59, step=0.01, format="%.2f")

smappee_tarief = st.sidebar.number_input(
    "Smappee werk standaardtarief (€/kWh)", min_value=0.0,
    value=0.00, step=0.01, format="%.2f")

st.sidebar.divider()
basis_ac_prijs = st.sidebar.number_input(
    "Basis AC-prijs publieke paal (€/kWh, excl. korting/BTW)",
    min_value=0.0, value=0.35, step=0.01, format="%.2f",
    help="Basistarief vóór Radius-korting en vóór 21% BTW.")

def _secret(sectie, sleutel):
    try:
        v = st.secrets[sectie][sleutel]
        return str(v) if v else ""
    except Exception:
        return ""


ocm_api_key = st.sidebar.text_input(
    "Open Charge Map API-sleutel", type="password",
    value=_secret("openchargemap", "api_key"),
    help="Wordt automatisch geladen uit Secrets. Gratis sleutel via openchargemap.org.")

tomtom_api_key = st.sidebar.text_input(
    "TomTom API-sleutel", type="password",
    value=_secret("tomtom", "api_key"),
    help="Gratis sleutel via developer.tomtom.com. Voegt actuele, commerciele "
         "palen toe (inclusief netwerken zoals Electra).")


def radius_prijs_incl_btw():
    """(Basis_AC_Prijs * (1 - Korting/100)) * 1.21"""
    return (basis_ac_prijs * (1 - radius_korting / 100)) * 1.21


st.sidebar.metric("→ Radius-prijs incl. 21% BTW",
                  f"€ {radius_prijs_incl_btw():.3f}/kWh")


def prijs_voor_methode(methode):
    """Geeft het €/kWh tarief voor een gekozen laadmethode."""
    if methode == "Radius Fleetpass":
        return radius_prijs_incl_btw()
    if methode == "Electra Kaart":
        return electra_eigen
    if methode == "Smappee (Werk)":
        return smappee_tarief
    return electra_eigen


# ---------------------------------------------------------------------------
# TABS
# ---------------------------------------------------------------------------
st.title("⚡ EV-Laadbeheer — BYD Sealion 7 Comfort")

tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Dashboard", "🗺️ Live Prijskaart",
    "📝 Laadsessie Loggen", "📥 Facturen Importeren",
])


# ===========================================================================
# TABBLAD 1 — DASHBOARD & EXCEL EXPORT
# ===========================================================================
with tab1:
    st.header("Dashboard")
    df = laad_sessies()

    totaal_sessies = len(df)
    totaal_kwh = float(df["kwh"].sum()) if totaal_sessies else 0.0
    totaal_kosten = float(df["kosten"].sum()) if totaal_sessies else 0.0

    k1, k2, k3 = st.columns(3)
    k1.metric("🔌 Aantal laadsessies", f"{totaal_sessies}")
    k2.metric("⚡ Totaal geladen", f"{totaal_kwh:,.1f} kWh")
    k3.metric("💶 Totale kosten", f"€ {totaal_kosten:,.2f}")

    st.divider()

    # Slimme EV-statistieken (minstens 2 sessies met km-stand nodig)
    st.subheader("🚗 Slimme EV-statistieken")
    km_df = df[df["km_stand"].notna() & (df["km_stand"] > 0)]
    if len(km_df) >= 2:
        km_min = int(km_df["km_stand"].min())
        km_max = int(km_df["km_stand"].max())
        gereden_km = km_max - km_min

        if gereden_km > 0:
            verbruik_100 = (totaal_kwh / gereden_km) * 100          # kWh/100km
            kost_per_km_cent = (totaal_kosten / gereden_km) * 100   # eurocent/km

            s1, s2, s3 = st.columns(3)
            s1.metric("📏 Gereden kilometers", f"{gereden_km:,} km")
            s2.metric("🔋 Gemiddeld verbruik", f"{verbruik_100:.1f} kWh/100km")
            s3.metric("💰 Kostprijs per km", f"{kost_per_km_cent:.1f} ct/km")
        else:
            st.info("Kilometerstanden zijn gelijk — kan nog geen verbruik berekenen.")
    else:
        st.info("Voeg minstens 2 sessies met kilometerstand toe voor verbruiksstatistieken.")

    st.divider()

    if totaal_sessies:
        # Kosten per laadmethode (Plotly bar)
        st.subheader("Kosten per laadmethode")
        per_methode = df.groupby("methode", as_index=False)["kosten"].sum()
        fig = px.bar(per_methode, x="methode", y="kosten",
                     labels={"methode": "Laadmethode", "kosten": "Kosten (€)"},
                     color="methode", text_auto=".2f")
        fig.update_layout(showlegend=False, height=380)
        st.plotly_chart(fig, use_container_width=True)

        st.divider()

        # Overzichtstabel + filters
        st.subheader("Overzicht laadsessies")
        f1, f2, f3 = st.columns(3)
        methodes = ["Alle"] + sorted(df["methode"].dropna().unique().tolist())
        gekozen_methode = f1.selectbox("Filter op laadmethode", methodes)

        datums = pd.to_datetime(df["datum"], errors="coerce")
        min_d = datums.min().date() if datums.notna().any() else date.today()
        max_d = datums.max().date() if datums.notna().any() else date.today()
        van_datum = f2.date_input("Vanaf datum", min_d)
        tot_datum = f3.date_input("Tot en met datum", max_d)

        gefilterd = df.copy()
        gefilterd["_dt"] = pd.to_datetime(gefilterd["datum"], errors="coerce")
        if gekozen_methode != "Alle":
            gefilterd = gefilterd[gefilterd["methode"] == gekozen_methode]
        gefilterd = gefilterd[
            (gefilterd["_dt"].dt.date >= van_datum) &
            (gefilterd["_dt"].dt.date <= tot_datum)
        ].drop(columns=["_dt"])

        toon = gefilterd.rename(columns={
            "id": "ID", "datum": "Datum", "locatienaam": "Locatie",
            "adres": "Adres", "kwh": "kWh", "methode": "Methode",
            "kosten": "Kosten (€)", "type_lader": "Type lader",
            "km_stand": "Km-stand",
        })
        st.dataframe(toon, use_container_width=True, hide_index=True)

        # Downloadknoppen
        d1, d2 = st.columns(2)
        csv_bytes = toon.to_csv(index=False, sep=";").encode("utf-8-sig")
        d1.download_button("⬇️ Download CSV", csv_bytes,
                           file_name="laadsessies.csv", mime="text/csv",
                           use_container_width=True)

        excel_buffer = BytesIO()
        try:
            with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
                toon.to_excel(writer, index=False, sheet_name="Laadsessies")
            d2.download_button(
                "⬇️ Download Excel", excel_buffer.getvalue(),
                file_name="laadsessies.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True)
        except Exception:
            d2.info("Installeer 'openpyxl' voor Excel-export.")
    else:
        st.info("Nog geen laadsessies. Log je eerste sessie in tabblad '📝 Laadsessie Loggen'.")


# ===========================================================================
# TABBLAD 2 — LIVE PRIJSKAART
# ===========================================================================
def _max_vermogen(poi):
    """Hoogste vermogen (kW) van alle connectoren van een paal."""
    mp = 0.0
    for c in (poi.get("Connections") or []):
        p = c.get("PowerKW")
        try:
            if p:
                mp = max(mp, float(p))
        except (TypeError, ValueError):
            pass
    return mp


# Bekende snelladernetwerken (DC) en het Electra-eigen netwerk.
ELECTRA_EIGEN = ("electra",)
SNELLADER_PARTNERS = ("ionity", "fastned", "atlante", "tesla")


def classificeer_paal(operator, titel, max_power):
    """Categorie + kleur + prijs op basis van netwerk en vermogen.

    - Electra (eigen netwerk)            -> groen, eigen tarief
    - Ionity/Fastned/Atlante/Tesla (DC)  -> oranje, partner-tarief
    - Overige DC-snelladers (> 22 kW)    -> rood, partner-tarief
    - Publieke AC-palen (<= 22 kW)       -> blauw, Radius-tarief
    """
    tekst = f"{operator} {titel}".lower()
    if any(w in tekst for w in ELECTRA_EIGEN):
        return "🟢 Electra", "green", electra_eigen
    if any(w in tekst for w in SNELLADER_PARTNERS):
        return "🟠 Electra Partner", "orange", electra_partner
    if max_power and max_power > 22:
        return "🔴 Overige DC", "red", electra_partner
    return "🔵 Publieke AC (Radius)", "blue", radius_prijs_incl_btw()


@st.cache_data(ttl=600, show_spinner=False)
def haal_laadpalen(lat, lon, api_key):
    """Haalt openbare laadpalen op via de gratis Open Charge Map API."""
    url = "https://api.openchargemap.io/v3/poi/"
    params = {
        "output": "json", "countrycode": "BE",
        "latitude": lat, "longitude": lon,
        "distance": 25, "distanceunit": "KM",
        "maxresults": 200, "compact": True, "verbose": False,
    }
    if api_key:
        params["key"] = api_key
    headers = {"User-Agent": "EV-Laadbeheer-BYD/1.0"}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


def ocm_naar_rijen(data, fallback_plaats):
    """Zet ruwe Open Charge Map-data om naar uniforme paalrijen."""
    rijen = []
    for poi in data:
        adres_info = poi.get("AddressInfo") or {}
        plat, plon = adres_info.get("Latitude"), adres_info.get("Longitude")
        if plat is None or plon is None:
            continue
        titel = adres_info.get("Title", "Onbekende paal")
        adres = ", ".join(filter(None, [
            adres_info.get("AddressLine1"), adres_info.get("Town")]))
        operator = (poi.get("OperatorInfo") or {}).get("Title", "") or ""
        max_power = _max_vermogen(poi)
        categorie, kleur, prijs = classificeer_paal(operator, titel, max_power)
        rijen.append({
            "Locatie": titel, "Adres": adres or fallback_plaats,
            "lat": plat, "lon": plon,
            "Categorie": categorie, "kleur": kleur,
            "Netwerk": operator or "Onbekend",
            "kW": round(max_power) if max_power else None,
            "Prijs": prijs, "label": f"€{prijs:.2f}", "Bron": "OCM",
        })
    return rijen


def _osm_vermogen(tags):
    """Best-effort vermogen (kW) uit diverse OpenStreetMap-tags."""
    kandidaten = []
    for k, v in tags.items():
        if k == "maxpower" or k == "charging_station:output" or k.endswith(":output"):
            m = re.search(r"(\d+(?:[.,]\d+)?)", str(v))
            if m:
                val = float(m.group(1).replace(",", "."))
                if val > 1000:          # waarde in watt -> kW
                    val = val / 1000.0
                kandidaten.append(val)
    return max(kandidaten) if kandidaten else 0.0


# Meerdere Overpass-servers: als de eerste traag/overbelast is, probeert de app
# automatisch de volgende. overpass-api.de is vaak druk, mirrors zijn sneller.
OVERPASS_SERVERS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]


@st.cache_data(ttl=600, show_spinner=False)
def haal_osm_palen(lat, lon, radius_m=20000):
    """Haalt laadpalen op via de gratis, sleutelvrije OpenStreetMap Overpass-API.

    Alleen 'node'-objecten (verreweg de meeste laadpalen) -> veel snellere query.
    """
    query = (
        "[out:json][timeout:20];"
        f'node["amenity"="charging_station"](around:{radius_m},{lat},{lon});'
        "out;"
    )
    laatste_fout = None
    for server in OVERPASS_SERVERS:
        try:
            r = requests.post(server, data={"data": query},
                              headers={"User-Agent": "EV-Laadbeheer-BYD/1.0"},
                              timeout=20)
            r.raise_for_status()
            return r.json().get("elements", [])
        except Exception as e:      # server traag/onbereikbaar -> volgende proberen
            laatste_fout = e
            continue
    raise RuntimeError(f"Alle OpenStreetMap-servers gaven een fout: {laatste_fout}")


def osm_naar_rijen(elements, fallback_plaats):
    """Zet ruwe OpenStreetMap-elementen om naar uniforme paalrijen."""
    rijen = []
    for el in elements:
        plat = el.get("lat") or (el.get("center") or {}).get("lat")
        plon = el.get("lon") or (el.get("center") or {}).get("lon")
        if plat is None or plon is None:
            continue
        tags = el.get("tags") or {}
        operator = tags.get("network") or tags.get("operator") or ""
        titel = tags.get("name") or operator or "Laadpaal"
        adres = ", ".join(filter(None, [
            tags.get("addr:street"), tags.get("addr:city")]))
        max_power = _osm_vermogen(tags)
        categorie, kleur, prijs = classificeer_paal(operator, titel, max_power)
        rijen.append({
            "Locatie": titel, "Adres": adres or fallback_plaats,
            "lat": plat, "lon": plon,
            "Categorie": categorie, "kleur": kleur,
            "Netwerk": operator or "Onbekend",
            "kW": round(max_power) if max_power else None,
            "Prijs": prijs, "label": f"€{prijs:.2f}", "Bron": "OSM",
        })
    return rijen


@st.cache_data(ttl=600, show_spinner=False)
def haal_tomtom_palen(lat, lon, api_key, radius_m=25000):
    """Haalt laadpalen op via de commerciele TomTom Search API (gratis tier)."""
    if not api_key:
        return []
    url = "https://api.tomtom.com/search/2/nearbySearch/.json"
    params = {
        "key": api_key, "lat": lat, "lon": lon, "radius": radius_m,
        "categorySet": 7309,   # 7309 = Electric Vehicle Station
        "limit": 100, "view": "Unified",
    }
    r = requests.get(url, params=params,
                     headers={"User-Agent": "EV-Laadbeheer-BYD/1.0"}, timeout=20)
    r.raise_for_status()
    return r.json().get("results", [])


def tomtom_naar_rijen(results, fallback_plaats):
    """Zet ruwe TomTom-resultaten om naar uniforme paalrijen."""
    rijen = []
    for res in results:
        pos = res.get("position") or {}
        plat, plon = pos.get("lat"), pos.get("lon")
        if plat is None or plon is None:
            continue
        poi = res.get("poi") or {}
        titel = poi.get("name") or "Laadpaal"
        brands = poi.get("brands") or []
        operator = (brands[0].get("name") if brands else "") or ""
        addr = res.get("address") or {}
        adres = ", ".join(filter(None, [
            addr.get("streetName"), addr.get("municipality")]))
        max_power = 0.0
        for con in ((res.get("chargingPark") or {}).get("connectors") or []):
            p = con.get("ratedPowerKW")
            try:
                if p:
                    max_power = max(max_power, float(p))
            except (TypeError, ValueError):
                pass
        categorie, kleur, prijs = classificeer_paal(operator, titel, max_power)
        rijen.append({
            "Locatie": titel, "Adres": adres or fallback_plaats,
            "lat": plat, "lon": plon,
            "Categorie": categorie, "kleur": kleur,
            "Netwerk": operator or "Onbekend",
            "kW": round(max_power) if max_power else None,
            "Prijs": prijs, "label": f"€{prijs:.2f}", "Bron": "TomTom",
        })
    return rijen


def dedupe_palen(rijen):
    """Verwijdert dubbels (zelfde locatie) op afgeronde coordinaten."""
    gezien, uniek = set(), []
    for r in rijen:
        sleutel = (round(float(r["lat"]), 4), round(float(r["lon"]), 4))
        if sleutel in gezien:
            continue
        gezien.add(sleutel)
        uniek.append(r)
    return uniek


with tab2:
    st.header("Live Prijskaart")

    c1, c2 = st.columns([1, 2])
    stad = c1.selectbox("Kies een stad", list(STEDEN.keys()))
    lat, lon = STEDEN[stad]

    if c2.button("🔄 Laadpalen laden / vernieuwen", use_container_width=True):
        st.cache_data.clear()

    # Eigen locatie: zoekt de dichtstbijzijnde palen rond jou i.p.v. rond de stad.
    if HEEFT_GEOLOCATIE:
        st.caption("📍 Of gebruik je eigen locatie — klik op het locatie-icoon "
                   "en sta toegang toe:")
        eigen = streamlit_geolocation()
        if eigen and eigen.get("latitude") and eigen.get("longitude"):
            lat, lon = eigen["latitude"], eigen["longitude"]
            st.success(f"Eigen locatie gebruikt ({lat:.4f}, {lon:.4f}) — "
                       "palen worden rond jou gezocht.")
    else:
        st.caption("Tip: voeg het pakket 'streamlit-geolocation' toe voor "
                   "'gebruik mijn locatie'.")

    bron = st.radio("Databron", ["Alle bronnen (aanbevolen)", "Open Charge Map",
                                 "OpenStreetMap", "TomTom"], horizontal=True)
    alle = bron == "Alle bronnen (aanbevolen)"
    # Coordinaten grof afronden (~1 km): binnen die straal wordt de al opgehaalde
    # data hergebruikt i.p.v. bij elke locatieklik opnieuw alle bronnen te bevragen.
    qlat, qlon = round(lat, 2), round(lon, 2)

    ocm_rijen, osm_rijen, tt_rijen = [], [], []
    with st.spinner("Laadpalen laden uit de gekozen bronnen…"):
        if alle or bron == "Open Charge Map":
            try:
                ocm_rijen = ocm_naar_rijen(
                    haal_laadpalen(qlat, qlon, ocm_api_key), stad)
            except Exception as e:
                st.warning(f"Open Charge Map niet bereikbaar: {e}")
        if alle or bron == "OpenStreetMap":
            try:
                osm_rijen = osm_naar_rijen(haal_osm_palen(qlat, qlon), stad)
            except Exception as e:
                st.warning(f"OpenStreetMap niet bereikbaar: {e}")
        if alle or bron == "TomTom":
            try:
                tt_rijen = tomtom_naar_rijen(
                    haal_tomtom_palen(qlat, qlon, tomtom_api_key), stad)
            except Exception as e:
                st.warning(f"TomTom niet bereikbaar: {e}")

    if (alle or bron == "TomTom") and not tomtom_api_key:
        st.info("Voeg een TomTom-sleutel toe (zijbalk of Secrets) voor de meest "
                "complete, actuele palen zoals nieuwe Electra-snelladers.")

    palen_df = pd.DataFrame(dedupe_palen(ocm_rijen + osm_rijen + tt_rijen))
    if not palen_df.empty:
        st.caption(f"🔌 {len(palen_df)} unieke palen "
                   f"(Open Charge Map: {len(ocm_rijen)} · "
                   f"OpenStreetMap: {len(osm_rijen)} · "
                   f"TomTom: {len(tt_rijen)}).")

    if not palen_df.empty:
        kleur_map = {
            "🟢 Electra": "green", "🟠 Electra Partner": "orange",
            "🔵 Publieke AC (Radius)": "blue", "🔴 Overige DC": "red",
        }
        fig = px.scatter_mapbox(
            palen_df, lat="lat", lon="lon", text="label",
            color="Categorie", color_discrete_map=kleur_map,
            hover_name="Locatie",
            hover_data={"Adres": True, "Netwerk": True, "kW": True,
                        "Bron": True, "Prijs": ":.2f",
                        "lat": False, "lon": False, "label": False},
            zoom=11, height=560,
        )
        fig.update_traces(textposition="top center",
                          marker=dict(size=15),
                          textfont=dict(size=12, color="black"))
        fig.update_layout(mapbox_style="open-street-map",
                          margin=dict(l=0, r=0, t=0, b=0),
                          legend=dict(orientation="h", yanchor="bottom", y=1.01))
        st.plotly_chart(fig, use_container_width=True)

        st.caption("🟢 Electra · 🟠 Ionity/Fastned/Atlante · 🔵 Publieke AC (Radius) · 🔴 Overige DC")

        st.divider()
        st.subheader("Paal selecteren")
        opties = palen_df["Locatie"] + " — " + palen_df["Adres"]
        keuze_idx = st.selectbox(
            "Selecteer een laadpaal", range(len(palen_df)),
            format_func=lambda i: opties.iloc[i])
        gekozen = palen_df.iloc[keuze_idx]

        info1, info2 = st.columns(2)
        info1.metric("Geselecteerde paal", gekozen["Locatie"])
        info2.metric("Verwacht tarief", f"€ {gekozen['Prijs']:.2f}/kWh")

        # Universele Google Maps geo-link (Apple CarPlay / Android Auto)
        maps_url = (
            "https://www.google.com/maps/dir/?api=1&destination="
            f"{gekozen['lat']},{gekozen['lon']}"
        )
        st.link_button("🗺️ Start Navigatie (Google Maps)", maps_url,
                       use_container_width=True)

        if st.button("📝 Gebruik deze paal voor nieuwe laadsessie",
                     use_container_width=True):
            st.session_state["geselecteerde_locatie"] = gekozen["Locatie"]
            st.session_state["geselecteerd_adres"] = gekozen["Adres"]
            st.session_state["geselecteerd_tarief"] = float(gekozen["Prijs"])
            st.session_state["geselecteerde_categorie"] = gekozen["Categorie"]
            st.success("Paal + tarief doorgestuurd naar tabblad "
                       "'📝 Laadsessie Loggen'.")
    else:
        st.info("Geen laadpalen gevonden. Probeer een andere stad of voeg een API-sleutel toe.")


# ===========================================================================
# TABBLAD 3 — LAADSESSIE LOGGEN
# ===========================================================================
with tab3:
    st.header("Laadsessie loggen")

    vorige_km = laatste_km_stand()
    if vorige_km:
        st.caption(f"ℹ️ Vorige geregistreerde stand: {vorige_km:,.0f} km".replace(",", "."))

    if st.session_state["geselecteerde_locatie"]:
        st.success(f"Paal van kaart geladen: {st.session_state['geselecteerde_locatie']}")

    # Live berekening buiten het formulier (formulier herberekent niet live)
    st.subheader("Batterij & berekening")
    b1, b2 = st.columns(2)
    start_pct = b1.slider("Startpercentage batterij (%)", 0, 100, 20)
    eind_pct = b2.slider("Eindpercentage batterij (%)", 0, 100, 80)

    # Best passende laadmethode voorstellen op basis van de gekozen paal.
    methode_opties = ["Radius Fleetpass", "Electra Kaart", "Smappee (Werk)"]
    cat = st.session_state.get("geselecteerde_categorie", "")
    if "Radius" in cat:
        methode_idx = 0            # Publieke AC (Radius)
    elif "Electra" in cat:
        methode_idx = 1            # Electra (eigen of partner)
    else:
        methode_idx = 0
    methode = st.selectbox("Laadmethode", methode_opties, index=methode_idx)
    standaard_tarief = prijs_voor_methode(methode)

    geladen_kwh = max(0.0, (eind_pct - start_pct) / 100 * BATTERIJ_CAPACITEIT)

    # Als er via de kaart een paal is gekozen, neemt die prijs de standaard over.
    kaart_tarief = st.session_state.get("geselecteerd_tarief")
    if kaart_tarief is not None:
        default_tarief = float(kaart_tarief)
        st.caption(f"💡 Tarief automatisch overgenomen van de gekozen paal "
                   f"(€ {default_tarief:.3f}/kWh). Je kunt het hieronder aanpassen.")
    else:
        default_tarief = float(standaard_tarief)

    toegepast_tarief = st.number_input(
        "Toegepast tarief (€/kWh) — handmatig aanpasbaar",
        min_value=0.0, value=round(default_tarief, 3),
        step=0.01, format="%.3f")
    reele_kosten = geladen_kwh * toegepast_tarief

    m1, m2, m3 = st.columns(3)
    m1.metric("Te laden", f"{geladen_kwh:.1f} kWh")
    m2.metric("Tarief", f"€ {toegepast_tarief:.3f}/kWh")
    m3.metric("Totale kostprijs", f"€ {reele_kosten:.2f}")

    if eind_pct <= start_pct:
        st.warning("Eindpercentage moet hoger zijn dan startpercentage.")

    st.divider()

    with st.form("log_form", clear_on_submit=False):
        st.subheader("Sessiegegevens")
        c1, c2 = st.columns(2)
        datum_in = c1.date_input("Datum", date.today())
        type_lader = c2.selectbox("Type lader", ["AC (Traag)", "DC (Snel)"])

        locatie_in = st.text_input(
            "Locatie", value=st.session_state["geselecteerde_locatie"])
        adres_in = st.text_input(
            "Adres", value=st.session_state["geselecteerd_adres"])

        km_in = st.number_input(
            "Huidige kilometerstand (km) *", min_value=0,
            value=int(vorige_km), step=1)

        verzonden = st.form_submit_button("💾 Laadsessie opslaan",
                                          use_container_width=True)

        if verzonden:
            if geladen_kwh <= 0:
                st.error("Kan niet opslaan: aantal kWh is 0. Pas de percentages aan.")
            elif km_in <= 0:
                st.error("Kilometerstand is verplicht en moet groter zijn dan 0.")
            else:
                voeg_sessie_toe(
                    datum_in.isoformat(), locatie_in.strip() or "Onbekend",
                    adres_in.strip(), round(geladen_kwh, 2), methode,
                    round(reele_kosten, 2), type_lader, int(km_in))
                # Selectie wissen na opslaan
                st.session_state["geselecteerde_locatie"] = ""
                st.session_state["geselecteerd_adres"] = ""
                st.session_state["geselecteerd_tarief"] = None
                st.session_state["geselecteerde_categorie"] = ""
                st.success(
                    f"Sessie opgeslagen: {geladen_kwh:.1f} kWh · "
                    f"€ {reele_kosten:.2f} · {int(km_in):,} km".replace(",", "."))


# ===========================================================================
# TABBLAD 4 — CSV/EXCEL FACTUREN IMPORTEREN
# ===========================================================================
with tab4:
    st.header("Facturen importeren")
    st.write("Importeer laadsessies in bulk via een CSV- of Excel-bestand.")
    st.caption("Verwachte kolommen: datum, locatienaam, adres, kwh, methode, "
               "kosten, type_lader, km_stand")

    bestand = st.file_uploader("Kies een CSV- of Excel-bestand",
                               type=["csv", "xlsx", "xls"])

    if bestand is not None:
        try:
            if bestand.name.lower().endswith(".csv"):
                imp = pd.read_csv(bestand, sep=None, engine="python")
            else:
                imp = pd.read_excel(bestand)
            imp.columns = [str(c).strip().lower() for c in imp.columns]

            st.subheader("Voorbeeld van geïmporteerde gegevens")
            st.dataframe(imp.head(20), use_container_width=True, hide_index=True)

            verwacht = ["datum", "locatienaam", "adres", "kwh", "methode",
                        "kosten", "type_lader", "km_stand"]
            ontbreekt = [k for k in verwacht if k not in imp.columns]
            if ontbreekt:
                st.warning("Ontbrekende kolommen worden leeg opgeslagen: "
                           + ", ".join(ontbreekt))

            if st.button(f"✅ {len(imp)} rijen importeren in database",
                         use_container_width=True):
                aantal = 0
                for _, r in imp.iterrows():
                    try:
                        km_val = r.get("km_stand")
                        km_val = int(km_val) if pd.notna(km_val) else 0
                        voeg_sessie_toe(
                            str(r.get("datum", date.today().isoformat())),
                            str(r.get("locatienaam", "Import")),
                            str(r.get("adres", "")),
                            float(r.get("kwh", 0) or 0),
                            str(r.get("methode", "Import")),
                            float(r.get("kosten", 0) or 0),
                            str(r.get("type_lader", "")),
                            km_val)
                        aantal += 1
                    except Exception:
                        continue
                st.success(f"{aantal} sessies geïmporteerd. "
                           "Bekijk ze in het Dashboard.")
        except Exception as e:
            st.error(f"Kon het bestand niet verwerken: {e}")
