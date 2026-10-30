"""Stage A of Crawl Organizer: who to crawl, with their website, deduped by canonical organizer name.

Sources, merged by canon name (collectors.common.canon_org):
  1. KNOWN: the 14 lacrosse organizers that already have a dedicated scraper (adapters/). Websites are known.
  2. The app's data (a local export, tools/backup_db.py): every lacrosse organizer named on Tourney Machine, with how
     many events they run and how big those get (final team counts, expected_teams, org precedent).
  3. lacrosse_websites.json in this folder: public organizer websites added by hand or from Find Organizer.
     Names and websites only; never contacts (public repo).

qualifies_vendor: dig for a vendor contact only for organizers whose events reach VENDOR_MIN_TEAMS by precedent
(past finals, expected_teams, org_prec_max). Current registrations never disqualify: a 5-team listing today can
finish with 200."""
from __future__ import annotations
import os, re, json, pathlib, datetime as dt

from ..common import canon_org as _canon_org

VENDOR_MIN_TEAMS = 20
SEED = pathlib.Path(__file__).with_name("lacrosse_websites.json")

# slug -> (display name, homepage to start from, scraper slug in adapters/)
KNOWN = {
    "mlt": ("MLT", "https://mylacrossetournaments.com/", "mlt"),
    "apex": ("APEX Lacrosse Events", "https://apexlacrosseevents.com/", "apex"),
    "buku": ("Buku Lacrosse", "https://www.buku.events/", "buku"),
    "pll-play": ("PLL Play", "https://premierlacrosseleague.com/play/tournaments", "pll-play"),
    "nxt": ("NXT Sports", "https://www.nxtsports.com/", "nxt"),           # events.nxtsports.com is only the registration host
    "victory": ("Victory Event Series", "https://victoryeventseries.com/", "victory"),
    "hogans": ("Hogan's Lacrosse", "https://www.hoganlax.com/", "hogans"),
    "ml8": ("ML8 Events", "https://www.ml8events.com/", "ml8"),
    "trilogy": ("Trilogy Lacrosse", "https://trilogylacrosse.com/", "trilogy"),
    "primetime": ("PrimeTime Lacrosse", "https://primetimelacrosse.com/", "primetime"),
    "adrenaline": ("Adrenaline Lacrosse", "https://adrln.com/", "adrenaline"),
    "nlf": ("National Lacrosse Federation", "https://nationallacrossefederation.com/", "nlf"),
    "aloha": ("Aloha Tournaments", "https://www.alohatournaments.com/", "aloha"),
    "alliance": ("Alliance Lacrosse League", "https://www.thealliancelacrosseleague.com/", "alliance"),
}

# Where the events actually are when the homepage doesn't link there in words the crawler recognises, or the list is
# built by JavaScript (the crawler then renders it with a headless browser). Crawled before the pages found from the homepage.
START_PAGES = {
    "nxt": ["https://www.nxtsports.com/page/all-events"],
    "hogans": ["https://www.hoganlax.com/page/home", "https://www.hoganlax.com/page/tournaments"],   # homepage "Upcoming Events" cards
    "pll-play": ["https://premierlacrosseleague.com/play/tournaments"],
    "trilogy": ["https://trilogylacrosse.com/trilogy-events-for-teams/"],   # footer "Team Events": every tournament on one page
    "mlt": ["https://mylacrossetournaments.com/events/"],
    "apex": ["https://apexlacrosseevents.com/boys-tournaments/", "https://apexlacrosseevents.com/boys-showcases/"],
    "buku": ["https://www.buku.events/tournaments"],
    "ml8": ["https://www.ml8events.com/events"],
    "primetime": ["https://primetimelacrosse.com/events/"],
    "adrenaline": ["https://adrln.com/events/"],
    "nlf": ["https://nationallacrossefederation.com/events/fall-events/", "https://nationallacrossefederation.com/events/summer-events/",
            "https://nationallacrossefederation.com/events/winter-events/"],     # listings are last season's; event pages are current
    "victory": ["https://victoryeventseries.com/boys/", "https://victoryeventseries.com/girls/",
                "https://victoryeventseries.com/showcases/"],              # homepage shows only the next few
}


def canon_org(name):
    """common.canon_org, with apostrophes dropped first so "Hogan's" and "Hogans" are one organizer."""
    return _canon_org(re.sub(r"['’]", "", name or ""))


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60]


