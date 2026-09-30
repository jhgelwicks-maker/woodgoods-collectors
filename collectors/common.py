"""Shared schema, date parsing, and Supabase upsert for every collector.

Every collector returns a list of dicts with these keys (missing = None):
  source            short slug, e.g. "tourneymachine"
  source_event_id   stable id on that source; dedupe_key = f"{source}:{source_event_id}"
  name, sport, organizer_name
  start_date, end_date   (date objects)
  venue_name, city, state, city_states (list of "City, ST")
  venue_count, split_venue (bool)
  team_count, division_count, divisions (text)
  vendor_contact_email, vendor_contact_name, age_groups (text)
  listing_url, official_url
  raw (dict, kept as jsonb for debugging)
"""
import os, re, json, time, datetime, logging, requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("collectors")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

UA = {"user-agent": "WoodgoodsEventBot/1.0 (+contact: ops@woodgoods)"}
MON = {m: i for i, m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}

def parse_date_range(text, default_year=None):
    """Handles 'Oct 17, 2026' | 'Oct 3 - 4, 2026' | 'Sep 13 - Oct 18, 2026' |
    'Jun 17, 2025 - Jun 18, 2027' | 'October 10-12, 2026' | 'September 4-6, 2026'."""
    if not text: return None, None
    t = text.replace("\u2013", "-").replace("\u2014", "-").strip()
    t = re.sub(r"([A-Za-z]{3})[a-z]*", lambda m: m.group(1), t)  # October -> Oct
    t = re.sub(r"\s*-\s*", " - ", t)
    D = datetime.date
    m = re.match(r"(\w{3}) (\d+), (\d{4}) - (\w{3}) (\d+), (\d{4})$", t)
    if m: return D(int(m[3]), MON[m[1]], int(m[2])), D(int(m[6]), MON[m[4]], int(m[5]))
    m = re.match(r"(\w{3}) (\d+) - (\w{3}) (\d+), (\d{4})$", t)
    if m: return D(int(m[5]), MON[m[1]], int(m[2])), D(int(m[5]), MON[m[3]], int(m[4]))
    m = re.match(r"(\w{3}) (\d+) - (\d+), (\d{4})$", t)
    if m: return D(int(m[4]), MON[m[1]], int(m[2])), D(int(m[4]), MON[m[1]], int(m[3]))
    m = re.match(r"(\w{3}) (\d+), (\d{4})$", t)
    if m: x = D(int(m[3]), MON[m[1]], int(m[2])); return x, x
    if default_year:
        m = re.match(r"(\w{3}) (\d+) - (\d+)$", t)
        if m: return D(default_year, MON[m[1]], int(m[2])), D(default_year, MON[m[1]], int(m[3]))
        m = re.match(r"(\w{3}) (\d+)$", t)
        if m: x = D(default_year, MON[m[1]], int(m[2])); return x, x
    log.warning("unparsed date: %r", text)
    return None, None

def states_from(city_states):
    return sorted({c.split(", ")[-1] for c in (city_states or []) if ", " in c})

def normalize(row):
    r = dict(row)
    r["dedupe_key"] = f"{r['source']}:{r['source_event_id']}"
    cs = r.get("city_states") or []
    r["venue_count"] = r.get("venue_count") or (len(cs) or None)
    r["split_venue"] = bool(r.get("split_venue") or (len(cs) > 1))
    if not r.get("state") and cs: r["state"] = ", ".join(states_from(cs))
    if not r.get("city") and cs: r["city"] = cs[0].split(", ")[0]
    s, e = r.get("start_date"), r.get("end_date")
    r["days"] = (e - s).days + 1 if s and e else None
    r["is_league"] = bool(r["days"] and r["days"] > 3)
    for k in ("start_date", "end_date"):
        if isinstance(r.get(k), datetime.date): r[k] = r[k].isoformat()
    r["city_states"] = cs
    r["last_seen_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return r

class Supabase:
    """Writes go through the app's locked ingest endpoint (Lovable Cloud does not expose a service key)."""
    def __init__(self):
        self.url = os.environ["INGEST_URL"].rstrip("/")
        self.h = {"x-ingest-key": os.environ["INGEST_KEY"], "Content-Type": "application/json"}

    def _post(self, body):
        res = requests.post(self.url, headers=self.h, data=json.dumps(body, default=str), timeout=120)
        if res.status_code >= 300:
            raise RuntimeError(f"ingest failed {res.status_code}: {res.text[:500]}")
        return res.json() if res.text else {}

    def upsert_candidates(self, rows, chunk=400):
        merged = {}
        for r in rows:
            nr = {k: v for k, v in normalize(r).items() if v is not None}
            key = nr["dedupe_key"]
            if key in merged:
                merged[key].update(nr)          # later row fills gaps, never blanks a value
            else:
                merged[key] = nr
        rows = list(merged.values())
        n = 0
        for i in range(0, len(rows), chunk):
            batch = rows[i:i+chunk]
            self._post({"source": batch[0]["source"] if batch else None, "rows": batch})
            n += len(batch)
        return n

    def log_run(self, source, rows_seen, rows_upserted, errors, started, note=""):
        self._post({"source": source, "run": {"started_at": started, "finished_at": datetime.datetime.utcnow().isoformat()+"Z",
                    "rows_seen": rows_seen, "rows_upserted": rows_upserted, "errors": errors, "note": note[:2000]}})

INSECURE_HOSTS = {"mayouthsoccer.org"}   # sites with a broken certificate chain

def polite_get(url, headers=None, timeout=40, tries=4, sleep=1.0):
    h = {**UA, **(headers or {})}
    verify = not any(host in url for host in INSECURE_HOSTS)
    for i in range(tries):
        try:
            r = requests.get(url, headers=h, timeout=timeout, verify=verify)
            if r.status_code == 200: return r.text
            log.warning("GET %s -> %s", url, r.status_code)
            if r.status_code in (429, 403, 503):
                time.sleep(15 * (i + 1)); continue      # back off hard when rate-limited
        except requests.RequestException as e:
            log.warning("GET %s failed: %s", url, e)
        time.sleep(sleep * (i + 1))
    return None
