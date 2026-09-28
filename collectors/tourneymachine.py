"""Tourney Machine (SportsEngine Tourney) — public JSON search + public event page for team counts.
Pulls the ENTIRE upcoming index (no date/market filter); filtering happens in Supabase."""
import json, re, time, collections, urllib.parse, datetime
from concurrent.futures import ThreadPoolExecutor
from . import common
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

def team_counts(ids, workers=6):
    def one(tid):
        b = common.polite_get(EVENT.format(id=tid), headers=H_HTML)
        if not b: return tid, None
        opts = OPT.findall(b)
        teams = {o[0] for o in opts}; divs = collections.Counter(o[2] for o in opts)
        return tid, {"team_count": len(teams), "division_count": len(divs),
                     "divisions": "; ".join(f"{k} ({v})" for k, v in sorted(divs.items()))}
    out = {}
    with ThreadPoolExecutor(workers) as ex:
        for tid, res in ex.map(one, ids):
            out[tid] = res
    return out

def collect():
    seen = search_all()
    tc = team_counts(list(seen))
    rows, errors = [], 0
    for tid, r in seen.items():
        s, e = common.parse_date_range(r["date"])
        t = tc.get(tid) or {}
        if not t: errors += 1
        rows.append({
            "source": "tourneymachine", "source_event_id": tid,
            "name": r["title"], "sport": r["icon"], "organizer_name": r["customer"],
            "start_date": s, "end_date": e, "venue_name": None,
            "city_states": r["cityStates"], "team_count": t.get("team_count"),
            "division_count": t.get("division_count"), "divisions": t.get("divisions"),
            "listing_url": "https://www.tourneymachine.com/" + r["link"], "official_url": None,
            "raw": {"date_text": r["date"], "location": r["location"]},
        })
    return rows, errors
