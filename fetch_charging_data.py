# -*- coding: utf-8 -*-
"""
Haalt OCPI-laaddata op (NL NDW gzip-stream + Tesla BE roaming) -> GeoJSON
========================================================================

Schrijft 'charging_stations.geojson' met per punt een realtime OCPI-status en
kleur (groen = AVAILABLE, rood = CHARGING). Bedoeld om in te laden op een
Mapbox GL-kaart met marker-clustering (zie charging_map.html).

Bronnen (aanpasbaar in FEEDS):
  * Nederland (NDW)  : https://opendata.ndw.nu/charging_point_locations_ocpi.json.gz
                       -> wordt live als GZIP-stream uitgepakt.
  * Belgie (Tesla)   : https://charging-roaming-data.tesla.com/ocpi/cpo/2.2.1/locations
                       -> OCPI-endpoint met token (paginering via offset/Link).

Gebruik:
    pip install requests
    python fetch_charging_data.py            # schrijft charging_stations.geojson
    python fetch_charging_data.py --out data.geojson
"""

import argparse
import gzip
import io
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

import requests

# ---------------------------------------------------------------------------
# LIVE OCPI-feeds (met echte vrij/bezet-status). Zet 'gzip' True voor een
# .json.gz-download; anders gepagineerd OCPI-endpoint.
#
# Alles hier is strikt GRATIS (geen betaalde tokens). Krijg je later een gratis
# publieke OCPI-feed met live status voor een land, voeg die dan hier toe als
# extra blok (url + eventueel token). De 'merge_live_over_static'-stap laat die
# live status dan automatisch de statische (blauwe) markers overschrijven.
# ---------------------------------------------------------------------------
FEEDS = [
    {
        "naam": "Nederland (NDW)", "land": "NL",
        "url": "https://opendata.ndw.nu/charging_point_locations_ocpi.json.gz",
        "gzip": True, "token": None,
    },
    {
        "naam": "Tesla (BE roaming)", "land": "BE",
        "url": "https://charging-roaming-data.tesla.com/ocpi/cpo/2.2.1/locations",
        "gzip": False,
        "token": "ODhiZmM0ZjQtOGFmMS00YzY0LTg4MmItMWQyOTQ2YjE2OTcz",
    },
]

# OCPI-status -> kleur voor de kaart (groen beschikbaar, rood bezet/laden).
STATUS_KLEUR = {
    "AVAILABLE":   "#2ecc71",
    "CHARGING":    "#e74c3c",
    "RESERVED":    "#f1c40f",
    "BLOCKED":     "#e74c3c",
    "INOPERATIVE": "#95a5a6",
    "OUTOFORDER":  "#c0392b",
    "PLANNED":     "#7f8c8d",
    "REMOVED":     "#7f8c8d",
    "UNKNOWN":     "#bdc3c7",
}
UA = {"User-Agent": "EV-ChargingMap/1.0", "Accept": "application/json"}

# Belgie heeft geen landelijk OCPI-bulkbestand; OpenStreetMap vult het breed op.
INCLUDE_OSM_BELGIE = True
OVERPASS_SERVERS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]


def haal_osm_belgie():
    """Alle laadpalen in Belgie via OpenStreetMap (Overpass), sleutelvrij."""
    query = ('[out:json][timeout:120];area["ISO3166-1"="BE"][admin_level=2]->.b;'
             'node["amenity"="charging_station"](area.b);out;')
    for server in OVERPASS_SERVERS:
        try:
            r = requests.post(server, data={"data": query}, headers=UA, timeout=180)
            r.raise_for_status()
            return r.json().get("elements", [])
        except Exception:
            continue
    return []


def osm_naar_feature(el):
    """OSM-node -> compacte feature (blauw = locatie, geen live status)."""
    lat, lon = el.get("lat"), el.get("lon")
    if lat is None or lon is None:
        return None
    tags = el.get("tags") or {}
    naam = (tags.get("name") or tags.get("operator") or "Laadpaal")[:60]
    return _locatie_feature(lon, lat, naam, tags.get("addr:city") or "")


