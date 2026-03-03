"""
CSWeb -> ArcGIS Online Sync
Uses only the 'requests' library (no arcgis SDK) for GitHub Actions compatibility.
"""

import json
import logging
import os
import time

import requests

# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# -- Config -------------------------------------------------------------------
BASE_URL   = os.environ.get("CSWEB_BASE_URL", "https://criticisable-oversubtly-muriel.ngrok-free.dev")
CSWEB_USER = os.environ.get("CSWEB_USER",     "admin")
CSWEB_PASS = os.environ.get("CSWEB_PASS",     "EvLs4777@")

AGOL_USER  = os.environ.get("AGOL_USER",      "JohnGEO1")
AGOL_PASS  = os.environ.get("AGOL_PASS",      "EvLs4777@")

FEATURE_LAYER_URL = os.environ.get(
    "AGOL_FEATURE_LAYER_URL",
    "https://services8.arcgis.com/oTalEaSXAuyNT7xf/arcgis/rest/services/NBS/FeatureServer/0"
)
DICTIONARY = os.environ.get("CSWEB_DICT", "QUESTIONS_DICT")


# -- 1. CSWeb token -----------------------------------------------------------

def get_csweb_token():
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
        log.info("  -> CSWeb token obtained")
        return token
    log.error("Failed to get CSWeb token: HTTP %s - %s", resp.status_code, resp.text)
    return None


# -- 2. ArcGIS Online token ---------------------------------------------------

def get_agol_token():
    log.info("Requesting ArcGIS Online token ...")
    resp = requests.post(
        "https://www.arcgis.com/sharing/rest/generateToken",
        data={
            "username":   AGOL_USER,
            "password":   AGOL_PASS,
            "referer":    "https://www.arcgis.com",
            "expiration": 60,
            "f":          "json",
        },
        timeout=30,
    )
    data = resp.json()
    if "token" in data:
        log.info("  -> ArcGIS token obtained")
        return data["token"]
    log.error("Failed to get ArcGIS token: %s", data)
    return None


# -- 3. Fetch active cases ----------------------------------------------------

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


# -- 4. Parse cases -----------------------------------------------------------

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


# -- 5. Build features --------------------------------------------------------

def build_features(parsed_cases):
    features, no_coords = [], 0
    for case in parsed_cases:
        lat = case.get("gps_lat")
        lon = case.get("gps_lon")
        if lat is None or lon is None:
            no_coords += 1
            continue

        features.append({
            "geometry": {
                "x": float(lon),
                "y": float(lat),
                "spatialReference": {"wkid": 4326},
            },
            "attributes": {
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
        })

    if no_coords:
        log.info("  -> skipped %d case(s) with no GPS coordinates", no_coords)
    log.info("  -> %d feature(s) ready for upload", len(features))
    return features


# -- 6. Upload to ArcGIS Online -----------------------------------------------

def upload_to_arcgis(features, agol_token):
    if not features:
        log.info("No features to upload.")
        return

    # Delete all existing features
    log.info("Deleting all existing features ...")
    del_resp = requests.post(
        f"{FEATURE_LAYER_URL}/deleteFeatures",
        data={
            "where": "1=1",
            "f":     "json",
            "token": agol_token,
        },
        timeout=60,
    )
    log.info("  -> delete result: %s", del_resp.json())

    # Add new features in batches of 500
    batch_size = 500
    total_added = 0
    for i in range(0, len(features), batch_size):
        batch = features[i:i + batch_size]
        log.info("Adding batch %d (%d features) ...", i // batch_size + 1, len(batch))
        add_resp = requests.post(
            f"{FEATURE_LAYER_URL}/addFeatures",
            data={
                "features": json.dumps(batch),
                "f":        "json",
                "token":    agol_token,
            },
            timeout=60,
        )
        result = add_resp.json()
        added  = sum(1 for r in result.get("addResults", []) if r.get("success"))
        total_added += added
        log.info("  -> added: %d", added)

    log.info("Synced %d features to ArcGIS Online", total_added)


# -- Entry point --------------------------------------------------------------

def main():
    log.info("=== CSWeb -> ArcGIS Online Sync started ===")
    start = time.time()

    csweb_token = get_csweb_token()
    if not csweb_token:
        raise SystemExit("Aborting: could not obtain CSWeb token.")

    agol_token = get_agol_token()
    if not agol_token:
        raise SystemExit("Aborting: could not obtain ArcGIS token.")

    active_cases = fetch_active_cases(csweb_token)
    parsed_cases = parse_cases(active_cases)
    features     = build_features(parsed_cases)
    upload_to_arcgis(features, agol_token)

    log.info("=== Done in %.1fs ===", time.time() - start)


if __name__ == "__main__":
    main()
