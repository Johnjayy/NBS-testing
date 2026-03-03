"""
CSWeb → ArcGIS Online Sync
Runs every 10 minutes via Heroku Scheduler.
"""

import json
import logging
import os
import time

import requests
from arcgis.features import Feature, FeatureLayer
from arcgis.geometry import Point
from arcgis.gis import GIS

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
BASE_URL   = os.environ.get("CSWEB_BASE_URL", "https://criticisable-oversubtly-muriel.ngrok-free.dev")
CSWEB_USER = os.environ.get("CSWEB_USER",     "admin")
CSWEB_PASS = os.environ.get("CSWEB_PASS",     "EvLs4777@")

AGOL_USER  = os.environ.get("AGOL_USER",      "JohnGEO1")
AGOL_PASS  = os.environ.get("AGOL_PASS",      "EvLs4777@")
AGOL_URL   = os.environ.get("AGOL_URL",       "https://www.arcgis.com")

FEATURE_LAYER_URL = os.environ.get(
    "AGOL_FEATURE_LAYER_URL",
    "https://services8.arcgis.com/oTalEaSXAuyNT7xf/arcgis/rest/services/NBS/FeatureServer/0"
)
DICTIONARY = os.environ.get("CSWEB_DICT", "QUESTIONS_DICT")


# ── 1. CSWeb token ────────────────────────────────────────────────────────────

def get_token():
    log.info("Requesting CSWeb token ...")
    resp = requests.post(
        f"{BASE_URL}/csweb/api/token",
        data={
            "grant_type":    "password",
            "client_id":     "cspro_android",
            "client_secret": "cspro",
            "username":      CSWEB_USER,
            "password":      CSWEB_PASS,
        },
        timeout=30,
    )
    if resp.status_code == 200:
        token = resp.json().get("access_token")
        log.info("  -> token obtained")
        return token
    log.error("Failed to get token: HTTP %s", resp.status_code)
    return None


# ── 2. Fetch active cases ─────────────────────────────────────────────────────

def fetch_active_cases(token):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{BASE_URL}/csweb/api/dictionaries/{DICTIONARY}/cases"
    log.info("Fetching cases from %s", url)

    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"CSWeb API error: HTTP {resp.status_code}")

    all_cases = resp.json()
    active    = [c for c in all_cases if not c.get("deleted", False)]
    log.info(
        "  -> total: %d | active: %d | deleted: %d",
        len(all_cases), len(active), len(all_cases) - len(active),
    )
    return active


# ── 3. Parse cases ────────────────────────────────────────────────────────────

def parse_cases(active_cases):
    parsed, skipped = [], 0
    for case in active_cases:
        try:
            level1 = json.loads(case["level-1"])
            qrec   = level1.get("QUESTIONS_REC", {})
            coord  = level1.get("COORDINATE", {})
            others = level1.get("OTHERS", {})

            parsed.append({
                "state_id":     qrec.get("STATE_ID",      ""),
                "lga_id":       qrec.get("LGA_ID",        ""),
                "town":         qrec.get("TOWN",           ""),
                "ea_name":      qrec.get("EA_NAME",        ""),
                "cluster":      qrec.get("CLUSTER",       None),
                "gps_lat":      coord.get("GPS_LAT",      None),
                "gps_lon":      coord.get("GPS_LON",      None),
                "gps_accuracy": coord.get("GPS_ACCURACY", None),
                "zone_id":      others.get("ZONE_ID",     ""),
                "map_id":       others.get("MAP_ID",      ""),
                "sector":       others.get("SECTOR",      ""),
            })
        except Exception as exc:
            log.warning("Skipping case: %s", exc)
            skipped += 1

    log.info("  -> parsed: %d | skipped: %d", len(parsed), skipped)
    return parsed


# ── 4. Build ArcGIS features ──────────────────────────────────────────────────

def build_features(parsed_cases):
    features, no_coords = [], 0
    for case in parsed_cases:
        lat = case.get("gps_lat")
        lon = case.get("gps_lon")
        if lat is None or lon is None:
            no_coords += 1
            continue

        geometry = Point({
            "x": float(lon),
            "y": float(lat),
            "spatialReference": {"wkid": 4326},
        })

        attributes = {
            "state_id":     case["state_id"],
            "lga_id":       case["lga_id"],
            "town":         case["town"],
            "ea_name":      case["ea_name"],
            "cluster":      case["cluster"],
            "gps_lat":      case["gps_lat"],
            "gps_lon":      case["gps_lon"],
            "gps_accuracy": case["gps_accuracy"],
            "zone_id":      case["zone_id"],
            "map_id":       case["map_id"],
            "sector":       case["sector"],
        }

        features.append(Feature(geometry=geometry, attributes=attributes))

    if no_coords:
        log.info("  -> skipped %d case(s) with no GPS coordinates", no_coords)
    log.info("  -> %d feature(s) ready for upload", len(features))
    return features


# ── 5. Upload to ArcGIS Online ────────────────────────────────────────────────

def upload_to_arcgis(features):
    if not features:
        log.info("No features to upload.")
        return

    log.info("Connecting to ArcGIS Online ...")
    gis    = GIS(AGOL_URL, AGOL_USER, AGOL_PASS)
    flayer = FeatureLayer(FEATURE_LAYER_URL, gis=gis)

    log.info("Deleting all existing features ...")
    flayer.delete_features(where="1=1")

    log.info("Adding %d feature(s) ...", len(features))
    flayer.edit_features(adds=features)
    log.info("Synced %d features to ArcGIS Online", len(features))


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    log.info("=== CSWeb -> ArcGIS Online Sync started ===")
    start = time.time()

    token = get_token()
    if not token:
        raise SystemExit("Aborting: could not obtain CSWeb token.")

    active_cases = fetch_active_cases(token)
    parsed_cases = parse_cases(active_cases)
    features     = build_features(parsed_cases)
    upload_to_arcgis(features)

    log.info("=== Done in %.1fs ===", time.time() - start)


if __name__ == "__main__":
    main()