def load_seed(path=SEED):
    """{organizer name: {"website": homepage, "pages": [event pages]}}. An entry may be just the homepage. Missing file = empty."""
    try:
        data = json.loads(pathlib.Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {}
    out = {}
    for k, v in data.items():
        v = {"website": v} if isinstance(v, str) else v
        if not k.startswith("_") and isinstance(v, dict) and str(v.get("website", "")).startswith("http"):
            out[k] = {"website": v["website"], "pages": [u for u in v.get("pages") or [] if u.startswith("http")]}
    return out


def from_app_data(export_dir):
    """Lacrosse organizers in the app's data -> {canon: stats}. export_dir holds event_candidates.jsonl / event_history.jsonl."""
    stats = {}
    def add(name, teams, state, start):
        c = canon_org(name)
        if not c: return
        s = stats.setdefault(c, {"names": {}, "events": 0, "max_teams": 0, "states": set(), "next_event": None})
        s["names"][name] = s["names"].get(name, 0) + 1
        s["events"] += 1
        s["max_teams"] = max(s["max_teams"], teams or 0)
        if state: s["states"].add(state)
        if start and start >= dt.date.today().isoformat() and (not s["next_event"] or start < s["next_event"]): s["next_event"] = start
    for fname in ("event_candidates.jsonl", "event_history.jsonl"):
        p = pathlib.Path(export_dir) / fname
        if not p.exists(): continue
        for line in p.open():
            r = json.loads(line)
            if not (r.get("sport") or "").lower().startswith("lacrosse") or not r.get("organizer_name"): continue
            if r.get("source") == "recurrence": continue
            teams = max(int(r.get("team_count") or 0), int(r.get("expected_teams") or 0), int(r.get("org_prec_max") or 0), int(r.get("final_team_count") or 0))
            st = (r.get("state") or "").split(",")[0].strip().upper()
            st = st if re.fullmatch(r"[A-Z]{2}", st) else None
            add(r["organizer_name"].strip(), teams, st, r.get("start_date"))
    return stats


def build(export_dir=None, seed=None):
    """-> list of organizer dicts: slug, name, canon, website, scraper, events, max_teams, states, next_event, qualifies_vendor."""
    seed = load_seed() if seed is None else seed
    orgs = {}
    for slug, (name, site, scraper) in KNOWN.items():
        orgs[canon_org(name)] = {"slug": slug, "name": name, "canon": canon_org(name), "website": site, "scraper": scraper,
                                 "start_pages": START_PAGES.get(slug, []), "events": 0, "max_teams": 0, "states": [], "next_event": None}
    for c, s in (from_app_data(export_dir) if export_dir and os.path.isdir(export_dir) else {}).items():
        name = max(s["names"], key=s["names"].get)
        o = orgs.get(c)                       # exact canon match only; aliases live in common.ORG_ALIASES
        if o is None:
            o = orgs[c] = {"slug": slugify(name), "name": name, "canon": c, "website": None, "scraper": None,
                           "events": 0, "max_teams": 0, "states": [], "next_event": None}
        o["events"] += s["events"]; o["max_teams"] = max(o["max_teams"], s["max_teams"])
        o["states"] = sorted(set(o["states"]) | s["states"])
        if s["next_event"] and (not o["next_event"] or s["next_event"] < o["next_event"]): o["next_event"] = s["next_event"]
    for name, entry in seed.items():
        site = entry["website"] if isinstance(entry, dict) else entry
        c = canon_org(name)
        o = orgs.setdefault(c, {"slug": slugify(name), "name": name, "canon": c, "website": None, "scraper": None,
                                "events": 0, "max_teams": 0, "states": [], "next_event": None})
        o["website"] = o["website"] or site
        o["seeded"] = True                    # crawl.py --seed-only crawls just these
        if isinstance(entry, dict) and not o["scraper"]:
            o["start_pages"] = list(dict.fromkeys((o.get("start_pages") or []) + entry["pages"]))
    out = list(orgs.values())
    for o in out:
        o["qualifies_vendor"] = o["max_teams"] >= VENDOR_MIN_TEAMS
    out.sort(key=lambda o: (o["website"] is None, -o["max_teams"], o["name"]))
    return out
