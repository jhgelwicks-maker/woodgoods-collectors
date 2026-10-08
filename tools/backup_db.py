"""Download a full copy of the Woodgoods Ops database to this Mac.

    python tools/backup_db.py

Settings: ~/WoodGoodsAI/.export.env (EXPORT_KEY=..., optional EXPORT_URL=...), never committed.
Output:   ~/WoodGoodsAI/backup/db/current/<table>.jsonl  every row, every column (restore copy)
          ~/WoodGoodsAI/backup/db/current/event_history.csv, event_candidates.csv  main columns, for Excel/Numbers
          ~/WoodGoodsAI/backup/db/previous/                the copy before this one
          ~/WoodGoodsAI/backup/logs/db-backup.log           one line per run
A new copy only replaces the old one after every table downloaded completely."""
import os, sys, csv, json, time, shutil, datetime, urllib.request, urllib.parse, urllib.error

HOME = os.path.expanduser("~/WoodGoodsAI")
ENV_FILE = os.path.join(HOME, ".export.env")
ROOT = os.path.join(HOME, "backup", "db")
LOG = os.path.join(HOME, "backup", "logs", "db-backup.log")
PAGE = 1000   # the database caps every response at 1,000 rows
CSV_COLS = {
    "event_history": ["dedupe_key", "source", "short_id", "name", "sport", "organizer_name", "start_date", "end_date", "city", "state",
                      "team_count", "division_count", "venue_count", "fields_used", "teams_per_field", "schedule_quality",
                      "age_groups", "gender", "listing_url", "first_seen_at", "last_seen_at"],
    "event_candidates": ["dedupe_key", "source", "name", "sport", "organizer_name", "start_date", "end_date", "venue_name", "city", "state",
                         "team_count", "expected_attendance", "schedule_quality", "sun_score", "fit_score", "tags", "listing_url",
                         "first_seen_at", "last_seen_at"],
}

def settings():
    env = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1); env[k.strip()] = v.strip().strip('"')
    if not env.get("EXPORT_KEY"):
        sys.exit(f"EXPORT_KEY missing: put it in {ENV_FILE}")
    return env.get("EXPORT_URL", "https://opswg.lovable.app/api/public/export"), env["EXPORT_KEY"]

def get(url, key, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url + ("?" + q if q else ""), headers={"x-export-key": key, "user-agent": "woodgoods-backup/1.0"})
    for i in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 400): raise RuntimeError(f"export refused: {e.code} {e.read()[:200]!r}")
            err = e
        except Exception as e:  # network blips: retry
            err = e
        time.sleep(5 * (i + 1))
    raise RuntimeError(f"export failed after retries: {err}")

def cell(v):
    if isinstance(v, (dict, list)): v = json.dumps(v)
    s = "" if v is None else str(v)
    return s[:32000]   # spreadsheet cell limit; the .jsonl copy keeps everything

def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M} {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f: f.write(line + "\n")

def main():
    url, key = settings()
    t0 = time.time()
    tmp = os.path.join(ROOT, "incoming")
    shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    try:
        tables = get(url, key)["tables"]
        counts = {}
        for t in tables:
            name, n, after = t["name"], 0, None
            with open(os.path.join(tmp, f"{name}.jsonl"), "w") as f:
                writer = None
                if name in CSV_COLS:
                    cf = open(os.path.join(tmp, f"{name}.csv"), "w", newline="")
                    writer = csv.writer(cf); writer.writerow(CSV_COLS[name])
                while True:
                    page = get(url, key, table=name, after=after, limit=PAGE)
                    for r in page["rows"]:
                        f.write(json.dumps(r) + "\n")
                        if writer: writer.writerow([cell(r.get(c)) for c in CSV_COLS[name]])
                    n += len(page["rows"]); after = page.get("next_after")
                    if not after: break
                if writer: cf.close()
            counts[name] = n
            if n < t["rows"]:
                raise RuntimeError(f"{name}: got {n} rows, app reported {t['rows']}")
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"FAILED, previous backup kept: {e}")
        sys.exit(1)
    cur, prev = os.path.join(ROOT, "current"), os.path.join(ROOT, "previous")
    shutil.rmtree(prev, ignore_errors=True)
    if os.path.exists(cur): os.rename(cur, prev)
    os.rename(tmp, cur)
    big = ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:4])
    log(f"ok: {len(counts)} tables, {sum(counts.values()):,} rows in {(time.time() - t0) / 60:.1f} min ({big})")

if __name__ == "__main__":
    main()
