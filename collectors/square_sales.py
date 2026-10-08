"""
collectors/square_sales.py — pull Square payments + line items per location and date range.

Two uses:
  python collectors/square_sales.py --since 2026-04-01 --until 2026-10-01 --csv sales.csv
      → one row per line item with timestamp, location, amount, item name. Hand this to the
        calibration (sales-by-hour per event).
  python collectors/square_sales.py --days 8 --post
      → weekly job: posts {"sales": [...]} to the app ingest endpoint. The app joins
        location_id + date to the kit booking → event, and fills venue_results.

Env: SQUARE_ACCESS_TOKEN (required), INGEST_URL + INGEST_KEY (for --post).
Square API version pinned via Square-Version header; bump deliberately.

What we store per line item (flat, no PII — no customer names, cards, or emails):
  payment_id, order_id, location_id, location_name, device_id, created_at (UTC ISO),
  local_hour (in the location's timezone), amount_cents, tip_cents, status,
  item_name, item_variation, category, quantity, line_total_cents, tender_type
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

API = "https://connect.squareup.com/v2"
VERSION = "2025-01-23"
TOKEN = os.environ.get("SQUARE_ACCESS_TOKEN")


def _h():
    if not TOKEN:
        sys.exit("SQUARE_ACCESS_TOKEN not set")
    return {"Authorization": f"Bearer {TOKEN}", "Square-Version": VERSION, "Content-Type": "application/json"}


def _get(path, params=None):
    for attempt in range(5):
        r = requests.get(f"{API}{path}", headers=_h(), params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def _post(path, body):
    for attempt in range(5):
        r = requests.post(f"{API}{path}", headers=_h(), json=body, timeout=30)
        if r.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


# --------------------------------------------------------------------------- locations

def locations() -> dict[str, dict]:
    out = {}
    for loc in _get("/locations").get("locations", []):
        out[loc["id"]] = {"name": loc.get("name"), "tz": loc.get("timezone") or "America/New_York",
                          "status": loc.get("status")}
    return out


# --------------------------------------------------------------------------- payments

def payments(begin: str, end: str, location_id: str) -> list[dict]:
    """All payments for one location between two RFC3339 timestamps."""
    out, cursor = [], None
    while True:
        params = {"begin_time": begin, "end_time": end, "location_id": location_id, "limit": 100,
                  "sort_order": "ASC"}
        if cursor:
            params["cursor"] = cursor
        data = _get("/payments", params)
        out.extend(data.get("payments", []))
        cursor = data.get("cursor")
        if not cursor:
            return out


def orders(order_ids: list[str]) -> dict[str, dict]:
    """Batch-retrieve orders for line items (max 100 per call)."""
    out = {}
    for i in range(0, len(order_ids), 100):
        batch = order_ids[i:i + 100]
        data = _post("/orders/batch-retrieve", {"order_ids": batch})
        for o in data.get("orders", []):
            out[o["id"]] = o
    return out


# --------------------------------------------------------------------------- flatten

def flatten(pays: list[dict], ords: dict[str, dict], loc: dict) -> list[dict]:
    rows = []
    tz = ZoneInfo(loc["tz"])
    for p in pays:
        if p.get("status") not in ("COMPLETED", "APPROVED"):
            continue
        created = p["created_at"]
        local = dt.datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(tz)
        base = {
            "payment_id": p["id"],
            "order_id": p.get("order_id"),
            "location_id": p.get("location_id"),
            "location_name": loc["name"],
            "device_id": (p.get("device_details") or {}).get("device_id"),
            "created_at": created,
            "local_date": local.date().isoformat(),
            "local_hour": local.hour,
            "amount_cents": (p.get("amount_money") or {}).get("amount", 0),
            "tip_cents": (p.get("tip_money") or {}).get("amount", 0),
            "status": p.get("status"),
            "tender_type": p.get("source_type"),
        }
        o = ords.get(p.get("order_id") or "")
        items = (o or {}).get("line_items") or []
        if not items:
            rows.append({**base, "item_name": None, "item_variation": None, "category": None,
                         "quantity": None, "line_total_cents": base["amount_cents"]})
            continue
        for li in items:
            rows.append({**base,
                         "item_name": li.get("name"),
                         "item_variation": li.get("variation_name"),
                         "category": (li.get("metadata") or {}).get("category"),
                         "quantity": li.get("quantity"),
                         "line_total_cents": (li.get("total_money") or {}).get("amount")})
    return rows


# --------------------------------------------------------------------------- main

def collect(begin: str, end: str, only_locations: list[str] | None = None) -> list[dict]:
    locs = locations()
    rows = []
    for lid, loc in locs.items():
        if only_locations and lid not in only_locations and loc["name"] not in only_locations:
            continue
        pays = payments(begin, end, lid)
        if not pays:
            continue
        ords = orders(sorted({p["order_id"] for p in pays if p.get("order_id")}))
        rows.extend(flatten(pays, ords, loc))
        print(f"{loc['name']}: {len(pays)} payments, {len(ords)} orders", file=sys.stderr)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="YYYY-MM-DD (UTC start)")
    ap.add_argument("--until", help="YYYY-MM-DD (UTC end, exclusive)")
    ap.add_argument("--days", type=int, help="alternative: last N days")
    ap.add_argument("--location", action="append", help="location id or name; repeatable")
    ap.add_argument("--csv", help="write rows to this CSV")
    ap.add_argument("--post", action="store_true", help="post {sales:[...]} to INGEST_URL")
    a = ap.parse_args()

    if a.days:
        end = dt.datetime.now(dt.timezone.utc)
        begin = end - dt.timedelta(days=a.days)
    elif a.since and a.until:
        begin = dt.datetime.fromisoformat(a.since).replace(tzinfo=dt.timezone.utc)
        end = dt.datetime.fromisoformat(a.until).replace(tzinfo=dt.timezone.utc)
    else:
        sys.exit("give --days N or --since/--until")
    rows = collect(begin.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z"), a.location)
    print(f"{len(rows)} line items", file=sys.stderr)

    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["payment_id"])
            w.writeheader(); w.writerows(rows)
    if a.post:
        url, key = os.environ.get("INGEST_URL"), os.environ.get("INGEST_KEY")
        if not (url and key):
            sys.exit("INGEST_URL / INGEST_KEY not set")
        for i in range(0, len(rows), 500):
            r = requests.post(url, headers={"x-ingest-key": key}, json={"sales": rows[i:i + 500]}, timeout=60)
            if r.status_code != 200:
                sys.exit(f"ingest failed {r.status_code}: {r.text[:200]}")
        print("posted", file=sys.stderr)


if __name__ == "__main__":
    main()
