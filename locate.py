#!/usr/bin/env python3
"""
Triangulate a MeshCore source from observations stored by collector.py.

Usage:
  ./locate.py --target <pubkey-prefix>     # locate one
  ./locate.py --validate                   # run against every GPS-known target,
                                           # report per-target error and aggregate

Algorithm: multi-anchor chain-walk + weighted geometric median (Weiszfeld).
See README.md for the rationale.
"""
from __future__ import annotations

import argparse
import configparser
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

WEISZFELD_ITERS = 200


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def load_config(path: str = "config.ini") -> dict:
    p = configparser.ConfigParser()
    if not Path(path).exists():
        print(f"[!] Config file '{path}' not found. Copy config.example.ini.", file=sys.stderr)
        sys.exit(2)
    p.read(path)
    return {
        "db_path": p.get("storage", "db_path", fallback="./meshcore_data.db"),
        "max_rf_km": p.getfloat("locator", "max_rf_km", fallback=35.0),
        "days_lookback": p.getint("locator", "days_lookback", fallback=14),
        "min_observers": p.getint("locator", "min_observers", fallback=2),
    }


def open_db(db_path: str) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def load_relay_index(db: sqlite3.Connection) -> tuple[dict, dict]:
    """Returns (one_byte_index, full_pubkey_gps) for chain-walk lookups.

    one_byte_index: {hex_byte: [(public_key, lat, lng), ...]}
                    All known repeaters indexed by the FIRST byte of pubkey.
    full_pubkey_gps: {public_key: (lat, lng)} for receiver lookups
    """
    one_byte: dict[str, list] = defaultdict(list)
    full: dict[str, tuple[float, float]] = {}
    rows = db.execute(
        "SELECT public_key, lat, lng FROM contacts "
        "WHERE lat IS NOT NULL AND lat != 0 AND lng IS NOT NULL AND lng != 0"
    ).fetchall()
    for r in rows:
        pk = r["public_key"].lower()
        full[pk] = (r["lat"], r["lng"])
        one_byte[pk[:2]].append((pk, r["lat"], r["lng"]))
    return one_byte, full


def parse_path(path_json: str | None) -> list[str]:
    """Parse a path_json field. Returns list of lowercase 1-byte hex hashes.

    Accepts both standard JSON arrays (`["6A","EA"]`) and the comma-separated
    form some brokers emit (`6a,ea`).
    """
    if not path_json:
        return []
    s = path_json.strip()
    if not s or s == "[]":
        return []
    if s.startswith("["):
        try:
            arr = json.loads(s)
        except json.JSONDecodeError:
            return []
        return [str(x).lower() for x in arr if isinstance(x, str)]
    return [seg.strip().lower() for seg in s.split(",") if seg.strip()]


def locate(
    db: sqlite3.Connection,
    target_prefix: str,
    cfg: dict,
) -> dict | None:
    """Run chain-walk multi-anchor triangulation.

    Returns dict with 'name', 'estimate' (lat,lng), 'n_observers_used',
    'n_paths_used', 'n_h0_candidates', 'n_direct'. Optional 'actual' and
    'error_km' if the target self-advertises GPS. Returns:
      - None: target prefix doesn't match any contact
      - {'no_data': True, 'name': ...}: no observations of this target in window
      - {'no_chain': True, 'name': ...}: every chain rejected
    """
    target_prefix = target_prefix.lower()

    # Resolve target
    row = db.execute(
        "SELECT public_key, name, role, lat, lng FROM contacts "
        "WHERE public_key LIKE ? LIMIT 1",
        (target_prefix + "%",),
    ).fetchone()
    if row is None:
        return None
    target_pk = row["public_key"].lower()
    target_name = row["name"] or "?"
    actual_lat = row["lat"]
    actual_lng = row["lng"]

    # Pull observations within lookback window
    obs = db.execute(
        f"""
        SELECT receiver_pk, path_json
        FROM observations
        WHERE source_pk = ?
          AND timestamp >= datetime('now','-{cfg['days_lookback']} days')
        """,
        (target_pk,),
    ).fetchall()
    if not obs:
        return {"name": target_name, "no_data": True}

    one_byte_index, full_pk_gps = load_relay_index(db)

    h0_counts: dict[str, dict] = {}
    direct_counts: dict[str, int] = defaultdict(int)
    observers_used: set[str] = set()
    paths_used = 0

    for r in obs:
        receiver = r["receiver_pk"].lower()
        recv_gps = full_pk_gps.get(receiver)
        if recv_gps is None:
            continue
        recv_lat, recv_lng = recv_gps

        path = parse_path(r["path_json"])
        # Strip self-references (some brokers include the source byte)
        path = [h for h in path if not target_pk.startswith(h)]

        if not path:
            direct_counts[receiver] += 1
            observers_used.add(receiver)
            paths_used += 1
            continue

        # Walk right-to-left from receiver. For each hop, pick the GPS-known
        # repeater whose pubkey starts with that byte AND whose location is
        # closest to the previously resolved hop.
        prev_lat, prev_lng = recv_lat, recv_lng
        chain_ok = True
        h0_pk = None
        h0_lat = h0_lng = 0.0
        for i in range(len(path) - 1, -1, -1):
            byte_key = path[i]
            cands = one_byte_index.get(byte_key, [])
            if not cands:
                chain_ok = False
                break
            # Disambiguate by distance to previous hop
            best = min(cands, key=lambda c: haversine_km(c[1], c[2], prev_lat, prev_lng))
            d = haversine_km(best[1], best[2], prev_lat, prev_lng)
            if d > cfg["max_rf_km"]:
                chain_ok = False
                break
            if i == 0:
                h0_pk, h0_lat, h0_lng = best
            prev_lat, prev_lng = best[1], best[2]

        if not chain_ok or h0_pk is None:
            continue

        if h0_pk not in h0_counts:
            h0_counts[h0_pk] = {"lat": h0_lat, "lng": h0_lng, "n_paths": 0}
        h0_counts[h0_pk]["n_paths"] += 1
        observers_used.add(receiver)
        paths_used += 1

    if not h0_counts and not direct_counts:
        return {"name": target_name, "no_chain": True}

    # Build the constraint pool
    pts = list(h0_counts.values())
    for obs_pk, n in direct_counts.items():
        ola, olg = full_pk_gps[obs_pk]
        pts.append({"lat": ola, "lng": olg, "n_paths": n})

    # Weighted geometric median (Weiszfeld iteration)
    x = sum(p["lat"] for p in pts) / len(pts)
    y = sum(p["lng"] for p in pts) / len(pts)
    for _ in range(WEISZFELD_ITERS):
        nx = ny = den = 0.0
        for p in pts:
            d = max(haversine_km(x, y, p["lat"], p["lng"]), 0.05)
            w = math.log(p["n_paths"] + 1) / d
            nx += p["lat"] * w
            ny += p["lng"] * w
            den += w
        nx, ny = nx / den, ny / den
        if abs(nx - x) < 1e-7 and abs(ny - y) < 1e-7:
            break
        x, y = nx, ny

    result: dict = {
        "name": target_name,
        "estimate": (round(x, 5), round(y, 5)),
        "n_observers_used": len(observers_used),
        "n_paths_used": paths_used,
        "n_h0_candidates": len(h0_counts),
        "n_direct": sum(direct_counts.values()),
    }
    if actual_lat is not None and actual_lng is not None and actual_lat != 0:
        result["actual"] = (actual_lat, actual_lng)
        result["error_km"] = round(haversine_km(x, y, actual_lat, actual_lng), 2)
    return result


