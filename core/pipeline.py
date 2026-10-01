"""Validate -> dedupe -> sink.

The philosophy matches the Woodgoods Ops design: collectors pull
everything unfiltered, filters get applied downstream. But 'unfiltered'
must not mean 'unvalidated' - three of these organizers publish events
with years already in the past (stale copy from a prior season), and
those will silently poison the table if nothing catches them.
"""
from __future__ import annotations
import os, json, logging, datetime as dt
from collections import Counter

from core.venues import enrich

_SHOWCASE_RE = None


def infer_event_type(ev):
    """Normalize event_type from the name. A showcase is a different product
    from a tournament - fewer teams, different foot traffic - and the dedupe
    layer relies on the distinction."""
    import re as _re
    n = (ev.name or "").lower()
    if _re.search(r"\bshowcase|\bclinic\b|\bcombine\b|\bprospect day\b", n):
        ev.event_type = "showcase"
    elif _re.search(r"\bcamp\b|\btraining\b", n):
        ev.event_type = "camp"
    elif _re.search(r"\bleague\b", n):
        ev.event_type = "league"
    elif not ev.event_type:
        ev.event_type = "tournament"
    return ev

log = logging.getLogger("collectors")

# Anything older than this is almost certainly stale site copy, not a real
# past event. Flagged, not dropped - a human should see it.
STALE_DAYS = 30


class Validator:
    def __init__(self, today=None, horizon_days=540):
        self.today = today or dt.date.today()
        self.horizon = self.today + dt.timedelta(days=horizon_days)
        self.stats = Counter()

    def check(self, ev):
        """Return (ok, flags). ok=False means don't write it."""
        flags = []

        if not ev.name or len(ev.name) < 3:
            self.stats["reject_no_name"] += 1
            return False, ["no_name"]

        if not ev.sport:
            # Should be impossible - BaseCollector.run() stamps it - but a
            # row with no sport would be unroutable once other sports land
            # in the same table.
            self.stats["reject_no_sport"] += 1
            return False, ["no_sport"]

        if not ev.start_date:
            self.stats["reject_no_date"] += 1
            return False, ["no_date"]

        age = (self.today - ev.start_date).days
        if age > STALE_DAYS:
            # The MLT 'Feb 6-7 2026' / Trilogy 'Jan 2026' class of bug.
            self.stats["flag_stale_date"] += 1
            flags.append(f"stale_date:{ev.start_date.isoformat()}")

        if ev.start_date > self.horizon:
            self.stats["flag_far_future"] += 1
            flags.append("far_future")

        if ev.end_date and ev.end_date < ev.start_date:
            ev.end_date = ev.start_date
            flags.append("end_before_start")

        if ev.end_date and (ev.end_date - ev.start_date).days > 14:
            self.stats["flag_long_span"] += 1
            flags.append("long_span")

        if not ev.state:
            self.stats["flag_no_state"] += 1
            flags.append("no_state")        # can't route an operator without it

        if ev.gender == "unknown":
            self.stats["flag_gender_unknown"] += 1
            flags.append("gender_unknown")

        self.stats["accepted"] += 1
        return True, flags


def dedupe(events):
    """Collapse by fingerprint, preferring the record with the most fields."""
    best = {}
    for ev in events:
        score = sum(1 for v in (ev.venue, ev.city, ev.state, ev.age_range,
                                ev.team_count, ev.end_date) if v)
        cur = best.get(ev.fingerprint)
        if cur is None or score > cur[0]:
            best[ev.fingerprint] = (score, ev)
    return [e for _, e in best.values()]


