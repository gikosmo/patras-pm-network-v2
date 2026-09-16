"""
Regenerates config.json's `station_ids` list from the network's station
spreadsheet (data/patras_station_list.csv -- a plain CSV export with
columns: SN, ID, Name, Network, Active, Location, latitude, longitude,
Purple Air map name, Site description, Mac Address, Installation Date,
Uninstalled, Owner, Associated, Notes).

A station counts as "active" (included) when its Uninstalled column is
blank or "-". Any other value (a date, "?", "check date!!!!", etc.) marks
it retired/replaced and it's excluded. Rows with no numeric ID (test or
placeholder rows, e.g. the indoor Kypseli row with no PurpleAir ID) are
also excluded, regardless of the Uninstalled column.

Coordinates come from the spreadsheet's own "latitude"/"longitude" columns
and are written into config.json's `stations` list, keyed by sensor_index.
fetch_data.py uses these to override whatever location PurpleAir itself
reports for each sensor, so the dashboard map always reflects the
spreadsheet (the network's own record of where each station actually is),
not PurpleAir's own device-reported GPS, which can be stale or imprecise.
A handful of active stations have no coordinates on file (e.g. Drepano,
Upatras Ps) -- they are still monitored, they just won't appear on the map
or in nearest-neighbor comparisons.

This replaces PatrasAir's earlier bounding-box sensor selection
(get_sensors_in_bbox). Selecting by explicit ID list instead means a
sensor is never silently dropped for sitting a few hundred meters outside
a lat/long box, and location_type (indoor/outdoor) no longer matters
either -- PurpleAir returns whatever sensor_index you ask for, regardless
of how it's registered.

Run this whenever the spreadsheet changes, then commit the updated
config.json:

    python scripts/build_station_list.py
    python scripts/build_station_list.py --csv path/to/NewExport.csv
    python scripts/build_station_list.py --dry-run   # just print, don't write config.json
"""
from __future__ import annotations

import csv
import json
import argparse
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("build_station_list")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "data" / "patras_station_list.csv"
CONFIG_PATH = ROOT / "config.json"


def _get(row: dict, *keys: str) -> str:
    """Return the first non-empty value among several possible header
    spellings (the spreadsheet export has a trailing space on 'Uninstalled ',
    and column names can shift slightly between exports)."""
    for k in keys:
        v = row.get(k)
        if v is not None and v.strip():
            return v.strip()
    return ""


def load_stations(csv_path: Path) -> tuple[list[dict], list[dict]]:
    # The spreadsheet export is sometimes UTF-8, sometimes Windows-1252/
    # Latin-1 (Excel's default "CSV" export), and can contain the odd stray
    # non-breaking-space byte. Try UTF-8 first, fall back to cp1252 rather
    # than crashing on one bad byte in an unused column.
    active, inactive = [], []
    try:
        f = csv_path.open(newline="", encoding="utf-8-sig")
        f.read()
        f.seek(0)
    except UnicodeDecodeError:
        f = csv_path.open(newline="", encoding="cp1252")
    with f:
        reader = csv.DictReader(f)
        for row in reader:
            sn = _get(row, "SN")
            sensor_id = _get(row, "ID")
            name = _get(row, "Name")
            mac = _get(row, "Mac Address")
            lat = _get(row, "latitude")
            lon = _get(row, "longitude")
            uninstalled = _get(row, "Uninstalled", "Uninstalled ")

            if not sensor_id:
                # No sensor_id at all (e.g. a fully blank row) -- nothing to do.
                continue

            entry = {
                "sensor_index": int(sensor_id) if sensor_id.isdigit() else sensor_id,
                "sn": sn or None,
                "name": name,
                "network": _get(row, "Network"),
                "uninstalled": uninstalled,
                "mac": mac,
                "latitude": lat or None,
                "longitude": lon or None,
            }

            # A row with no numeric PurpleAir ID isn't a real, queryable
            # sensor (e.g. the indoor "Kypseli (Inside)" row, which only has
            # a purpleair.com sensorlist link, no ID column value) -- skip
            # it outright regardless of the Uninstalled column.
            is_placeholder = not sensor_id.isdigit()
            is_active = uninstalled in ("", "-")

            if is_placeholder:
                log.info("Skipping row with no numeric PurpleAir ID: %s (%s)", name, sensor_id)
                continue
            if is_active and (lat in (None, "", "-") or lon in (None, "", "-")):
                log.warning("Active station %s (%s) has no coordinates on file -- "
                            "it will still be monitored but won't appear on the map "
                            "or in nearest-neighbor comparisons.", name, sensor_id)
            (active if is_active else inactive).append(entry)

    return active, inactive


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="Path to the station-list CSV")
    ap.add_argument("--dry-run", action="store_true", help="Print the result, don't write config.json")
    args = ap.parse_args()

    active, inactive = load_stations(args.csv)
    log.info("%d active station(s), %d inactive/uninstalled excluded", len(active), len(inactive))
    for e in active:
        log.info("  %s  %s  (lat=%s, lon=%s)", e["sensor_index"], e["name"], e["latitude"], e["longitude"])

    station_ids = [e["sensor_index"] for e in active]
    stations = [
        {
            "sensor_index": e["sensor_index"],
            "sn": e["sn"] if e["sn"] not in (None, "-") else None,
            "name": e["name"],
            "latitude": float(e["latitude"]) if e["latitude"] not in (None, "") else None,
            "longitude": float(e["longitude"]) if e["longitude"] not in (None, "") else None,
        }
        for e in active
    ]

    if args.dry_run:
        print(json.dumps({"station_ids": station_ids, "stations": stations}, indent=2))
        return

    config = json.loads(CONFIG_PATH.read_text())
    config.pop("bbox", None)
    config["station_source"] = {
        "_comment": "Sensor selection is an explicit ID list from the network's own "
                    "station spreadsheet, not a lat/long bounding box. Regenerate this "
                    "list with scripts/build_station_list.py whenever the spreadsheet changes.",
        "list_file": "data/patras_station_list.csv",
    }
    config["station_ids"] = station_ids
    config["stations"] = stations

    # Keep key order tidy: station_source / station_ids / stations first,
    # then whatever else was already in config.json (history, thresholds, output).
    ordered = {}
    for k in ("station_source", "station_ids", "stations"):
        ordered[k] = config.pop(k)
    ordered.update(config)

    CONFIG_PATH.write_text(json.dumps(ordered, indent=2) + "\n")
    log.info("Wrote %d station_ids + coordinates -> %s", len(station_ids), CONFIG_PATH)


if __name__ == "__main__":
    main()
