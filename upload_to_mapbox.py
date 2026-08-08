# -*- coding: utf-8 -*-
"""
Upload charging_stations.geojson naar een VASTE Mapbox-tileset (Uploads API).

Zo verandert de tileset-ID nooit: elke run vervangt de data van dezelfde
tileset (kevin8908.<MAPBOX_TILESET_ID>). Bedoeld om automatisch te draaien
in GitHub Actions na fetch_charging_data.py.

Env vars (worden in GitHub als secrets gezet):
    MAPBOX_USERNAME      bv. "kevin8908"
    MAPBOX_SECRET_TOKEN  een 'sk.'-token met scope uploads:write (+ tilesets:write)
    MAPBOX_TILESET_ID    het deel na de punt, bv. "charging_stations" (<=32 tekens)
    GEOJSON_FILE         optioneel, standaard "charging_stations.geojson"
"""

import os
import sys
import time

import boto3
import requests

USERNAME = os.environ["MAPBOX_USERNAME"]
TOKEN = os.environ["MAPBOX_SECRET_TOKEN"]
TILESET = os.environ.get("MAPBOX_TILESET_ID", "charging_stations")
GEOJSON = os.environ.get("GEOJSON_FILE", "charging_stations.geojson")
API = "https://api.mapbox.com/uploads/v1"


def main():
    # 1) Tijdelijke S3-credentials ophalen bij Mapbox.
    r = requests.get(f"{API}/{USERNAME}/credentials",
                     params={"access_token": TOKEN}, timeout=30)
    r.raise_for_status()
    c = r.json()

    # 2) Het GeoJSON-bestand naar de Mapbox-staging-bucket uploaden.
    s3 = boto3.client(
        "s3",
        aws_access_key_id=c["accessKeyId"],
        aws_secret_access_key=c["secretAccessKey"],
        aws_session_token=c["sessionToken"],
        region_name="us-east-1")
    with open(GEOJSON, "rb") as f:
        s3.put_object(Bucket=c["bucket"], Key=c["key"], Body=f)
    print(f"Geupload naar staging: {GEOJSON}")

    # 3) Upload-taak aanmaken -> vult/vervangt de vaste tileset.
    doel = f"{USERNAME}.{TILESET}"
    r = requests.post(
        f"{API}/{USERNAME}",
        params={"access_token": TOKEN},
        json={"url": c["url"], "tileset": doel, "name": "charging_stations"},
        timeout=30)
    r.raise_for_status()
    upload_id = r.json()["id"]
    print(f"Verwerking gestart voor tileset {doel} (id {upload_id})")

    # 4) Wachten tot Mapbox klaar is met verwerken.
    for _ in range(90):
        time.sleep(10)
        s = requests.get(f"{API}/{USERNAME}/{upload_id}",
                         params={"access_token": TOKEN}, timeout=30).json()
        if s.get("error"):
            sys.exit(f"Mapbox-fout: {s['error']}")
        if s.get("complete"):
            print(f"Klaar! Tileset bijgewerkt: mapbox://{doel}")
            return
        print(f"  bezig… voortgang {s.get('progress')}")
    print("Nog niet klaar na wachttijd — Mapbox verwerkt mogelijk nog door.")


if __name__ == "__main__":
    main()
