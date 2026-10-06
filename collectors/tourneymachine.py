"""Tourney Machine (SportsEngine Tourney) — public JSON search + public event page for team counts.
Pulls the ENTIRE upcoming index (no date/market filter); filtering happens in Supabase."""
import json, re, time, collections, urllib.parse, datetime
from concurrent.futures import ThreadPoolExecutor
from . import common, weather
from .common import log

SEARCH = "https://api.tourneymachine.com/v2/Tournaments/Search?per_page=100&page={page}&query={q}"
EVENT = "https://www.tourneymachine.com/Public/Results/Tournament.aspx?IDTournament={id}"
H_JSON = {"accept": "application/json", "origin": "https://www.tourneymachine.com", "referer": "https://www.tourneymachine.com/",
          "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/152.0 Safari/537.36"}
H_HTML = {**H_JSON, "accept": "text/html"}
# Year queries enumerate the whole index (keywords include dates); sport queries are a safety net.
QUERIES = [str(y) for y in range(datetime.date.today().year, datetime.date.today().year + 4)] + \
          ["tournament", "lacrosse", "soccer", "baseball", "softball", "volleyball", "basketball", "hockey", "football", "field hockey"]
OPT = re.compile(r'<option value="(h[0-9a-f]{31})"[^>]*>([^<]*)\(Division: (.*?)\)</option>')
# The event page ships its venues in the loadLocations() map callback:
#   complex0.name = 'Victory Athletic Center'; complex0.lat = '33.48'; complex0.long = '-117.1'
COMPLEX_FIELD = re.compile(r"(complex\d+)\.(\w+)\s*=\s*'([^']*)'")
PLACEHOLDER = re.compile(r"multiple locations|various|^tbd$|see field assignments|multi[- ]site", re.I)
ADDR = re.compile(r'maps\?q=([^"]+)"[^>]*>\s*<address>(.*?)</address>', re.S)

def parse_complexes(body):
    """-> [{complex_id, venue_name, lat, long}] in page order, [] when absent."""
    if not body or "loadLocations" not in body: return []
    seg = body[body.index("loadLocations"):]
    seg = seg[:seg.find("var map =")] if "var map =" in seg else seg[:6000]
    acc = {}
    for m in COMPLEX_FIELD.finditer(seg):
        acc.setdefault(m.group(1), {})[m.group(2)] = m.group(3)
    out = []
    for key in sorted(acc, key=lambda k: int(re.sub(r"\D", "", k) or 0)):
        c = acc[key]; name = (c.get("name") or "").strip()
        if not name or PLACEHOLDER.search(name): continue
        out.append({"complex_id": c.get("ID"), "venue_name": name, "lat": c.get("lat"), "long": c.get("long"), "address": None})
    import html as _h
    addr_list = [re.sub(r"\s+", " ", _h.unescape(re.sub(r"<br\s*/?>", ", ", a))).strip(" ,") for _, a in ADDR.findall(body)]
    if len(addr_list) == len(out):
        for v, a in zip(out, addr_list): v["address"] = a
    return out

def search_all():
    seen = {}
    for q in QUERIES:
        page = 1
        while True:
            txt = common.polite_get(SEARCH.format(page=page, q=urllib.parse.quote(q)), headers=H_JSON)
            if not txt: break
            d = json.loads(txt)
            for r in d["result"]:
                seen[r["link"].split("=")[-1]] = r
            if d["metadata"]["pagination"]["lastPage"] or not d["result"]: break
            page += 1; time.sleep(0.4)
        log.info("tourneymachine query %r -> %d total so far", q, len(seen))
    return seen

def team_counts(ids, workers=3):
    def one(tid):
        b = common.polite_get(EVENT.format(id=tid), headers=H_HTML)
        time.sleep(0.3)
        if not b: return tid, None
        opts = OPT.findall(b)
        teams = {o[0] for o in opts}; divs = collections.Counter(o[2] for o in opts)
        docs = common.parse_documents(b)
        return tid, {"documents": docs, "has_field_map": bool(docs and any(d["kind"] == "field_map" for d in docs)),
                     "clubs": common.clubs_from_options(opts),
                     "team_count": len(teams), "division_count": len(divs),
                     "divisions": "; ".join(f"{k} ({v})" for k, v in sorted(divs.items())),
                     "complexes": parse_complexes(b), "_body": b}   # same page fetch, no extra request
    out = {}
    with ThreadPoolExecutor(workers) as ex:
        for tid, res in ex.map(one, ids):
            out[tid] = res
    return out

