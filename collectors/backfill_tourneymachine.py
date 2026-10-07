"""One-time historical backfill of Tourney Machine (2020 -> now) via sequential short links
tourneymachine.com/R{n}. Each page carries the final team list, divisions, venues (with addresses),
and the per-division schedules, from which teams-per-location is computed.

Run as a sharded one-off workflow, not weekly:  python -m collectors.backfill_tourneymachine START END
Rows are posted to the ingest endpoint as {"history": [...]} -> event_history table."""
import re, sys, os, json, html, time, datetime, collections, statistics
from concurrent.futures import ThreadPoolExecutor
from . import common
from .common import log

UA = {"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/152.0 Safari/537.36",
      "referer": "https://www.tourneymachine.com/", "accept": "text/html"}
BASE = "https://www.tourneymachine.com"
TRACKED = {"lacrosse", "soccer", "baseball", "softball", "volleyball", "hockey", "field hockey", "football", "basketball"}
OPT = re.compile(r'<option value="(h[0-9a-f]{31})"[^>]*>([^<]*)\(Division: (.*?)\)</option>')
COMPLEX = re.compile(r"(complex\d+)\.(\w+)\s*=\s*'([^']*)'")
ADDR = re.compile(r'maps\?q=([^"]+)"[^>]*>\s*<address>(.*?)</address>', re.S)
GAME = re.compile(r"<tr class='schedule_row[^']*'[^>]*data-gameid[^>]*>(.*?)</tr>", re.S)
GAME_FULL = re.compile(r"<tr class='schedule_row (date_\d{8})[^']*'[^>]*data-gameid[^>]*>(.*?)</tr>", re.S)
TIME = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)")
FAC = re.compile(r"data-facilityid='[^']*'>\s*([^<]+?)\s*</td>")
TEAMID = re.compile(r"data-teamid='(h[0-9a-f]{31})'")
DIV_WORKERS = 4      # division schedule pages fetched in parallel per event
MAX_DIVISIONS = 16   # divisions per event; enough to see which fields and venues are in use
LINK_SLEEP = 0.25    # pause between short-link fetches

class IngestRejected(RuntimeError):
    """The ingest endpoint refused a batch. The shard stops instead of crawling for hours into nothing."""

def _get(url):
    import requests
    for i in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=40, allow_redirects=True)
            if r.status_code == 200: return r
            if r.status_code in (429, 403, 503): time.sleep(8 * (i + 1)); continue
            return None
        except Exception: time.sleep(2 * (i + 1))
    return None

SPORT_WORDS = [("lacrosse", r"lacrosse|\blax\b"), ("soccer", r"soccer|futsal|\bfc\b"), ("baseball", r"baseball|\b\d{1,2}u\b.*(?:bats?|diamond)"),
               ("softball", r"softball|fastpitch"), ("volleyball", r"volleyball"), ("basketball", r"basketball|hoops|\baau\b"),
               ("hockey", r"\bhockey\b"), ("field hockey", r"field hockey"), ("football", r"football|7v7|flag")]
def infer_sport(text):
    t = (text or "").lower()
    for sp, rx in SPORT_WORDS:
        if re.search(rx, t): return sp
    return None