def _locatie_feature(lon, lat, naam, adres, max_kw=None):
    """Compacte 'locatie'-feature (blauw) voor bronnen zonder live status."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [round(lon, 5), round(lat, 5)]},
        "properties": {
            "naam": (naam or "Laadpaal")[:60], "adres": (adres or "")[:60],
            "status": "LOCATIE", "beschikbaar": 0, "totaal": 0,
            "max_kw": max_kw, "kleur": "#3498db",
        },
    }


# --- Frankrijk (IRVE open data: statische locaties, geen live status) ---------
INCLUDE_FRANKRIJK = True
FR_IRVE_GEOJSON_URL = ("https://public.opendatasoft.com/api/explore/v2.1/"
                       "catalog/datasets/mobilityref-france-irve-220/"
                       "exports/geojson")


def haal_frankrijk():
    r = requests.get(FR_IRVE_GEOJSON_URL, headers=UA, timeout=300)
    r.raise_for_status()
    return r.json().get("features", [])


def fr_naar_feature(feat):
    geom = feat.get("geometry") or {}
    coords = geom.get("coordinates") or []
    if geom.get("type") != "Point" or len(coords) < 2:
        return None
    p = feat.get("properties") or {}
    naam = p.get("nom_station") or p.get("nom_operateur") or "Borne"
    kw = None
    pv = p.get("puissance_nominale")
    try:
        v = float(pv)
        kw = round(v / 1000.0, 1) if v > 1000 else round(v, 1)
    except (TypeError, ValueError):
        pass
    return _locatie_feature(coords[0], coords[1], naam,
                            p.get("nom_operateur") or "", kw)


# --- Luxemburg (Chargy KML: locaties; sleutelvrij, vul de KML-URL in) ---------
INCLUDE_LUXEMBURG = True
LU_KML_URL = ""   # KML-download van data.public.lu (leeg = overslaan)


def haal_luxemburg():
    if not LU_KML_URL:
        return []
    r = requests.get(LU_KML_URL, headers=UA, timeout=60)
    r.raise_for_status()
    ns = "{http://www.opengis.net/kml/2.2}"
    root = ET.fromstring(r.content)
    features = []
    for pm in root.iter(ns + "Placemark"):
        naam = pm.findtext(ns + "name") or "Borne"
        coord = pm.find(".//" + ns + "coordinates")
        if coord is None or not coord.text:
            continue
        deel = coord.text.strip().split(",")
        try:
            lon, lat = float(deel[0]), float(deel[1])
        except (ValueError, IndexError):
            continue
        features.append(_locatie_feature(lon, lat, naam, "Luxembourg"))
    return features


def _pak_locaties(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("locations"), list):
            return payload["locations"]
    return []


def haal_gzip_feed(url):
    """Downloadt een .json.gz en pakt die live (streaming) uit."""
    r = requests.get(url, headers=UA, stream=True, timeout=180)
    r.raise_for_status()
    r.raw.decode_content = False               # ruwe gzip-bytes behouden
    with gzip.GzipFile(fileobj=r.raw) as gz:
        payload = json.load(gz)
    return _pak_locaties(payload)


def haal_ocpi_feed(url, token=None, max_paginas=500):
    """Haalt een gepagineerd OCPI locations-endpoint op (offset/limit + Link)."""
    headers = dict(UA)
    if token:
        headers["Authorization"] = f"Token {token}"
    locaties, volgende, params = [], url, {"limit": 100, "offset": 0}
    for _ in range(max_paginas):
        r = requests.get(volgende, headers=headers, params=params, timeout=60)
        r.raise_for_status()
        batch = _pak_locaties(r.json())
        locaties.extend(batch)
        m = re.search(r'<([^>]+)>\s*;\s*rel="next"', r.headers.get("Link", ""))
        if m:
            volgende, params = m.group(1), {}
        elif params and len(batch) >= params.get("limit", 100):
            params = {"limit": params["limit"],
                      "offset": params["offset"] + params["limit"]}
        else:
            break
    return locaties


def _max_kw(evse):
    mp = 0.0
    for con in (evse.get("connectors") or []):
        v = con.get("max_electric_power", con.get("max_power"))
        try:
            v = float(v)
            if v > 1000:
                v /= 1000.0
            mp = max(mp, v)
        except (TypeError, ValueError):
            continue
    return round(mp, 1) if mp else None


def _status_samenvatting(evses):
    tellingen = {}
    for e in evses:
        s = str(e.get("status", "UNKNOWN")).upper()
        tellingen[s] = tellingen.get(s, 0) + 1
    beschikbaar = tellingen.get("AVAILABLE", 0)
    if beschikbaar > 0:
        hoofd = "AVAILABLE"
    elif tellingen.get("CHARGING", 0) > 0:
        hoofd = "CHARGING"
    else:
        hoofd = max(tellingen, key=tellingen.get) if tellingen else "UNKNOWN"
    return beschikbaar, len(evses), hoofd


def locatie_naar_feature(loc, bron, land):
    coord = loc.get("coordinates") or {}
    try:
        lon = float(coord.get("longitude", loc.get("longitude")))
        lat = float(coord.get("latitude", loc.get("latitude")))
    except (TypeError, ValueError):
        return None
    evses = loc.get("evses") or []
    beschikbaar, totaal, hoofd = _status_samenvatting(evses)
    vermogens = [p for p in (_max_kw(e) for e in evses) if p]
    # Compact: coordinaten afgerond (~1 m) en alleen de velden die de kaart nodig
    # heeft. Dit houdt het GeoJSON-bestand klein genoeg om op GitHub te uploaden.
    return {
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [round(lon, 5), round(lat, 5)]},
        "properties": {
            "naam": (loc.get("name") or "Laadlocatie")[:60],
            "adres": (", ".join(filter(None, [loc.get("address"),
                                              loc.get("city")])))[:60],
            "status": hoofd,
            "beschikbaar": beschikbaar,
            "totaal": totaal,
            "max_kw": max(vermogens) if vermogens else None,
            "kleur": STATUS_KLEUR.get(hoofd, STATUS_KLEUR["UNKNOWN"]),
        },
    }


# --- Monta (BE, DATEX II): brede locaties via roaming. Credentials via env vars
#     zodat ze NIET in de (publieke) code staan:
#     PowerShell:  $env:MONTA_CLIENT_ID="..."; $env:MONTA_CLIENT_SECRET="..."
INCLUDE_MONTA = True
MONTA_BASIS = "https://public-api.monta.com/api/v1"


def _datex_naam(obj):
    try:
        return obj["values"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return None


def _diep_max(obj, sleutel):
    m = 0.0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == sleutel:
                try:
                    m = max(m, float(v))
                except (TypeError, ValueError):
                    pass
            else:
                m = max(m, _diep_max(v, sleutel))
    elif isinstance(obj, list):
        for v in obj:
            m = max(m, _diep_max(v, sleutel))
    return m


def haal_monta(country="BE", per_page=1000, max_paginas=50):
    cid = os.environ.get("MONTA_CLIENT_ID")
    sec = os.environ.get("MONTA_CLIENT_SECRET")
    if not (cid and sec):
        return []
    tok = requests.post(f"{MONTA_BASIS}/auth/token",
                        json={"clientId": cid, "clientSecret": sec},
                        headers={"accept": "application/json"}, timeout=30)
    tok.raise_for_status()
    access = tok.json().get("accessToken")
    sites = []
    for page in range(1, max_paginas + 1):
        r = requests.get(f"{MONTA_BASIS}/afir/charge-points",
                         params={"country": country, "page": page,
                                 "perPage": per_page},
                         headers={"Authorization": f"Bearer {access}",
                                  "accept": "application/json"}, timeout=90)
        r.raise_for_status()
        pagina = []
        for t in (r.json().get("energyInfrastructureTable") or []):
            pagina += (t.get("energyInfrastructureSite") or [])
        sites += pagina
        if len(pagina) < per_page:
            break
    return sites


def monta_site_naar_feature(site):
    coord = ((((site.get("locationReference") or {}).get("locPointLocation") or {})
              .get("pointByCoordinates") or {}).get("pointCoordinates") or {})
    try:
        lat = float(coord.get("latitude"))
        lon = float(coord.get("longitude"))
    except (TypeError, ValueError):
        return None
    naam = _datex_naam(site.get("name")) or "Laadlocatie"
    fac = ((((site.get("locationReference") or {}).get("locPointLocation") or {})
            .get("locLocationExtensionG") or {}).get("FacilityLocation") or {})
    stad = _datex_naam((fac.get("address") or {}).get("city")) or ""
    watt = _diep_max(site, "maxPowerAtSocket")
    kw = round(watt / 1000.0, 1) if watt > 1000 else (round(watt, 1) if watt else None)
    return _locatie_feature(lon, lat, naam, stad, kw)


# --- EnergyVision (BE, DATEX II v3.7 XML): live status via aparte feeds.
#     API-key via env var (uit de publieke code houden):
#     PowerShell:  $env:ENERGYVISION_API_KEY="..."
INCLUDE_ENERGYVISION = True
EV_BASE = "https://datex.cpo.energyvision.be/datex"
EV_NS = {
    "aegi": "http://datex2.eu/schema/3/afirEnergyInfrastructure",
    "com": "http://datex2.eu/schema/3/common",
    "loc": "http://datex2.eu/schema/3/locationReferencing",
    "locx": "http://datex2.eu/schema/3/locationExtension",
    "afac": "http://datex2.eu/schema/3/afirFacilities",
}


def haal_energyvision():
    key = os.environ.get("ENERGYVISION_API_KEY")
    if not key:
        return []
    h = {"Authorization": f"Bearer {key}", "accept": "application/xml"}
    rt = requests.get(f"{EV_BASE}/energy-infrastructure-table", headers=h, timeout=180)
    rt.raise_for_status()
    rs = requests.get(f"{EV_BASE}/energy-infrastructure-status", headers=h, timeout=180)
    rs.raise_for_status()
    return _ev_features(rt.content, rs.content)


def _ev_tag(ns, naam):
    return f"{{{EV_NS[ns]}}}{naam}"


def _ev_features(static_xml, status_xml):
    # 1) Live status per EVSE-id uit de dynamische feed.
    statusmap = {}
    for rp in ET.fromstring(status_xml).iter(_ev_tag("aegi", "refillPointStatus")):
        ref = rp.find(_ev_tag("afac", "reference"))
        st = rp.find(_ev_tag("aegi", "status"))
        if ref is not None and st is not None and st.text:
            statusmap[ref.get("id")] = st.text.strip().lower()

    # 2) Statische sites koppelen aan die statussen.
    features = []
    for site in ET.fromstring(static_xml).iter(_ev_tag("aegi", "energyInfrastructureSite")):
        pc = site.find(f".//{_ev_tag('loc', 'pointCoordinates')}")
        if pc is None:
            continue
        la = pc.find(_ev_tag("loc", "latitude"))
        lo = pc.find(_ev_tag("loc", "longitude"))
        try:
            lat, lon = float(la.text), float(lo.text)
        except (TypeError, ValueError, AttributeError):
            continue

        merk = site.find(f".//{_ev_tag('aegi', 'brand')}//{_ev_tag('com', 'value')}")
        naam = merk.text.strip() if merk is not None and merk.text else "EnergyVision"
        stad_el = site.find(f".//{_ev_tag('locx', 'city')}//{_ev_tag('com', 'value')}")
        stad = stad_el.text.strip() if stad_el is not None and stad_el.text else ""
        pw = site.find(f".//{_ev_tag('aegi', 'totalMaximumPower')}")
        max_kw = None
        if pw is not None and pw.text:
            try:
                w = float(pw.text)
                max_kw = round(w / 1000.0, 1) if w > 1000 else round(w, 1)
            except ValueError:
                pass

        beschikbaar, totaal, laadt = 0, 0, False
        for rp in site.iter(_ev_tag("aegi", "refillPoint")):
            totaal += 1
            s = statusmap.get(rp.get("id"))
            if s == "available":
                beschikbaar += 1
            elif s == "charging":
                laadt = True

        if beschikbaar > 0:
            status, kleur = "AVAILABLE", "#2ecc71"
        elif laadt:
            status, kleur = "CHARGING", "#e74c3c"
        else:
            status, kleur = "UNKNOWN", "#bdc3c7"

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [round(lon, 5), round(lat, 5)]},
            "properties": {
                "naam": naam[:60], "adres": stad[:60], "status": status,
                "beschikbaar": beschikbaar, "totaal": totaal,
                "max_kw": max_kw, "kleur": kleur,
            },
        })
    return features


def _afstand_m(la1, lo1, la2, lo2):
    la1, lo1, la2, lo2 = map(math.radians, (la1, lo1, la2, lo2))
    d = 2 * math.asin(math.sqrt(
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2))
    return 6371000.0 * d


def merge_live_over_static(features, tol_m=80):
    """Slimme fallback: waar een LIVE-paal (met echte status) dicht bij een
    STATISCHE paal (status 'LOCATIE') ligt, wint de live-paal. Zo overschrijft
    elke (gratis) live OCPI-feed automatisch de blauwe locatie-markers.
    """
    live = [f for f in features if f["properties"]["status"] != "LOCATIE"]
    statisch = [f for f in features if f["properties"]["status"] == "LOCATIE"]

    cel = 0.001  # ~100 m raster voor snelle nabijheidscheck
    raster = {}
    for f in live:
        lon, lat = f["geometry"]["coordinates"]
        raster.setdefault((round(lat / cel), round(lon / cel)), []).append((lat, lon))

    def heeft_live_dichtbij(lat, lon):
        ky, kx = round(lat / cel), round(lon / cel)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for (la, lo) in raster.get((ky + dy, kx + dx), []):
                    if _afstand_m(lat, lon, la, lo) <= tol_m:
                        return True
        return False

    behouden = [f for f in statisch
                if not heeft_live_dichtbij(f["geometry"]["coordinates"][1],
                                           f["geometry"]["coordinates"][0])]
    vervangen = len(statisch) - len(behouden)
    if vervangen:
        print(f"  ~ live status overschrijft {vervangen} statische markers")
    return live + behouden


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="charging_stations.geojson")
    args = p.parse_args()

    features, gezien = [], set()
    for feed in FEEDS:
        naam = feed["naam"]
        try:
            if feed.get("gzip"):
                locaties = haal_gzip_feed(feed["url"])
            else:
                locaties = haal_ocpi_feed(feed["url"], feed.get("token"))
        except Exception as e:
            print(f"  ! {naam}: overgeslagen ({e})", file=sys.stderr)
            continue

        n = 0
        for loc in locaties:
            f = locatie_naar_feature(loc, naam, feed.get("land"))
            if not f:
                continue
            lon, lat = f["geometry"]["coordinates"]
            sleutel = loc.get("id") or (round(lat, 5), round(lon, 5))
            if sleutel in gezien:
                continue
            gezien.add(sleutel)
            features.append(f)
            n += 1
        print(f"  + {naam}: {n} punten")

    # Belgie breed vullen via OpenStreetMap (locaties, geen live status).
    if INCLUDE_OSM_BELGIE:
        try:
            elementen = haal_osm_belgie()
        except Exception as e:
            elementen = []
            print(f"  ! OpenStreetMap Belgie: overgeslagen ({e})", file=sys.stderr)
        n = 0
        for el in elementen:
            f = osm_naar_feature(el)
            if not f:
                continue
            lon, lat = f["geometry"]["coordinates"]
            sleutel = (round(lat, 5), round(lon, 5))
            if sleutel in gezien:
                continue
            gezien.add(sleutel)
            features.append(f)
            n += 1
        print(f"  + OpenStreetMap Belgie: {n} punten")

    def _voeg_toe(nieuwe_features, label):
        n = 0
        for f in nieuwe_features:
            if not f:
                continue
            lon, lat = f["geometry"]["coordinates"]
            sleutel = (round(lat, 5), round(lon, 5))
            if sleutel in gezien:
                continue
            gezien.add(sleutel)
            features.append(f)
            n += 1
        print(f"  + {label}: {n} punten")

    if INCLUDE_FRANKRIJK:
        try:
            _voeg_toe((fr_naar_feature(x) for x in haal_frankrijk()),
                      "Frankrijk (IRVE)")
        except Exception as e:
            print(f"  ! Frankrijk (IRVE): overgeslagen ({e})", file=sys.stderr)

    if INCLUDE_LUXEMBURG:
        try:
            _voeg_toe(haal_luxemburg(), "Luxemburg (Chargy)")
        except Exception as e:
            print(f"  ! Luxemburg: overgeslagen ({e})", file=sys.stderr)

    if INCLUDE_MONTA:
        try:
            _voeg_toe((monta_site_naar_feature(s) for s in haal_monta("BE")),
                      "Monta (BE, locaties)")
        except Exception as e:
            print(f"  ! Monta: overgeslagen ({e})", file=sys.stderr)

    if INCLUDE_ENERGYVISION:
        try:
            _voeg_toe(haal_energyvision(), "EnergyVision (BE, LIVE)")
        except Exception as e:
            print(f"  ! EnergyVision: overgeslagen ({e})", file=sys.stderr)

    # Slimme fallback toepassen: live status wint van statische locaties.
    features = merge_live_over_static(features)

    geojson = {"type": "FeatureCollection", "features": features}
    with open(args.out, "w", encoding="utf-8") as f:
        # separators zonder spaties = compacter bestand
        json.dump(geojson, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Totaal {len(features)} punten -> {args.out}")


if __name__ == "__main__":
    main()