DETAIL_WINDOW_DAYS = 120   # inside this window: event page (team counts, venues) every week
ROLL_WEEKS = 4             # beyond it: every event refreshed once every ROLL_WEEKS weeks, a slice per run

SCHEDULE_WINDOW_DAYS = 21   # read division schedules (fields in use, teams per site) this close to the event

def schedule_stats(tid, body):
    """Fields in use and teams/games per site from the division schedule pages."""
    from .backfill_tourneymachine import site_counts
    div_ids = list(dict.fromkeys(re.findall(r"IDDivision=(h[0-9a-f]{31})", body)))
    if not div_ids: return None
    cx = parse_complexes(body)
    per_site, games, sched_teams, fields = site_counts(tid, div_ids[:40], cx)
    fields_used = sum(len(v) for v in fields.values()) or None
    return {"per_site": dict(per_site), "games": dict(games), "schedule_teams": sched_teams or None,
            "fields": {k: sorted(v) for k, v in fields.items()}, "fields_used": fields_used,
            "profile": getattr(site_counts, "last_schedule", None)}

def collect():
    seen = search_all()
    today = datetime.date.today()
    week = today.isocalendar()[1]
    def wanted(tid, r):
        s, _ = common.parse_date_range(r["date"])
        if s is None or s < today - datetime.timedelta(days=3): return False
        if s <= today + datetime.timedelta(days=DETAIL_WINDOW_DAYS): return True
        return int(tid[-6:], 16) % ROLL_WEEKS == week % ROLL_WEEKS      # stable slice of the far-out events
    detail_ids = [tid for tid, r in seen.items() if wanted(tid, r)]
    log.info("tourneymachine: %d events in index, fetching detail for %d (%d-day window + 1/%d of the rest)",
             len(seen), len(detail_ids), DETAIL_WINDOW_DAYS, ROLL_WEEKS)
    tc = team_counts(detail_ids)
    rows, errors = [], 0
    for tid, r in seen.items():
        s, e = common.parse_date_range(r["date"])
        t = tc.get(tid) or {}
        if tid in tc and not t: errors += 1
        cx = t.get("complexes") or []
        clim = None
        v0 = next((v for v in cx if v.get("lat") and v.get("long")), None)
        if v0 and s and today <= s <= today + datetime.timedelta(days=DETAIL_WINDOW_DAYS):
            try: clim = weather.climatology(float(v0["lat"]), float(v0["long"]), s, e)
            except Exception as ex: log.warning("climatology failed %s: %s", tid, ex)
        fc = weather.forecast(float(v0["lat"]), float(v0["long"]), s, e) if v0 and s and s <= today + datetime.timedelta(days=15) else None
        rows.append({
            "weather_climatology": clim, "sun_score": clim["sun_score"] if clim else None,
            "weather_forecast": fc, "uv_index": (fc or {}).get("uv_index_max") or (clim or {}).get("uv_index_est"),
            "source": "tourneymachine", "source_event_id": tid,
            "name": r["title"], "sport": r["icon"], "organizer_name": r["customer"],
            "start_date": s, "end_date": e,
            "venue_name": cx[0]["venue_name"] if len(cx) == 1 else None,   # multi-site: see raw.venues
            "venue_lat": cx[0]["lat"] if len(cx) == 1 else None,
            "venue_long": cx[0]["long"] if len(cx) == 1 else None,
            "venue_count": max(len(cx), len(r["cityStates"])) or None,
            "city_states": r["cityStates"], "team_count": t.get("team_count"),
            "division_count": t.get("division_count"), "divisions": t.get("divisions"),
            "listing_url": "https://www.tourneymachine.com/" + r["link"], "official_url": None,
            "venues": cx or None,
            "fields_used": sched["fields_used"] if sched else None,
            "documents": t.get("documents"), "has_field_map": t.get("has_field_map"),
            "clubs": t.get("clubs"), "club_count": len(t["clubs"]) if t.get("clubs") else None,
            "registration_snapshot": {"date": today.isoformat(), "teams": t.get("team_count")} if t.get("team_count") is not None else None,
            "schedule_profile": sched.get("profile") if sched else None,
            "schedule_quality": (sched.get("profile") or {}).get("schedule_quality") if sched else None,
            "last_day_pm_field_share": (sched.get("profile") or {}).get("last_day_pm_field_share") if sched else None,
            "day1_hours": (sched.get("profile") or {}).get("day1_hours") if sched else None,
            "last_day_hours": (sched.get("profile") or {}).get("last_day_hours") if sched else None,
            "schedule_teams": sched["schedule_teams"] if sched else None,
            "teams_per_location_sched_avg": (round(sum(sched["per_site"].values()) / len(sched["per_site"]), 1) if sched and sched["per_site"] else None),
            "teams_per_field": (round(t["team_count"] / sched["fields_used"], 1) if sched and sched.get("fields_used") and t.get("team_count") else None),
            "raw": {"date_text": r["date"], "location": r["location"], "venues": cx,
                    "all_city_states": r["cityStates"], "schedule_read": bool(sched)},
        })
    return rows, errors