def parse_event(h):
    tid = re.search(r'setTargeting\("Tournament_ID",\s*"(h[0-9a-f]{31})"', h)
    sport = re.search(r'setTargeting\("Sport",\s*"([^"]+)"', h)
    name = re.search(r'tournament-search-results-tournament-header"[^>]*>(.*?)</a>', h, re.S)
    t = re.sub(r"<[^>]+>", " ", h)
    d = re.search(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}(?:\s*-\s*(?:\w+ )?\d{1,2})?,? 20\d\d", t)
    s, e = common.parse_date_range(d.group(0).replace(" ,", ",")) if d else (None, None)
    opts = OPT.findall(h); teams = {o[0] for o in opts}; divs = collections.Counter(o[2] for o in opts)
    acc = {}
    seg = h[h.index("loadLocations"):] if "loadLocations" in h else ""
    for m in COMPLEX.finditer(seg[:8000]): acc.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    # address blocks appear in venue order on the page; pair them with complexes by order
    addr_list = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<br\s*/?>", ", ", a))).strip(" ,") for _, a in ADDR.findall(h)]
    venues = []
    for k in sorted(acc, key=lambda k: int(re.sub(r"\D", "", k) or 0)):
        c = acc[k]; nm = html.unescape((c.get("name") or "").strip())
        if not nm or re.search(r"multiple locations|various|^tbd$", nm, re.I): continue
        venues.append({"venue_name": nm, "lat": c.get("lat"), "long": c.get("long"), "address": None})
    # match addresses to venues: by order when counts agree, else by shared tokens
    if len(addr_list) == len(venues):
        for v, a in zip(venues, addr_list): v["address"] = a
    else:
        for v in venues:
            hit = next((a for a in addr_list if any(w.lower() in a.lower() for w in v["venue_name"].split() if len(w) > 3)), None)
            v["address"] = hit
    div_ids = list(dict.fromkeys(re.findall(r"IDDivision=(h[0-9a-f]{31})", h)))
    docs = common.parse_documents(h); clubs = common.clubs_from_options(opts)
    return {"documents": docs, "clubs": clubs, "club_count": len(clubs) if clubs else None,
            "has_field_map": bool(docs and any(d["kind"] == "field_map" for d in docs)),
            "tid": tid.group(1) if tid else None, "sport": (sport.group(1).lower() if sport else None),
            "name": html.unescape(name.group(1)).strip() if name else None, "start": s, "end": e,
            "team_count": len(teams), "division_count": len(divs),
            "divisions": "; ".join(f"{k} ({v})" for k, v in sorted(divs.items())), "venues": venues, "div_ids": div_ids}

def site_counts(tid, div_ids, venues):
    names = [v["venue_name"] for v in venues]
    team_sites = collections.defaultdict(set); games = collections.Counter(); div_sites = collections.defaultdict(set)
    fields = collections.defaultdict(set)      # site -> distinct field names
    game_log = []                              # (date, minutes-of-day, site, field)
    def fetch(dv):
        r = _get(f"{BASE}/Public/Results/Division.aspx?IDTournament={tid}&IDDivision={dv}"); time.sleep(0.25)
        return r
    with ThreadPoolExecutor(DIV_WORKERS) as ex:
        pages = list(ex.map(fetch, div_ids))
    for dv, r in zip(div_ids, pages):
        if not r: continue
        for dcls, row in GAME_FULL.findall(r.text):
            f = FAC.search(row)
            if not f: continue
            fac = html.unescape(f.group(1)).strip()
            site = next((n for n in names if fac.lower().startswith(n.lower())), fac.split(" - ")[0])
            games[site] += 1; fields[site].add(fac)
            tm_ = TIME.search(row)
            if tm_:
                try:
                    t = datetime.datetime.strptime(tm_.group(1).replace(" ", ""), "%I:%M%p")
                    game_log.append((dcls[5:], t.hour * 60 + t.minute, site, fac))
                except ValueError: pass
            for tm in TEAMID.findall(row): team_sites[tm].add(site); div_sites[dv].add(site)
    per_site = collections.Counter(s for sites in team_sites.values() for s in sites)
    site_counts.last_schedule = schedule_profile(game_log)
    return per_site, games, len(team_sites), {k: sorted(v) for k, v in fields.items()}

def _hm(m): return f"{m // 60:02d}:{m % 60:02d}"

