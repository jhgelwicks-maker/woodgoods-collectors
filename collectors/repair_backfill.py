"""Repair rows already collected by the backfill, using the saved files in ~/WoodGoodsAI/backup/backfill.

Two defects from the first full run (2026-10-07/08):
  - venue names cut at an apostrophe ("Afrim\\" for "Afrim's Sports Park"); those events are re-crawled
    because their per-venue team counts depended on the venue name too
  - no state when the address had a double comma, lowercase state or no street; re-parsed from the saved address

Repaired rows are written back into the shard files (so a later replay is correct) and posted to the app.

    python -m collectors.repair_backfill [--dry-run] [--parallel 4]"""
import os, sys, json, glob, argparse, datetime, time
from concurrent.futures import ThreadPoolExecutor
from . import common, backfill_tourneymachine as bf
from .common import log

BACKUP_DIR = os.path.expanduser("~/WoodGoodsAI/backup/backfill")

def truncated(r): return any((v.get("venue_name") or "").endswith("\\") for v in r.get("venues") or [])

def reparse_state(r):
    """Fill city_states/state/fingerprint from the saved venue addresses with the fixed parser. Returns True if changed."""
    if r.get("state"): return False
    cs = []
    for v in r.get("venues") or []:
        c = bf.addr_city_state(v.get("address"))
        if c and c not in cs: cs.append(c)
    if not cs: return False
    st = cs[0].split(", ")[-1]
    r["city_states"] = cs; r["state"] = st
    r["event_fingerprint"] = common.fingerprint(r.get("name"), r.get("sport"), st)
    return True

def reclean_clubs(r):
    """Re-run the (improved) club-name cleaning on the saved club roots and merge duplicates. Returns True if changed."""
    clubs = r.get("clubs")
    if not clubs: return False
    merged = {}
    for c in clubs:
        root = common.club_from_team(c.get("club"))
        if root: merged[root] = merged.get(root, 0) + (c.get("teams") or 0)
    new = [{"club": k, "teams": v} for k, v in sorted(merged.items(), key=lambda kv: -kv[1])] or None
    if new == clubs: return False
    r["clubs"] = new; r["club_count"] = len(new) if new else None
    return True

def recrawl(n):
    try:
        rows, _ = bf.collect(n, n + 1)
        return n, (rows[0] if rows else None)
    except Exception as e:  # noqa: BLE001
        log.warning("R%d recrawl failed: %s", n, e); return n, None

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--reclean", action="store_true", help="only re-clean club names and drop junk-named rows; no re-crawl")
    o = ap.parse_args()
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "R*.jsonl")))
    shards = {f: [json.loads(l) for l in open(f) if l.strip()] for f in files}
    need_crawl = [] if o.reclean else [r["short_id"] for rows in shards.values() for r in rows if truncated(r)]
    junk = 0
    if o.reclean:
        for f in shards:
            keep = [r for r in shards[f] if not bf.JUNK_NAME.search(r.get("name") or "")]
            junk += len(shards[f]) - len(keep); shards[f] = keep
        log.info("%d junk-named rows dropped from the files (delete them in the app separately)", junk)
    log.info("%d files, %d rows; %d events to re-crawl", len(files), sum(map(len, shards.values())), len(need_crawl))
    fixed = {}
    if need_crawl and not o.dry_run:
        with ThreadPoolExecutor(o.parallel) as ex:
            for i, (n, row) in enumerate(ex.map(recrawl, need_crawl), 1):
                if row:
                    row = {k: (v.isoformat() if isinstance(v, datetime.date) else v) for k, v in row.items()}
                    fixed[n] = row
                if i % 100 == 0: log.info("re-crawled %d/%d", i, len(need_crawl))
    changed, state_fixed = [], 0
    for f, rows in shards.items():
        dirty = False
        for i, r in enumerate(rows):
            if r["short_id"] in fixed:
                rows[i] = fixed[r["short_id"]]; changed.append(rows[i]); dirty = True; continue
            if reparse_state(r):
                state_fixed += 1; changed.append(r); dirty = True
            elif o.reclean and reclean_clubs(r):
                changed.append(r); dirty = True
        if junk and not o.dry_run: dirty = True
        if dirty and not o.dry_run:
            tmp = f + ".tmp"
            with open(tmp, "w") as out:
                for r in rows: out.write(json.dumps(r, default=str) + "\n")
            os.replace(tmp, f)
    log.info("repaired: %d re-crawled, %d states filled, %d rows to send", len(fixed), state_fixed, len(changed))
    if o.dry_run or not changed: return
    db = common.Supabase(); started = datetime.datetime.utcnow().isoformat() + "Z"
    for i in range(0, len(changed), 150):
        db._post({"source": "tourneymachine-history", "history": changed[i:i+150]})
    db.log_run("tourneymachine-history repair", len(changed), len(changed), len(need_crawl) - len(fixed), started,
               note=f"shard backfill repair: {len(fixed)} venue names re-crawled, {state_fixed} states filled, "
                    f"{len(changed) - len(fixed) - state_fixed} club lists re-cleaned")
    log.info("sent %d repaired rows", len(changed))

if __name__ == "__main__":
    main()