def cmd_target(db, prefix: str, cfg: dict) -> None:
    r = locate(db, prefix, cfg)
    if r is None:
        print(f"[!] No contact found matching prefix '{prefix}'")
        return
    if r.get("no_data"):
        print(f"[!] No observations of {r['name']} in last {cfg['days_lookback']} days")
        return
    if r.get("no_chain"):
        print(f"[!] {r['name']}: every chain rejected (no GPS-known relays in path, "
              f"or chains exceed max_rf_km={cfg['max_rf_km']})")
        return
    lat, lng = r["estimate"]
    print(f"  Target          : {r['name']}")
    print(f"  Estimate        : ({lat}, {lng})")
    print(f"  Observers used  : {r['n_observers_used']}")
    print(f"  Paths used      : {r['n_paths_used']}")
    print(f"  H0 candidates   : {r['n_h0_candidates']}")
    print(f"  Direct heard    : {r['n_direct']}")
    if "error_km" in r:
        print(f"  Actual          : {r['actual']}")
        print(f"  Error           : {r['error_km']} km")
    print(f"  Google Maps     : https://maps.google.com/?q={lat:.5f},{lng:.5f}")


def cmd_validate(db, cfg: dict) -> None:
    rows = db.execute(
        f"""
        SELECT o.source_pk AS pk, COUNT(DISTINCT o.receiver_pk) AS n_obs,
               c.name, c.role, c.lat, c.lng
        FROM observations o
        JOIN contacts c ON c.public_key = o.source_pk
        WHERE o.source_pk IS NOT NULL
          AND c.lat IS NOT NULL AND c.lat != 0
          AND o.timestamp >= datetime('now','-{cfg['days_lookback']} days')
        GROUP BY o.source_pk
        HAVING n_obs >= {cfg['min_observers']}
        ORDER BY n_obs DESC
        """
    ).fetchall()

    print(f"Validating against {len(rows)} GPS-known targets with ≥{cfg['min_observers']} observers")
    print()
    print(f"  {'name':<30} {'obs':>4} {'used':>4} {'h0':>3} {'dir':>3} {'err_km':>7}")
    print(f"  {'-'*30} {'-'*4} {'-'*4} {'-'*3} {'-'*3} {'-'*7}")
    errs: list[float] = []
    for c in rows:
        r = locate(db, c["pk"], cfg)
        if not r or r.get("no_chain") or r.get("no_data"):
            print(f"  {(c['name'] or '?')[:30]:<30} {c['n_obs']:>4} "
                  f"{0:>4} {0:>3} {0:>3} {'-':>7}")
            continue
        err = haversine_km(*r["estimate"], c["lat"], c["lng"])
        errs.append(err)
        print(f"  {(c['name'] or '?')[:30]:<30} {c['n_obs']:>4} "
              f"{r['n_observers_used']:>4} {r['n_h0_candidates']:>3} {r['n_direct']:>3} "
              f"{err:>7.2f}")

    if errs:
        s = sorted(errs)
        print()
        print(f"=== Aggregate over {len(errs)} targets ===")
        print(f"  median err : {statistics.median(s):.2f} km")
        print(f"  mean err   : {statistics.mean(s):.2f} km")
        print(f"  ≤  1 km    : {sum(1 for e in s if e <= 1)}")
        print(f"  ≤  5 km    : {sum(1 for e in s if e <= 5)}")
        print(f"  ≤ 10 km    : {sum(1 for e in s if e <= 10)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.ini")
    ap.add_argument("--target", help="Pubkey prefix of target to locate (≥4 hex chars)")
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    cfg = load_config(args.config)
    db = open_db(cfg["db_path"])

    if not args.target and not args.validate:
        ap.error("specify --target or --validate")
    if args.target:
        cmd_target(db, args.target, cfg)
    if args.validate:
        cmd_validate(db, cfg)


if __name__ == "__main__":
    main()