def schedule_profile(game_log):
    """Per day (and per site per day): first start, last start, estimated end, games, distinct fields,
    fields still active in the afternoon (starts >= 12:00). Game length = median gap between consecutive
    starts on the same field (bounded 40-90 min) so 'end' is the last start plus one game."""
    if not game_log: return None
    by_field = collections.defaultdict(list)
    for d, m, site, fac in game_log: by_field[(d, fac)].append(m)
    gaps = [b - a for arr in by_field.values() for a, b in zip(sorted(arr), sorted(arr)[1:]) if 20 <= b - a <= 180]
    glen = int(statistics.median(gaps)) if gaps else 60
    glen = max(40, min(90, glen))
    days = {}
    for d, m, site, fac in game_log:
        day = days.setdefault(d, {"games": 0, "fields": set(), "pm_fields": set(), "late_fields": set(), "first": m, "last": m, "sites": {}})
        day["games"] += 1; day["fields"].add(fac); day["first"] = min(day["first"], m); day["last"] = max(day["last"], m)
        if m >= 12 * 60: day["pm_fields"].add(fac)
        if m >= 14 * 60 + 30: day["late_fields"].add(fac)          # still playing mid/late afternoon
        sd = day["sites"].setdefault(site, {"games": 0, "fields": set(), "pm_fields": set(), "late_fields": set(), "first": m, "last": m})
        sd["games"] += 1; sd["fields"].add(fac); sd["first"] = min(sd["first"], m); sd["last"] = max(sd["last"], m)
        if m >= 12 * 60: sd["pm_fields"].add(fac)
        if m >= 14 * 60 + 30: sd["late_fields"].add(fac)
    out_days = []
    for d in sorted(days):
        day = days[d]
        def pack(x):
            return {"games": x["games"], "fields_active": len(x["fields"]), "fields_active_pm": len(x["pm_fields"]), "fields_active_late": len(x["late_fields"]),
                    "first_game": _hm(x["first"]), "last_game_start": _hm(x["last"]), "est_end": _hm(x["last"] + glen),
                    "hours": round((x["last"] + glen - x["first"]) / 60, 1)}
        row = {"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", **pack(day), "sites": {k: pack(v) for k, v in day["sites"].items()}}
        out_days.append(row)
    d1 = out_days[0]; last = out_days[-1]
    full_day_all = all(x["hours"] >= 8 for x in out_days)
    pm_share_last = round(min(1.0, last["fields_active_pm"] / d1["fields_active"]), 2) if d1["fields_active"] else None
    late_share_last = round(min(1.0, last["fields_active_late"] / d1["fields_active"]), 2) if d1["fields_active"] else None
    # 0-100. Day length: every day 9h+ earns full credit. Last day: when it ends and whether a
    # meaningful share of fields is still playing late. A normal championship taper (half the fields
    # after 2:30, running to 5pm) is a great event; a Sunday that ends at 11am, or one field until 6, is not.
    # day-length credit is weighted by how many of Saturday's fields were actually in use that day
    peak = max(x["fields_active"] for x in out_days) or 1
    q_hours = 50 * min(1.0, sum(min(x["hours"], 9) * min(1.0, x["fields_active"] / peak) for x in out_days) / (9 * len(out_days)))
    if len(out_days) > 1:
        end_min = last["last"] + glen if False else None
        end_h = int(last["est_end"][:2]) + int(last["est_end"][3:]) / 60
        end_credit = max(0.0, min(1.0, (end_h - 12.0) / 4.5))           # 12:00 -> 0, 16:30+ -> 1
        share_credit = max(0.0, min(1.0, (late_share_last or 0) / 0.5))  # 50%+ of Saturday's fields after 2:30 -> 1
        q_last = 50 * (0.3 * end_credit + 0.7 * share_credit) if end_h >= 13 else 50 * 0.25 * end_credit
        if (late_share_last or 0) < 0.15: q_last = min(q_last, 12)   # one or two fields running late is a championship, not a crowd
    else:
        q_last = 50 if d1["hours"] >= 8 else 25
    q = min(100, q_hours + q_last)
    return {"game_length_min": glen, "days": out_days, "day_count": len(out_days),
            "day1_hours": d1["hours"], "last_day_hours": last["hours"], "last_day_end": last["est_end"],
            "last_day_pm_field_share": pm_share_last, "last_day_late_field_share": late_share_last, "full_day_all_days": full_day_all,
            "field_hours": round(sum(x["fields_active"] * x["hours"] for x in out_days), 1),
            "schedule_quality": round(q)}