# ---- Weekly: archive events that just finished into event_history --------------------------
FINISHED_LOOKBACK_DAYS = 10

def collect_history():
    """Events that ended in the last FINISHED_LOOKBACK_DAYS: refetch the final team list, schedules,
    per-site counts, documents, clubs and actual weather, in the same shape as the backfill rows.
    Posted as {"history": [...]} so event_history grows every week without rerunning the backfill."""
    from . import backfill_tourneymachine as bf
    seen = search_all()
    today = datetime.date.today()
    rows, errors = [], 0
    for tid, r in seen.items():
        s0, e0 = common.parse_date_range(r["date"])
        if not e0 or not (today - datetime.timedelta(days=FINISHED_LOOKBACK_DAYS) <= e0 < today): continue
        if s0 and (e0 - s0).days > 6: continue                      # leagues are not tournaments
        page = bf._get(bf.EVENT.format(id=tid) if hasattr(bf, "EVENT") else EVENT.format(id=tid)); time.sleep(0.5)
        if not page: errors += 1; continue
        try:
            ev = bf.parse_event(page.text)
            ev["sport"] = ev["sport"] or r.get("icon")
            ev["name"] = ev["name"] or r.get("title")
            per_site, games, sched_teams, fields = ({}, {}, 0, {})
            if ev["div_ids"]:
                per_site, games, sched_teams, fields = bf.site_counts(ev["tid"], ev["div_ids"][:40], ev["venues"])
            profile = getattr(bf.site_counts, "last_schedule", None) if ev["div_ids"] else None
            row = bf.build_row(0, ev, per_site, games, sched_teams, fields, profile)
            row["short_id"] = None
            row["organizer_name"] = r.get("customer"); row["organizer_source"] = "listed"
            if not row["city_states"]: row["city_states"] = r.get("cityStates") or []
            _, lo, hi, gender = common.parse_age_groups(f"{ev['divisions']} {ev['name']}", s0.year if s0 else None)
            row["age_groups"] = f"{lo}" if lo and lo == hi else (f"{lo}\u2013{hi}" if lo else None)
            row["grad_year_min"], row["grad_year_max"], row["gender"] = lo, hi, gender
            st = row["city_states"][0].split(", ")[-1] if row["city_states"] else None
            row["state"] = st; row["name_core"] = common.name_core(row["name"])
            row["event_fingerprint"] = common.fingerprint(row["name"], row["sport"], st)
            row["calendar_week"] = s0.isocalendar()[1] if s0 else None
            v0 = next((v for v in ev["venues"] if v.get("lat") and v.get("long")), None)
            if v0:
                try: row["weather_actual"] = weather.actual(float(v0["lat"]), float(v0["long"]), s0, e0)
                except Exception: pass
            rows.append(row)
        except Exception as ex:
            errors += 1; log.warning("archive failed %s: %s", tid, ex)
    log.info("tourneymachine archive: %d finished events -> history, %d errors", len(rows), errors)
    return rows, errors