def process(events, validator=None, existing_rows=None, cross_source=True):
    """enrich -> validate -> exact dedupe -> cross-source dedupe.

    existing_rows: rows already in Supabase (e.g. from the Tourney Machine
    collector). Pass them in and this run will merge against them instead of
    creating parallel rows for the same real-world event.

    Returns (rows, rejected).
    """
    validator = validator or Validator()
    rows, rejected = [], []
    for ev in events:
        enrich(ev)
        infer_event_type(ev)
        ok, flags = validator.check(ev)
        row = ev.to_row()
        row["flags"] = flags
        row["source_type"] = "organizer_site"
        row["scraped_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (rows if ok else rejected).append(row)

    rows = dedupe_rows(rows)                      # exact-fingerprint pass

    if cross_source:
        from core.dedupe import cluster
        pool = rows + list(existing_rows or [])
        merged, _, site_reports = cluster(pool)
        log.info("cross-source dedupe: %d -> %d event-sites (%d corroborated)",
                 len(pool), len(merged),
                 sum(1 for m in merged if m.get("source_count", 1) > 1))
        if site_reports:
            log.warning("%d event(s) where sources DISAGREE on the site list:",
                        len(site_reports))
            for rep in site_reports[:10]:
                log.warning("   %s  %s (%s)", rep["start_date"],
                            rep["name"], rep["organizer"])
                for src, venues in rep["by_source"].items():
                    log.warning("       %-16s %s", src, ", ".join(venues))
        rows = merged
        globals()["_LAST_SITE_REPORTS"] = site_reports
    return rows, rejected


def dedupe_rows(rows):
    best = {}
    for r in rows:
        score = sum(1 for k in ("venue", "city", "state", "age_range",
                                "team_count", "end_date") if r.get(k))
        cur = best.get(r["fingerprint"])
        if cur is None or score > cur[0]:
            best[r["fingerprint"]] = (score, r)
    return [r for _, r in best.values()]


# --------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------

class JSONSink:
    """Writes to disk. Use for dry runs and for diffing between weeks."""

    def __init__(self, path):
        self.path = path

    def write(self, rows):
        with open(self.path, "w") as f:
            json.dump(rows, f, indent=2, default=str)
        log.info("wrote %d rows -> %s", len(rows), self.path)
        return len(rows)


class SupabaseSink:
    """Upserts into Supabase on the `fingerprint` unique key.

    Re-running is safe: an event that hasn't changed produces the same
    fingerprint and updates in place rather than duplicating. Set
    SUPABASE_URL and SUPABASE_SERVICE_KEY in the environment.

    Expected table (see schema.sql):
        create table events (
          fingerprint text primary key,
          ... ,
          first_seen timestamptz default now(),
          scraped_at timestamptz
        );
    """

    def __init__(self, url=None, key=None, table="events"):
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("SUPABASE_SERVICE_KEY", "")
        self.table = table

    @property
    def configured(self):
        return bool(self.url and self.key)

    def fetch_existing(self, since_days=0, limit=5000):
        """Pull rows already in the table so cross-source dedupe can merge
        against them (notably Tourney Machine's). Returns [] if unreachable."""
        if not self.configured:
            return []
        import requests
        headers = {"apikey": self.key, "Authorization": f"Bearer {self.key}"}
        params = {"select": "*", "limit": str(limit)}
        if since_days:
            cutoff = (dt.date.today() - dt.timedelta(days=since_days)).isoformat()
            params["start_date"] = f"gte.{cutoff}"
        try:
            r = requests.get(f"{self.url}/rest/v1/{self.table}",
                             headers=headers, params=params, timeout=60)
            if r.status_code >= 300:
                log.warning("fetch_existing %s: %s", r.status_code, r.text[:200])
                return []
            rows = r.json()
            for row in rows:
                row.setdefault("source_type", "tourney_machine")
            log.info("fetched %d existing rows for cross-source dedupe", len(rows))
            return rows
        except Exception as e:  # noqa: BLE001
            log.warning("fetch_existing failed: %s", e)
            return []

    def write(self, rows, chunk=200):
        if not self.configured:
            raise RuntimeError(
                "SUPABASE_URL / SUPABASE_SERVICE_KEY not set - "
                "run with --dry to write JSON instead")
        import requests
        endpoint = f"{self.url}/rest/v1/{self.table}"
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        total = 0
        for i in range(0, len(rows), chunk):
            batch = rows[i:i + chunk]
            for r in batch:
                if isinstance(r.get("flags"), list):
                    r["flags"] = ",".join(r["flags"])
                if isinstance(r.get("sources"), list):
                    import json as _json
                    r["sources"] = _json.dumps(r["sources"])
                r.pop("source_type", None)   # runtime-only field
            resp = requests.post(f"{endpoint}?on_conflict=fingerprint",
                                 headers=headers, json=batch, timeout=60)
            if resp.status_code >= 300:
                raise RuntimeError(f"supabase {resp.status_code}: {resp.text[:400]}")
            total += len(batch)
            log.info("upserted %d/%d", total, len(rows))
        return total