def build_row(n, ev, per_site, games, sched_teams, fields=None, profile=None):
    fields = fields or {}
    vc = len(ev["venues"]) or (1 if ev["team_count"] else None)
    for v in ev["venues"]:
        v["teams"] = per_site.get(v["venue_name"]); v["games"] = games.get(v["venue_name"])
        v["fields"] = fields.get(v["venue_name"]); v["field_count"] = len(fields.get(v["venue_name"]) or []) or None
        if profile:
            v["schedule"] = [{"date": d["date"], **d["sites"][v["venue_name"]]} for d in profile["days"] if v["venue_name"] in d["sites"]] or None
    fields_used = sum(len(x) for x in fields.values()) or None
    city_states = []
    for v in ev["venues"]:
        m = re.search(r",\s*([^,]+),\s*([A-Z]{2})\b", v.get("address") or "")
        if m:
            cs = f"{m.group(1).strip()}, {m.group(2)}"
            if cs not in city_states: city_states.append(cs)
    sched_avg = (round(sum(x for x in per_site.values()) / len(per_site), 1) if per_site else None)
    return {"source": "tourneymachine", "source_event_id": ev["tid"], "short_id": n, "name": ev["name"], "sport": ev["sport"],
            "start_date": ev["start"], "end_date": ev["end"], "city_states": city_states,
            "team_count": ev["team_count"], "division_count": ev["division_count"], "divisions": ev["divisions"],
            "venue_count": vc, "venues": ev["venues"],
            "teams_per_location_avg": (round(ev["team_count"] / vc, 1) if vc and ev["team_count"] else None),
            "teams_per_location_sched_avg": sched_avg, "schedule_teams": sched_teams or None,
            "fields_used": fields_used,
            "documents": ev.get("documents"), "has_field_map": ev.get("has_field_map"),
            "clubs": ev.get("clubs"), "club_count": ev.get("club_count"),
            "schedule_profile": profile,
            "schedule_quality": (profile or {}).get("schedule_quality"),
            "last_day_pm_field_share": (profile or {}).get("last_day_pm_field_share"),
            "day1_hours": (profile or {}).get("day1_hours"), "last_day_hours": (profile or {}).get("last_day_hours"),
            "teams_per_field": (round(ev["team_count"] / fields_used, 1) if fields_used and ev["team_count"] else None),
            "listing_url": f"{BASE}/Public/Results/Tournament.aspx?IDTournament={ev['tid']}"}

BLOCK_LIMIT = 25   # this many consecutive failed page fetches = we are being throttled; stop and keep what we have

