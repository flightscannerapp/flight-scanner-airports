#!/usr/bin/env python3
"""Build compact jsDelivr airport/runway JSON shards from OurAirports CSV files.

Alias collisions are resolved by code type so globally recognised airport codes
cannot be overwritten by weaker local identifiers. In particular, three-letter
IATA codes and four-letter ICAO codes win over GPS/ident/local-code collisions.

Example:
  python build_airport_runway_shards.py \
      --airports airports.csv \
      --runways runways.csv \
      --output airport-data
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def clean_code(value: str | None) -> str:
    return "".join(ch for ch in (value or "").strip().upper() if ch.isalnum())[:8]


def parse_float(value: str | None) -> float | None:
    try:
        result = float((value or "").strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_int(value: str | None) -> int | None:
    number = parse_float(value)
    return int(round(number)) if number is not None else None


def rounded(value: float) -> float:
    return round(value, 6)


def alias_priority(kind: str, code: str) -> int:
    """Return the precedence for one alias source.

    Standard-length IATA/ICAO identifiers get the strongest scores. This makes
    HAJ resolve to Hannover (IATA HAJ / ICAO EDDV) even when another airport has
    HAJ only as a local code. Local codes remain searchable when no stronger
    identifier owns the same alias.
    """
    if kind == "iata":
        return 500 if len(code) == 3 else 450
    if kind == "icao":
        return 500 if len(code) == 4 else 450
    if kind == "gps":
        return 400
    if kind == "ident":
        return 350
    if kind == "local":
        return 100
    return 0


def add_alias(aliases: dict[str, int], value: str | None, kind: str) -> None:
    code = clean_code(value)
    if not code:
        return
    priority = alias_priority(kind, code)
    previous = aliases.get(code)
    if previous is None or priority > previous:
        aliases[code] = priority


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--airports", required=True, type=Path)
    parser.add_argument("--runways", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    airport_rows: dict[str, dict[str, Any]] = {}
    with args.airports.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ident = clean_code(row.get("ident"))
            lat = parse_float(row.get("latitude_deg"))
            lon = parse_float(row.get("longitude_deg"))
            if not ident or lat is None or lon is None:
                continue
            if (row.get("type") or "").strip().lower() == "closed_airport":
                continue

            iata = clean_code(row.get("iata_code"))
            icao = clean_code(row.get("icao_code"))
            gps = clean_code(row.get("gps_code"))
            local = clean_code(row.get("local_code"))

            aliases: dict[str, int] = {}
            add_alias(aliases, iata, "iata")
            add_alias(aliases, icao, "icao")
            add_alias(aliases, gps, "gps")
            add_alias(aliases, ident, "ident")
            add_alias(aliases, local, "local")

            airport_rows[ident] = {
                "n": (row.get("name") or ident).strip()[:80],
                "i": iata,
                "c": icao or gps or ident,
                "lat": rounded(lat),
                "lon": rounded(lon),
                "e": parse_int(row.get("elevation_ft")),
                "r": [],
                "aliases": aliases,
            }

    with args.runways.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            ident = clean_code(row.get("airport_ident"))
            airport = airport_rows.get(ident)
            if airport is None or (row.get("closed") or "").strip() == "1":
                continue

            le_lat = parse_float(row.get("le_latitude_deg"))
            le_lon = parse_float(row.get("le_longitude_deg"))
            he_lat = parse_float(row.get("he_latitude_deg"))
            he_lon = parse_float(row.get("he_longitude_deg"))
            if None in (le_lat, le_lon, he_lat, he_lon):
                continue

            le = clean_code(row.get("le_ident"))
            he = clean_code(row.get("he_ident"))
            if not le and not he:
                continue

            airport["r"].append([
                le,
                he,
                rounded(le_lat),
                rounded(le_lon),
                rounded(he_lat),
                rounded(he_lon),
            ])

    shards: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    shard_priorities: dict[str, dict[str, int]] = defaultdict(dict)
    airport_count = 0
    runway_count = 0
    collision_count = 0
    stronger_replacements = 0

    for airport in airport_rows.values():
        aliases: dict[str, int] = airport.pop("aliases")
        if not aliases:
            continue

        airport["r"] = airport["r"][:16]
        if airport["e"] is None:
            airport.pop("e")
        airport_count += 1
        runway_count += len(airport["r"])

        for alias, priority in sorted(aliases.items()):
            shard = alias[0]
            if not shard.isalnum():
                continue

            previous_priority = shard_priorities[shard].get(alias)
            if previous_priority is None:
                shards[shard][alias] = airport
                shard_priorities[shard][alias] = priority
                continue

            collision_count += 1
            if priority > previous_priority:
                shards[shard][alias] = airport
                shard_priorities[shard][alias] = priority
                stronger_replacements += 1
            # Equal or weaker aliases do not overwrite the existing airport.

    alias_count = sum(len(payload) for payload in shards.values())

    args.output.mkdir(parents=True, exist_ok=True)
    for shard in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        payload = shards.get(shard, {})
        path = args.output / f"{shard}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

    manifest = {
        "format": 1,
        "airports": airport_count,
        "aliases": alias_count,
        "runways": runway_count,
        "shards": 36,
        "collisions": collision_count,
        "stronger_alias_replacements": stronger_replacements,
        "source": "OurAirports public-domain data",
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