def collect(start, end, tracked_only=True, with_sites=True, sink=None):
    rows, errors, blocked_streak, pending = [], 0, 0, []
    paused_once = False; collect.last_id = start; collect.upcoming = 0
    def flush():
        nonlocal pending
        if sink and pending:
            try: sink(pending)
            except Exception as ex:
                raise IngestRejected(f"post of {len(pending)} rows failed at R{collect.last_id}: {ex}") from ex
            pending = []
    for n in range(start, end):
        r = _get(f"{BASE}/R{n}"); time.sleep(LINK_SLEEP)
        collect.last_id = n
        if r is None:
            blocked_streak += 1
            if blocked_streak >= BLOCK_LIMIT:
                if not paused_once:
                    log.warning("R%d: %d consecutive failures; pausing 10 minutes before retrying", n, blocked_streak)
                    flush(); time.sleep(600); paused_once = True; blocked_streak = 0; continue
                log.error("R%d: throttled again after pause; stopping this shard. Resume from R%d", n, n); break
            continue
        blocked_streak = 0
        if "IDTournament=" not in r.url: continue
        try:
            ev = parse_event(r.text)
        except Exception as ex:
            errors += 1; log.warning("R%d parse failed: %s", n, ex); continue
        if not ev["tid"] or not ev["start"]: continue
        if not ev["sport"]:
            ev["sport"] = infer_sport(f"{ev['name']} {ev['divisions']}") or "unknown"
        if tracked_only and ev["sport"] not in TRACKED and ev["sport"] != "unknown": continue
        if (ev["end"] or ev["start"]) >= datetime.date.today():
            collect.upcoming += 1; continue     # not played yet: no final team count. The weekly job archives it once it ends
        per_site, games, sched_teams, fields = ({}, {}, 0, {})
        if with_sites and ev["div_ids"]:
            per_site, games, sched_teams, fields = site_counts(ev["tid"], ev["div_ids"][:MAX_DIVISIONS], ev["venues"])
        profile = getattr(site_counts, "last_schedule", None) if ev["div_ids"] else None
        row = build_row(n, ev, per_site, games, sched_teams, fields, profile)
        _, lo, hi, gender = common.parse_age_groups(f"{ev['divisions']} {ev['name']}", ev["start"].year if ev["start"] else None)
        row["age_groups"] = f"{lo}" if lo and lo == hi else (f"{lo}\u2013{hi}" if lo else None)
        row["grad_year_min"], row["grad_year_max"], row["gender"] = lo, hi, gender
        st = (row["city_states"][0].split(", ")[-1] if row["city_states"] else None)
        row["state"] = st; row["name_core"] = common.name_core(row["name"])
        row["event_fingerprint"] = common.fingerprint(row["name"], row["sport"], st)
        row["calendar_week"] = row["start_date"].isocalendar()[1] if row["start_date"] else None
        # weather_actual is filled nightly by the app; fetching it here doubled the time per event
        rows.append(row); pending.append(row)
        if len(pending) >= 150: flush(); log.info("backfill R%d: %d rows so far", n, len(rows))
    flush()
    return rows, errors

def main():
    start, end = int(sys.argv[1]), int(sys.argv[2])
    db = common.Supabase(); started = datetime.datetime.utcnow().isoformat() + "Z"
    backup = os.environ.get("BACKUP_DIR")
    backup_file = os.path.join(backup, f"R{start}-{end}.jsonl") if backup else None
    if backup: os.makedirs(backup, exist_ok=True)
    def sink(batch):
        rows = [{k: (v.isoformat() if isinstance(v, datetime.date) else v) for k, v in r.items()} for r in batch]
        if backup_file:                      # write to disk first, so a rejected post loses nothing
            with open(backup_file, "a") as f:
                for r in rows: f.write(json.dumps(r, default=str) + "\n")
                f.flush(); os.fsync(f.fileno())
        db._post({"source": "tourneymachine-history", "history": rows})
    t0 = time.time()
    try:
        rows, errors = collect(start, end, sink=sink)
    except IngestRejected as ex:
        log.error("STOPPED: %s. Rows up to here are in %s; resend them with: python -m collectors.replay_history <file>. "
                  "Then rerun with start=%d end=%d", ex, backup_file or "(no backup file: BACKUP_DIR not set)", collect.last_id, end)
        try: db.log_run(f"tourneymachine-history R{start}-{end}", 0, 0, 1, started, note=f"shard backfill stopped: {ex}")
        except Exception: pass
        sys.exit(1)
    last = getattr(collect, "last_id", end)
    note = "shard backfill" + ("" if last >= end - 1 else f"; stopped early at R{last}; rerun with start={last} end={end}")
    db.log_run(f"tourneymachine-history R{start}-{end}", len(rows), len(rows), errors, started, note=note)
    secs = time.time() - t0
    log.info("backfill R%d-R%d: %d rows, %d skipped as not finished yet, %d errors, %.0f s (%.2f s/id)",
             start, end, len(rows), collect.upcoming, errors, secs, secs / max(1, last - start + 1))

if __name__ == "__main__":
    main()
