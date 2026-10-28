"""Lacrosse match report: what the organizer websites say about each upcoming lacrosse event in the app.

    python tools/lacrosse_match_report.py                        # scrape the 14 organizer sites now
    python tools/lacrosse_match_report.py --scraped rows.jsonl    # reuse rows saved by an earlier run
    python tools/lacrosse_match_report.py --export-dir DIR        # app data (default: the newest of ~/WoodGoodsAI/backup/db/current and fresh)

Every event an organizer site lists (adapters/, via woodgoods_lacrosse) is matched against the app's upcoming lacrosse
rows from a local export: recurrence placeholders (dates guessed from last year's Tourney Machine event), live Tourney
Machine listings and App data(manual-lacrosse-sheet), a hand-curated import. Matching is collectors.event_match (evidence scoring, one-to-one), plus one
extra piece of evidence the placeholders allow: the same town. Placeholders and Tourney Machine history carry no
organizer, so name + venue/town + date are what confirm them.

Writes ~/WoodGoodsAI/reports/lacrosse_match_<date>.csv (never the repo: it is a working file) with one row per organizer
event, plus one row per sheet event of a scraped organizer that its website no longer lists:

  CONFIRMED            the app's event (placeholder / Tourney Machine / sheet) is on the organizer site: same weekend
                       AND same place (town, venue)
  DATE CONFIRMED       same weekend, but one side gives no town, so the place could not be checked
  PLACE DIFFERS        same weekend, but the organizer site lists another town or state
  DATE MOVED           a placeholder matches but the organizer site has another date: the date inferred from last
                       year's Tourney Machine event is wrong and must not be shown
  DATE CONFLICT        a live Tourney Machine listing or the sheet disagrees with the organizer site
  REVIEW               a possible match with weak evidence; a person should look
  NEW SITE             another venue of an event the app has, at a site the app doesn't list
  NEW                  on the organizer site, not in the app (the weekly run adds it as a wg- row)
  NOT ON ORGANIZER SITE  a sheet event the organizer's own site does not list (renamed, moved, cancelled or not posted)
  OLD COPY             an organizer row the app still has from an earlier scrape that gave it another id (venue learned
                       later); points at the current row so the app hides it
  GONE FROM ORGANIZER SITE  an upcoming organizer row whose event the organizer's site no longer lists; the app hides it
Tags add detail (VENUE DIFFERS, ALSO ON TOURNEY MACHINE, ...) and data_flags lists problems found in the scraped row."""
import sys, os, csv, json, re, argparse, pathlib, datetime as dt
from collections import defaultdict
from dataclasses import replace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dedupe import canon_org, normalize_venue, venue_match, is_compound_venue, level_tokens
from core.model import FULL_STATE
from collectors.event_match import score, core_tokens, CONFIRM_SCORE, ORG_WORDS
from collectors.organizers.match import to_event
from adapters.misc_sites import addr_key
from collectors.backfill_tourneymachine import schedule_score

HOME = pathlib.Path(os.path.expanduser("~/WoodGoodsAI"))
KIND_LABEL = {"placeholder": "placeholder (guessed from last year on Tourney Machine)", "tm": "Tourney Machine listing",
              "sheet": "App data(manual-lacrosse-sheet)"}
SOURCE_NAME = {"placeholder": "The app's guess from last year", "tm": "The Tourney Machine listing", "sheet": "App data(manual-lacrosse-sheet)"}
SOURCE_KIND = {"recurrence": "placeholder", "tourneymachine": "tm", "manual-lacrosse-sheet": "sheet"}


def _date(v):
    if isinstance(v, dt.date): return v
    try: return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError): return None


def _city_state(r):
    """(city, ST) from city/state or the first 'City, ST' in city_states. A city_states entry that is really the venue
    ('River City Sports Complex, VA', written when a source gives no town) is not a town; states must be two letters
    (the sheet has 'DC AREA')."""
    city, st = (r.get("city") or "").strip(), (r.get("state") or "").split(",")[0].strip().upper()
    cs = r.get("city_states") or []
    if cs and ", " in cs[0]:
        c2, s2 = cs[0].rsplit(", ", 1)
        if c2.strip().lower() != (r.get("venue_name") or "").strip().lower(): city = city or c2.strip()
        st = st or s2.strip().upper()
    if city and city.lower() == (r.get("venue_name") or "").strip().lower(): city = ""
    if city and (re.search(r"\b(tbd|tbc|to be (?:announced|determined))\b", city, re.I) or re.fullmatch(r"[A-Z]{2}", city)):
        city = ""                                       # 'NJ (venue TBC)', 'South Florida (TBD)': no town yet
    if not st and cs and re.match(r"[A-Z]{2}\b", cs[0] or ""): st = cs[0][:2]        # 'NJ (venue TBC)'
    if not re.fullmatch(r"[A-Z]{2}", st or ""): st = ""
    return city or None, st or None


def _town_key(city):
    """'URI, Kingston' and 'Kingston' are one town: compare the last part."""
    town = (city or "").split(",")[-1].lower().replace("saint ", "st ").replace(" township", "").replace(" twp", "")
    return re.sub(r"[^a-z]", "", town)


STATE_NAME = re.compile(r"\b(" + "|".join(sorted(map(re.escape, FULL_STATE), key=len, reverse=True)) + r")\b", re.I)


KEEP_NOTE = re.compile(r"\b(boys?|girls?|coed|hs|high school|youth|middle|ms|varsity|jv)\b", re.I)


def match_name(name):
    """Name used for matching: notes dropped ('– Site 2', '– site lists yr as 2026, presumed 2027', '(UPDATED THUR)',
    '(BOX)'; '(HS)' and '(Girls)' stay), state names as their codes ('Connecticut' = 'CT')."""
    n = re.sub(r"\s+[-–—]\s+site\b.*$", "", name or "", flags=re.I)
    n = re.sub(r"\(([^)]*)\)", lambda m: m.group(0) if KEEP_NOTE.search(m.group(1)) else " ", n)
    return re.sub(r"\s+", " ", STATE_NAME.sub(lambda m: FULL_STATE[m.group(1).lower()], n)).strip()


def load_app(export_dir, today):
    """Upcoming lacrosse rows the app shows, by kind. Rows the app marked duplicate are left out, except those hidden in
    favour of an organizer row (a placeholder our own badge record replaced): they still hold the match and its history
    links, and leaving them out would turn every confirmed event back into 'New' on the next run."""
    allr = [json.loads(line) for line in open(export_dir / "event_candidates.jsonl")]
    src = {r.get("id"): str(r.get("source") or "") for r in allr}
    rows = []
    for r in allr:
        ours = r.get("status") == "duplicate" and src.get(r.get("duplicate_of"), "").startswith(("wg-", "org-"))
        if not (r.get("sport") or "").lower().startswith("lacrosse") or (r.get("status") == "duplicate" and not ours): continue
        # the organizer-site rows themselves (wg- scrapers, org- crawler) are what is being checked, not something to
        # check them against: since the 2026-10-26 run they are in the app too
        if str(r.get("source") or "").startswith(("wg-", "org-")): continue
        s, e = _date(r.get("start_date")), _date(r.get("end_date"))
        if not s or (e or s) < today: continue
        r["_kind"] = SOURCE_KIND.get(r.get("source"), "other")
        rows.append(r)
    return rows


class Venues:
    """The app's venue table, to give scraped venue names the same ids and coordinates the app's rows carry."""
    def __init__(self, path):
        self.by_state, self.last_address = defaultdict(list), None
        if not path.exists(): return
        for line in open(path):
            v = json.loads(line)
            if v.get("state"):
                v["_names"] = [v.get("canonical_name") or ""] + list(v.get("aliases") or [])
                self.by_state[v["state"].upper()].append(v)

    def resolve(self, name, city, state):
        """(venue_id, (lat, lng), state) for a venue name, only when exactly one venue fits (same town breaks ties).
        Without a state ('Farmingdale State College') every state is searched and the venue's own state comes back."""
        if not name or not normalize_venue(name) or is_compound_venue(name): return None, None, None
        pool = self.by_state.get(state, []) if state else [v for vs in self.by_state.values() for v in vs]
        hits = [v for v in pool if any(venue_match(name, n) for n in v["_names"] if n)]
        if len(hits) > 1 and city:
            hits = [v for v in hits if _town_key(v.get("city")) == _town_key(city)] or hits
        if len(hits) != 1: return None, None, None
        v = hits[0]
        try: pt = (float(v["lat"]), float(v["lng"]))
        except (TypeError, ValueError, KeyError): pt = None
        a = (v.get("address") or "").strip()
        self.last_address = a if re.match(r"\d{1,6}\s+\S", a) else None     # a street address, not 'River City Sports Complex, Midlothian, VA'
        return v["id"], pt, v["state"].upper()


def scraped_rows(path, today):
    if path:
        rows = [json.loads(l) for l in open(path)]
    else:
        from collectors import woodgoods_lacrosse as wl
        rows, _ = wl.collect()
    for r in rows:
        r["start_date"], r["end_date"] = _date(r.get("start_date")), _date(r.get("end_date"))
    return [r for r in rows if r["start_date"] and (r["end_date"] or r["start_date"]) >= today]


def data_flags(r, today):
    """Problems in a scraped row that would mislead the dashboard if sent as is."""
    f, s, e = [], r["start_date"], r["end_date"] or r["start_date"]
    city, st = _city_state(r)
    if e < s: f.append("end before start")
    if (e - s).days > 3: f.append(f"{(e - s).days + 1} days long (league or camp?)")
    if s > today + dt.timedelta(days=550): f.append("more than 18 months out")
    if not st: f.append("no state")
    if not city and not r.get("venue_name"): f.append("no town or venue")
    if (r.get("raw") or {}).get("gender") in (None, "", "unknown"): f.append("gender unknown")
    if re.search(r"\b(learn more|register|view event|details|tbd)\b", r.get("name") or "", re.I): f.append("name looks like page text")
    if (r.get("raw") or {}).get("date_note"): f.append(f"date corrected by the scraper: {r['raw']['date_note']}")
    return f


# organizer names that appear inside event names ("APEX GIRLS SUMMER OPENER 2026", "Primetime Bulldog Brawl"); "alliance"
# is left out because it is also an ordinary word in other organizers' event names
ORG_IN_NAME = {"apex": "APEX", "nxt": "NXT", "mlt": "MLT", "primetime": "PrimeTime", "trilogy": "Trilogy", "victory": "Victory",
               "adrenaline": "Adrenaline", "aloha": "Aloha", "buku": "Buku", "hogan": "Hogan's", "hogans": "Hogan's",
               "ml8": "ML8", "nlf": "NLF", "pll": "PLL"}


def org_from_name(name):
    hits = {ORG_IN_NAME[w] for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w in ORG_IN_NAME}
    return hits.pop() if len(hits) == 1 else None


def series_key(r):
    """Rows that are one event on one weekend: 'Fall Brawl – Site 1/2/3', 'Western Shootout 2026 - Boys/- Girls'."""
    n = re.sub(r"\s*[-–—]\s*site\s*\w+.*$|\(\s*site\s*\w+\s*\)", " ", r.get("name") or "", flags=re.I)
    n = re.sub(r"\b(boys?|girls?|coed|(?:19|20)\d\d)\b", " ", n, flags=re.I)
    n = re.sub(r"\b(?:fri|satur|sun)day\b|\b(?:fri|sat|sun)\b", " ", n, flags=re.I)   # 'Veterans Day Classic: SATURDAY/SUNDAY ...'
    s0 = _date(r.get("start_date"))
    week = s0 - dt.timedelta(days=s0.weekday()) if s0 else None
    return r["_kind"], canon_org(r.get("organizer_name")), " ".join(sorted(core_tokens(n))), week


def newest_export():
    """The most recent local copy of the app's data that has what the report reads: backup/db/current (tools/backup_db.py)
    or backup/db/fresh (a partial pull), whichever was written last."""
    have = [p for p in (HOME / "backup/db/current", HOME / "backup/db/fresh")
            if all((p / f).exists() for f in ("event_candidates.jsonl", "event_history.jsonl", "venues.jsonl"))]
    return max(have, key=lambda p: (p / "event_candidates.jsonl").stat().st_mtime)


HISTORY_KEEP = ("id", "name", "start_date", "end_date", "team_count", "venues", "listing_url")


def history_rows(dirs, ids):
    """history id -> last year's Tourney Machine record (dates, teams, venues[], link), from event_history.jsonl in the
    first export that has it."""
    out = {}
    for d in dirs:
        p = d / "event_history.jsonl"
        if not p.exists(): continue
        for line in open(p):
            r = json.loads(line)
            if r.get("id") in ids: out[r["id"]] = {k: r.get(k) for k in HISTORY_KEEP}
        break
    return out


def _acronym(name):
    """'Orange County Great Park' -> 'OCGP', 'OC Great Park' -> 'OCGP', 'University of RI' -> 'URI'."""
    words = [w for w in re.findall(r"[A-Za-z]+", name or "") if w.lower() not in ("of", "the", "and", "at")]
    return "".join(w.upper() if w.isupper() and len(w) <= 3 else w[0].upper() for w in words)


def same_venue_loose(a, b):
    """Venue names Tourney Machine shortens: 'HBSC' = 'Huntington Beach Sports Complex', 'WCU' = 'West Chester University',
    'Hoover Met Complex' = 'Hoover Metropolitan Complex'. Only used inside one matched event, where the town agrees."""
    if not (a and b): return False
    for x, y in ((a.strip(), b), (b.strip(), a)):            # before _real_venue, which reads 'HBSC' as two states
        if re.fullmatch(r"[A-Z]{3,5}", x) and _real_venue(y) and _acronym(y) == x: return True
    if not (_real_venue(a) and _real_venue(b)): return False
    wa, wb = normalize_venue(a).split(), normalize_venue(b).split()
    return len(wa) == len(wb) >= 2 and all(p == q or (min(len(p), len(q)) >= 3 and (p.startswith(q) or q.startswith(p)))
                                           for p, q in zip(wa, wb))


def venue_here(v, r):
    """Is venue v of a Tourney Machine record this organizer site? The same venue name, or the same street address."""
    if same_venue(v.get("venue_name"), r.get("venue_name")) or same_venue_loose(v.get("venue_name"), r.get("venue_name")): return True
    a, b = addr_key(v.get("address")), addr_key((r.get("raw") or {}).get("address") or r.get("_address"))
    return bool(a and b and a[0].isdigit() and a == b)


def last_year_records(hrows):
    """The records of last year's edition: the newest weekend among the linked history (older years left out)."""
    dated = [(h, _date(h.get("start_date"))) for h in hrows if _date(h.get("start_date"))]
    if not dated: return []
    latest = max(s for _, s in dated)
    return sorted((h for h, s in dated if (latest - s).days <= 3), key=lambda h: str(h.get("start_date")))


def venue_schedule_quality(days):
    """The same 0-100 schedule score every Tourney Machine event gets (collectors.backfill_tourneymachine), on this venue's
    days only: Bucks Mill Park's lone Saturday, Mercer County Park's Saturday + Sunday. None when the days lack the numbers."""
    try:
        return (schedule_score(days) or {}).get("schedule_quality")
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def last_year_at(r, hrows):
    """Last year's numbers at this organizer site's venue, added up over last year's records of the event: Trilogy ran
    Veterans Day Classic 2025 as a Saturday and a Sunday tournament, both at Mercer County Park (64 + 70 teams, different
    age groups). None when last year's edition didn't use this venue."""
    hits = [(h, v) for h in last_year_records(hrows) for v in (h.get("venues") or []) if isinstance(v, dict) and venue_here(v, r)]
    if not hits: return None
    # 'Mercer County Park - Field 14 (Turf)' on Saturday is 'Mercer County Park - Field 14' on Sunday
    fields = sorted({re.sub(r"\s*\([^)]*\)", "", f).strip() for _, v in hits for f in (v.get("fields") or []) if isinstance(f, str)})
    total = lambda k: sum(int(v.get(k) or 0) for _, v in hits) or None
    days = sorted((d for _, v in hits for d in (v.get("schedule") or []) if isinstance(d, dict)), key=lambda d: str(d.get("date")))
    return {"year": _date(hits[0][0]["start_date"]).year, "venue_name": hits[0][1].get("venue_name"),
            "address": hits[0][1].get("address"), "teams": total("teams"), "games": total("games"),
            "field_count": len(fields) or max(int(v.get("field_count") or 0) for _, v in hits) or None, "fields": fields,
            "schedule": days, "schedule_quality": venue_schedule_quality(days),
            "from": [{"history_id": h["id"], "name": h.get("name"), "start_date": str(h.get("start_date")),
                      "teams_here": v.get("teams"), "event_teams": h.get("team_count"), "url": h.get("listing_url")} for h, v in hits]}


def venue_status(r, ly, recs):
    """Why a site has (or lacks) last year's venue numbers. 'new venue' only when both sides name their venues: an
    organizer giving no venue, or last year's records without a venue list, prove nothing."""
    if ly: return "same venue as last year"
    if not recs: return ""
    if not (_real_venue(r.get("venue_name")) or (r.get("raw") or {}).get("address")): return "organizer gives no venue"
    if not any(isinstance(v, dict) and v.get("venue_name") for h in recs for v in (h.get("venues") or [])):
        return "last year's venues unknown"
    town = _town_key(_city_state(r)[0])
    towns = {_town_key(a.split(",")[-2]) for h in recs for v in (h.get("venues") or []) if isinstance(v, dict)
             for a in [v.get("address") or ""] if a.count(",") >= 2}
    if town and town in towns: return "same town as last year, venue name differs"     # 'Bryant University' / 'CONATY INDOOR ATHLETIC CENTER'
    return "new venue this year"


def last_year_text(ly):
    """'2025: 134 teams, 16 fields (Nov 8 64, Nov 9 70)' for the CSV."""
    if not ly: return ""
    days = ", ".join(f"{_date(x['start_date']):%b %-d} {x['teams_here']}" for x in ly["from"]) if len(ly["from"]) > 1 else ""
    return (f"{ly['year']}: {ly['teams'] or '?'} teams, {ly['field_count'] or '?'} fields at {ly['venue_name']}"
            + (f" ({days})" if days else ""))


def needs_check(flags, status):
    """CHECK DATA only for what could mislead the dashboard: a corrected year, impossible or league-length dates, page text
    as a name, or no place on an event that was not matched to an app row with one. 'gender unknown' and a missing state
    on an event whose place matched the app's stay in data_flags only."""
    serious = ("date corrected", "end before start", "days long", "more than 18 months", "name looks like page text")
    if any(f.startswith(serious) or any(k in f for k in serious) for f in flags): return True
    place_matched = status in ("CONFIRMED", "PLACE DIFFERS", "DATE MOVED")
    return not place_matched and any(f in ("no state", "no town or venue") for f in flags)


PLAIN_WORDS = {"boys", "boy", "girls", "girl", "coed", "championships", "championship", "tournament"} | ORG_WORDS


def same_words(a, b):
    """'Girls Winter Sixes Championships' and 'APEX WINTER SIXES 2026': the same words once the organizer, boys/girls and
    'championship(s)' are set aside (at least two words left)."""
    wa, wb = core_tokens(match_name(a)) - PLAIN_WORDS, core_tokens(match_name(b)) - PLAIN_WORDS
    return len(wa) >= 2 and wa == wb


def status_for(kind, appr, r, res, off, pc):
    """(status, explanation, extra tags) for one app row against one organizer event."""
    label = KIND_LABEL.get(kind, kind)
    if res["decision"] != "confirmed":
        return "REVIEW", f"Possible match with {label}; the evidence is weak, check by hand.", []
    if abs(off) <= 2 and pc is True:                 # same event, same weekend, same place: everything matches
        return "CONFIRMED", {"placeholder": "The organizer site lists it the same weekend at the same place: the app's "
                                            "placeholder (guessed from last year) is real. Safe to show as confirmed.",
                             "tm": "Also listed on Tourney Machine; both agree on the weekend and the place.",
                             "sheet": "Matches App data(manual-lacrosse-sheet); the organizer site agrees on the weekend and "
                                      "the place."}.get(kind, "Matches an app row."), []
    if abs(off) <= 2 and pc:
        return "PLACE DIFFERS", (f"Same event and weekend, but {label} has {place(appr) or 'another place'} and the organizer "
                                 f"site says {place(r)} ({pc}). Show the organizer's place."), []
    if abs(off) <= 2:
        return "DATE CONFIRMED", (f"The organizer site confirms the weekend, but one side gives no town ({place(appr) or 'nothing'} "
                                  f"vs {place(r) or 'nothing'}), so the place could not be checked."), []
    if kind == "placeholder" and res.get("alt") and appr.get("_alt_start") and abs((r["start_date"] - appr["_alt_start"]).days) <= 2:
        return "DATE MOVED", (f"The app shows {appr['start_date']}: last year's date ({appr['_last_year']}) + 371 days, by week "
                              f"number. The organizer site says {r['start_date']}, the same weekend as last year. The app's date "
                              f"is wrong; show the organizer's."), ["APP DATE A WEEK LATE (week-number rule)"]
    if kind == "placeholder":
        return "DATE MOVED", (f"The app guessed {appr['start_date']} from last year's Tourney Machine event; the organizer site "
                              f"says {r['start_date']}. The guessed date is wrong; show the organizer's."), ["ORGANIZER CHANGED THE WEEKEND"]
    return "DATE CONFLICT", (f"{label[0].upper() + label[1:]} says {appr['start_date']}, the organizer site says {r['start_date']}. "
                             f"Don't show either as certain until checked."), []


def score_pair(app_ev, scr_ev, same_town, same_venue=False):
    """event_match.score, plus the same town as evidence when the venue could not be resolved to the same facility.
    Name + town + weekend is strong for a placeholder, whose organizer is unknown; town alone never confirms."""
    res = score(app_ev, scr_ev)
    if res["decision"] == "no_match" and res["score"] <= 0: return res
    la, ls = level_tokens(app_ev.name), level_tokens(scr_ev.name)
    if la and ls and not la & ls:                       # 'Albany Invitational (HS)' is not '... at Albany (Youth)'
        s = res["score"] - 3
        strong = set(res["anchors"]) & {"organizer", "name"}
        res = {**res, "score": round(s, 2), "reasons": res["reasons"] + [f"{'/'.join(la)} vs {'/'.join(ls)} -3"],
               "decision": "confirmed" if s >= CONFIRM_SCORE and len(res["anchors"]) >= 2 and strong else
                           "review" if s >= 3.5 and strong else "no_match"}
        if res["decision"] == "no_match": return res
    if same_venue and "venue" not in res["anchors"]:     # 'Mercer County Park' on both sides, same state, ids unresolved
        s = res["score"] + 2
        anchors = sorted(set(res["anchors"]) | {"venue"})
        strong = set(anchors) & {"organizer", "name"}
        decision = "confirmed" if s >= CONFIRM_SCORE and len(anchors) >= 2 and strong else res["decision"]
        res = {**res, "score": round(s, 2), "anchors": anchors, "decision": decision,
               "reasons": res["reasons"] + ["same venue name +2"], "moved": decision != "no_match" and res["days_apart"] > 2}
    elif same_town and "venue" not in res["anchors"]:
        s = res["score"] + 1.5
        anchors = sorted(set(res["anchors"]) | {"town"})
        strong = set(anchors) & {"organizer", "name"}
        decision = "confirmed" if s >= CONFIRM_SCORE and len(anchors) >= 2 and strong else res["decision"]
        res = {**res, "score": round(s, 2), "anchors": anchors, "decision": decision,
               "reasons": res["reasons"] + ["same town +1.5"], "moved": decision != "no_match" and res["days_apart"] > 2}
    return res


def _real_venue(v):
    """A venue name worth comparing: not empty, not 'TBD'/'TBC', not 'Multiple Locations', not a list of states ('DE and PA')."""
    v = (v or "").strip()
    return bool(v) and bool(normalize_venue(v)) and not re.search(r"\b(tbd|tbc)\b", v, re.I) \
        and not re.fullmatch(r"(?:[A-Z]{2}(?:\s*(?:,|and|&|/)\s*)?)+", v)


def same_venue(a, b):
    """True / False when both sides name a real venue, None otherwise. Word overlap on the distinctive words, without
    core.dedupe's two-venue rule ('Iron Peak Sports & Events' is one venue)."""
    if not (_real_venue(a) and _real_venue(b)): return None
    s1, s2 = set(normalize_venue(a).split()), set(normalize_venue(b).split())
    if not s1 or not s2: return None
    return s1 <= s2 or s2 <= s1 or len(s1 & s2) / len(s1 | s2) >= 0.6


def app_address(r):
    """The app row's street address: raw.address, else the first listing venue's address (Tourney Machine)."""
    a = (r.get("raw") or {}).get("address") if isinstance(r.get("raw"), dict) else None
    if not a:
        a = next((v.get("address") for v in (r.get("venues") or []) if isinstance(v, dict) and v.get("address")), None)
    return a or ""


CONTACT_ROLE = {"vendor": "vendor/sponsor", "director": "director", "organizer": "organizer inbox"}


def event_details(r):
    """What the organizer's page says beyond date and place: boys/girls, age divisions (grad years), event type, the
    vendor/sponsor inbox, every contact with its role, and any note on the date."""
    raw = r.get("raw") or {}
    contacts = raw.get("contacts") or []
    return {"boys_girls": raw.get("gender") or "", "age_divisions": r.get("age_groups") or "",
            "event_type": raw.get("event_type") or "", "vendor_contact": r.get("vendor_contact_email") or "",
            "contacts": "; ".join(" ".join(x for x in (c.get("name"), c.get("email"), c.get("phone"),
                                                        f"({CONTACT_ROLE.get(c.get('role'), c.get('role'))})") if x) for c in contacts),
            "date_note": raw.get("date_note") or ""}


def place(r):
    city, st = _city_state(r)
    return ", ".join(x for x in (r.get("venue_name"), ", ".join(x for x in (city, st) if x)) if x)


def place_check(app_r, scr_r, res):
    """None when unknown, True when the place agrees, else a short description of the difference."""
    ac, ast = _city_state(app_r)
    sc, sst = _city_state(scr_r)
    if ast and sst and ast != sst: return f"state {ast} vs {sst}"
    if "venue" in res["anchors"] or (ac and sc and _town_key(ac) == _town_key(sc)): return True
    vm = same_venue(app_r.get("venue_name"), scr_r.get("venue_name"))       # None when either side has no real venue
    if vm: return True
    if ac and sc: return f"town {ac} vs {sc}"
    if vm is False: return f"venue {app_r.get('venue_name')} vs {scr_r.get('venue_name')}"
    return None


def fmt_dates(start, end):
    """'Jun 19–20, 2027', 'Jun 29 – Jul 1, 2027', 'Jun 19, 2027'."""
    s, e = _date(start), _date(end) or _date(start)
    if not s: return ""
    if e == s: return f"{s:%b} {s.day}, {s.year}"
    if (e.year, e.month) == (s.year, s.month): return f"{s:%b} {s.day}–{e.day}, {s.year}"
    if e.year == s.year: return f"{s:%b} {s.day} – {e:%b} {e.day}, {s.year}"
    return f"{s:%b} {s.day}, {s.year} – {e:%b} {e.day}, {e.year}"


def review_reason(res, off, pc):
    """'Same name and weekend, but a different town (Towson vs Baltimore), so the evidence is only 6.0. ...'"""
    why = " ".join(res["reasons"])
    name = ("Same name" if re.search(r"distinctive name match|same generic name", why) else
            "Similar name" if "partial name" in why else
            "Different names" if re.search(r"different names|share nothing", why) else
            "A name too generic to compare")
    when = "same weekend" if abs(off) <= 2 else f"{abs(off)} days apart"
    where = (", same town" if pc is True else f", but a different {pc.split()[0]} ({pc.split(' ', 1)[1]})" if pc else
             ", and no town on one side to compare")
    return (f"{name}, {when}{where}. Score {res['score']}: not enough to claim a match, which needs 6+ points from two "
            f"different kinds of proof (organizer, name, venue or town). It may be the same event, perhaps at a second site, "
            f"but the tool won't claim that. Look at both links and decide.")


def badge(status, r, a, res, off, pc):
    """(badge, description) for the dashboard: the organizer's website is the real source."""
    org_dates = fmt_dates(r.get("start_date"), r.get("end_date")) if r else ""
    if status == "CONFIRMED":
        return "🟢 Confirmed · organizer website", "Confirmed by organizer website"
    if status == "DATE CONFIRMED":
        return "🟢 Date confirmed · place not checked", ("Organizer website confirms the date; one side gives no town, so the "
                                                        "place could not be compared")
    had = {"placeholder": "app guessed", "sheet": "sheet had", "tm": "Tourney Machine had"}.get((a or {}).get("_kind"), "app had")
    if status == "PLACE DIFFERS":
        app_place = ", ".join(x for x in _city_state(a) if x) or place(a) or "another place"
        return (f"🟠 Place corrected · {had.replace('guessed', 'had')} {app_place}, organizer says {place(r)}",
                "Same event and weekend, but the organizer's website lists a different place; the organizer's site is the "
                "real source")
    if status in ("DATE MOVED", "DATE CONFLICT"):
        # the app now shows the organizer's dates, so the badge says what it replaced: 'app guessed Nov 7'
        app_dates = fmt_dates(a.get("start_date"), a.get("end_date"))
        org_y, app_y = str(r.get("start_date"))[:4], str(a.get("start_date"))[:4]
        if org_y == app_y:
            app_dates = re.sub(r",\s*\d{4}$", "", app_dates)
            org_dates = org_dates
        desc = ("Organizer might have changed the date" if status == "DATE MOVED" else
                f"{SOURCE_NAME.get(a.get('_kind'), 'The app')} disagrees with the organizer's website; the organizer's "
                f"site is the only real source")
        return f"🔴 Date corrected · {had} {app_dates}, organizer says {org_dates}", desc
    if status == "REVIEW":
        return "🟡 Possible match, check", review_reason(res, off, pc)
    if status == "NOT ON ORGANIZER SITE":
        src = "App data(manual-lacrosse-sheet)" if a.get("_kind") == "sheet" else "a historical event"
        return "⚪ Not on organizer site", f"Loaded from {src}, not on the organizer's website"
    if status == "NEW SITE":
        return "🔵 New site · organizer website", ("Another venue of an event the app already has; the app doesn't list this "
                                                  "site yet (the weekly run adds it)")
    return "🔵 New · organizer website", "On the organizer's website, not in the app yet (the weekly run adds it)"


SITE_NOTE = re.compile(r"\s+[-–—]\s+site\s+\d+$", re.I)


def leftover_rows(export_dir, scr, today):
    """Organizer rows the app still shows that this scrape no longer produces: a row's id is its event + date + venue
    (else town), so when a scraper learns the venue ('Fall Classic (Boys) – Site 2' at NEBC, first sent as just
    Northborough) the app gets a new row and keeps the old one. Only upcoming rows of organizers this scrape reached.
    Returns [(old row, its current row)] where the current row is the same event on the same date, in the same town when
    that picks one site, else the event's first site; current row None when the organizer's site no longer lists the
    event. An old row is only paired with a current row the app already has, so nothing points at a missing row."""
    keys = {f"{r['source']}:{r['source_event_id']}" for r in scr}
    sources = {r["source"] for r in scr}
    base = lambda n: SITE_NOTE.sub("", n or "").strip().lower()
    app = [json.loads(l) for l in open(export_dir / "event_candidates.jsonl")]
    in_app = {a.get("dedupe_key") for a in app}
    by = defaultdict(list)
    for r in scr:
        if f"{r['source']}:{r['source_event_id']}" in in_app:
            by[(r["source"], base(r["name"]), _date(r["start_date"]))].append(r)
    # a scrape missing many of the events the app shows for that organizer (a page that failed to load: 3+ events and
    # over 30% gone at once) retires nothing as 'gone': those events are probably still on the site. Events, not rows:
    # an event's sites and old copies count once
    live, got = defaultdict(set), defaultdict(set)
    for a in app:
        e0 = _date(a.get("end_date")) or _date(a.get("start_date"))
        if a.get("source") in sources and a.get("status") not in ("duplicate", "stale") and e0 and e0 >= today:
            live[a["source"]].add((base(a.get("name")), _date(a.get("start_date"))))
    for r in scr:
        if (_date(r.get("end_date")) or _date(r["start_date"])) >= today:
            got[r["source"]].add((base(r["name"]), _date(r["start_date"])))
    partial = {src for src in sources if len(live[src] - got[src]) >= 3 and len(live[src] - got[src]) > 0.3 * len(live[src])}
    out = []
    for a in app:
        if a.get("source") not in sources or a.get("status") in ("duplicate", "stale") or a.get("dedupe_key") in keys: continue
        s0, e0 = _date(a.get("start_date")), _date(a.get("end_date"))
        if not s0 or (e0 or s0) < today: continue
        sibs = sorted(by.get((a["source"], base(a.get("name")), s0), []), key=lambda r: r["name"])
        if not sibs and any(base(r["name"]) == base(a.get("name")) and _date(r["start_date"]) == s0 for r in scr):
            continue                                   # its current row isn't in the app yet: wait for it
        town = [r for r in sibs if _town_key(r.get("city")) and _town_key(r.get("city")) == _town_key(a.get("city"))]
        if not sibs and a["source"] in partial:
            continue
        out.append((a, (town if len(town) == 1 else sibs or [None])[0]))
    return out


def leftover_records(pairs, now):
    """Report lines and badge records for leftover organizer rows: an old copy points at its current row (replaced_by,
    hidden like a replaced placeholder); one whose event is gone from the organizer's site is retired."""
    lines, recs = [], []
    for a, r in pairs:
        key = f"{r['source']}:{r['source_event_id']}" if r else None
        if r:
            status, bdg = "OLD COPY", "⚪ Old copy · replaced by the current organizer row"
            means = f"An earlier version of {r['name']} ({place(r)}) from the organizer's website; hidden in favour of {key}."
        else:
            status, bdg = "GONE FROM ORGANIZER SITE", "⚪ No longer on organizer website"
            means = "Came from the organizer's website, which no longer lists it (renamed, moved or cancelled); hidden."
        lines.append({"status": status, "badge": bdg, "badge_description": means, "details": means,
                      "organizer": a.get("organizer_name"), "event": a.get("name"), "start": a.get("start_date"),
                      "end": a.get("end_date"), "place": place(a), "organizer_url": a.get("listing_url"),
                      "app_dedupe_key": a.get("dedupe_key")})
        rec = {"app_dedupe_key": a["dedupe_key"], "status": "superseded" if r else "not_on_organizer_site", "badge": bdg,
               "badge_description": means, "details": means, "tags": [], "checked_at": now,
               "organizer": a.get("organizer_name"), "organizer_event_name": a.get("name"),
               "organizer_start": a.get("start_date"), "organizer_end": a.get("end_date"), "organizer_url": a.get("listing_url")}
        rec.update({"replaced_by": key} if r else {"retire": True})
        recs.append(rec)
    return lines, recs


def placed(r):
    """Does an organizer row name a real place: a venue (not 'Multiple Locations', 'DE and PA', TBD) or a town?"""
    v = r.get("venue_name") or ""
    return bool((_real_venue(v) and not re.search(r"multiple locations", v, re.I)) or _city_state(r)[0])


FIRM = ("CONFIRMED", "DATE CONFIRMED", "PLACE DIFFERS", "DATE MOVED", "DATE CONFLICT")


def event_links(scr, app, sgroups, linked, hrows):
    """For each organizer event matched for sure (never a REVIEW): what its site rows should take over from the app's
    copies. Every site of the event gets
      history_ids          last year's Tourney Machine records the matched placeholders were copied from (verified: the
                           organizer lists the event the same way), to link as confirmed precedent
      earlier_history_ids  older years the app itself had linked to those rows (its own guess, keep the tier it had)
      replaces             the placeholders and App data(manual-lacrosse-sheet) rows the site rows stand in for (to mark
                           duplicate once their data is copied): a sheet row points at the site in its own town
      same_event_keys      Tourney Machine listings of the same event (left as they are)
      last_year_venue      last year's numbers at this site's venue (None: see last_year_venue_status)
      last_year_venue_status  same venue as last year / same town as last year, venue name differs /
                           new venue this year / organizer gives no venue / last year's venues unknown
      last_year_event      last year's whole edition: its records, total teams, venues not listed this year
      primary_site         the site the replaced placeholders point to (most teams there last year, else the first)
    Returns ({site index: fields}, {replaced row key: organizer site key})."""
    site_extra, replaced_by = {}, {}
    for sites in sgroups.values():
        firm = [(j, st) for i in sites for j, st in linked.get(i, []) if st in FIRM]
        if not firm: continue
        ph = sorted({j for j, _ in firm if app[j]["_kind"] == "placeholder"})
        # a sheet row is replaced only by a site that says at least as much about the place, and never when the sheet's
        # date disagrees ('Spring Break Training Florida (multi-week)'): 'Fall Invitational – Site 1/2/3' in New Castle,
        # Georgetown and PA stay rather than give way to one organizer row that only says 'DE and PA'
        kinds = defaultdict(set)
        for j, st in firm: kinds[j].add(st)
        sheet = sorted(j for j in kinds if app[j]["_kind"] == "sheet" and "DATE CONFLICT" not in kinds[j]
                       and all(placed(scr[i]) for i in sites))
        others = sorted({j for j, _ in firm if app[j]["_kind"] not in ("placeholder", "sheet")})
        last = [h for h in dict.fromkeys((app[j].get("raw") or {}).get("history_id") for j in ph) if h in hrows]
        earlier = [h for h in dict.fromkeys(h for j in ph + sheet + others for h in (app[j].get("history_ids") or []))
                   if h in hrows and h not in last]
        recs = last_year_records([hrows[h] for h in last])
        here = {i: last_year_at(scr[i], recs) for i in sites}
        unused = [v.get("venue_name") for h in recs for v in (h.get("venues") or []) if isinstance(v, dict)
                  and not any(venue_here(v, scr[i]) for i in sites)]
        event = {"year": _date(recs[0]["start_date"]).year, "teams": sum(int(h.get("team_count") or 0) for h in recs) or None,
                 "records": [{"history_id": h["id"], "name": h.get("name"), "start_date": str(h.get("start_date")),
                              "teams": h.get("team_count"), "url": h.get("listing_url")} for h in recs],
                 "venues_not_listed_this_year": list(dict.fromkeys(unused))} if recs else None
        primary = max(sites, key=lambda i: ((here[i] or {}).get("teams") or 0, -sites.index(i)))
        key = lambda i: f"{scr[i]['source']}:{scr[i]['source_event_id']}"
        for j in ph: replaced_by[app[j]["dedupe_key"]] = key(primary)
        # the sheet lists sites one per row ('Fall Classic (Boys) – Site 1' in Northborough): the site in that town, else
        # (no town, 'NJ (venue TBC)', or another town) the primary site, like the placeholders
        for j in sheet:
            town = _town_key(_city_state(app[j])[0])
            same = [i for i in sites if town and _town_key(_city_state(scr[i])[0]) == town]
            replaced_by[app[j]["dedupe_key"]] = key(same[0] if len(same) == 1 else primary)
        for i in sites:
            site_extra[i] = {"history_ids": last, "earlier_history_ids": earlier,
                             "replaces": [app[j]["dedupe_key"] for j in ph + sheet],
                             "same_event_keys": [app[j]["dedupe_key"] for j in others],
                             "last_year_venue": here[i], "last_year_venue_status": venue_status(scr[i], here[i], recs),
                             "last_year_event": event, "primary_site": i == primary}
    return site_extra, replaced_by


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export-dir", type=pathlib.Path)
    ap.add_argument("--scraped", type=pathlib.Path, help="jsonl of woodgoods_lacrosse rows from an earlier run")
    ap.add_argument("--out", type=pathlib.Path)
    ap.add_argument("--send-one", metavar="DEDUPE_KEY", help="send the badge record(s) for one app event to the app (a test)")
    ap.add_argument("--send-event", metavar="NAME", help="send every badge record of one organizer event (all its sites and the "
                    "app rows they replace), e.g. 'Veterans Day Classic' (a test)")
    ap.add_argument("--send", action="store_true", help="send every badge record to the app")
    args = ap.parse_args(argv)
    today = dt.date.today()
    export = args.export_dir or newest_export()
    out = args.out or HOME / "reports" / f"lacrosse_match_{today}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    scr = scraped_rows(args.scraped, today)
    if not args.scraped:                                   # keep what was scraped so a rerun can skip the 5-minute crawl
        with open(out.with_suffix(".scraped.jsonl"), "w") as f:
            for r in scr: f.write(json.dumps(r, default=str) + "\n")
    app = load_app(export, today)
    venues = Venues(export / "venues.jsonl")
    for r in scr:
        city, st = _city_state(r)
        venues.last_address = None
        vid, pt, vst = venues.resolve(r.get("venue_name"), city, st)
        r["venue_ids"], r["venues"] = ([vid] if vid else []), ([{"lat": pt[0], "long": pt[1]}] if pt else [])
        if vst and not st: r["state"], r["_state_from_venue"] = vst, True
        # the street address: the organizer's own page first, else the app's venue list when the venue name matches
        # exactly one venue there (MLT never prints addresses)
        own = (r.get("raw") or {}).get("address")
        r["_address"], r["_address_source"] = ((own, "organizer website") if own else
                                               (venues.last_address, "app venue list") if vid and venues.last_address else ("", ""))
    for r in app:                                       # placeholders carry no organizer; their names often do
        if not r.get("organizer_name") and org_from_name(r.get("name")): r["organizer_name"] = org_from_name(r["name"])
    hids = {(r.get("raw") or {}).get("history_id") for r in app if r["_kind"] == "placeholder"}
    hids |= {h for r in app for h in (r.get("history_ids") or []) if isinstance(h, str)}
    hrows = history_rows([export, HOME / "backup/db/current"], hids - {None})
    hist = {k: _date(h.get("start_date")) for k, h in hrows.items()}
    for r in app:
        h, s0 = hist.get((r.get("raw") or {}).get("history_id")), _date(r.get("start_date"))
        if r["_kind"] == "placeholder" and h and s0 and (s0 - h).days == 371:
            r["_last_year"], r["_alt_start"] = h, s0 - dt.timedelta(days=7)     # the app's week-number rule ran a week late
        elif h: r["_last_year"] = h
    S = [replace(to_event(r), name=match_name(r["name"])) for r in scr]
    A = [replace(to_event(r), name=match_name(r["name"])) for r in app]
    towns = [_town_key(_city_state(r)[0]) for r in scr]

    def pair(j, i):
        """Best evidence for app row j vs organizer event i: at the app's date, and for a placeholder the app rolled
        a week late, also at last year's date + 364 days."""
        if not A[j].start or abs((A[j].start - S[i].start).days) > 63: return None
        ac = _town_key(_city_state(app[j])[0])
        same_town = bool(ac) and ac == towns[i] and A[j].state == S[i].state
        same_v = A[j].state == S[i].state and bool(same_venue(app[j].get("venue_name"), scr[i].get("venue_name")))
        res = {**score_pair(A[j], S[i], same_town, same_v), "alt": False}
        if app[j].get("_alt_start"):
            alt = {**score_pair(replace(A[j], start=app[j]["_alt_start"]), S[i], same_town, same_v), "alt": True}
            if alt["score"] > res["score"]: res = alt
        # the same organizer alone is not evidence: APEX runs dozens of events. A possible match needs the names to agree,
        # counting 'Girls Fall Prelim' = 'APEX FALL PRELIM 2026' (same words once organizer and boys/girls are set aside)
        if res["decision"] == "no_match": return None
        return res if "name" in res["anchors"] or same_words(app[j]["name"], scr[i]["name"]) else None

    def rank(j, i, res):                               # ties: closer name, then closer date (the NXT Can-Am pair)
        a_, b_ = core_tokens(app[j]["name"]), core_tokens(scr[i]["name"])
        return (res["score"], len(a_ & b_) / max(1, len(a_ | b_)), -abs((A[j].start - S[i].start).days))

    # one-to-one per kind of app row, event against event: on the app side "Site 1/2/3" and boys/girls rows of one event
    # on one weekend are one group; on the organizer side the "– Site N" rows of one event are one group. Inside a matched
    # pair each organizer site keeps its own best app row, so "Lax Fest – Site 2 (Destination KP)" meets the app's
    # Destination KP site.
    sgroups = defaultdict(list)
    for i, r in enumerate(scr):
        s0 = r["start_date"]
        sgroups[(canon_org(r.get("organizer_name")), match_name(r["name"]).lower(), s0 - dt.timedelta(days=s0.weekday()))].append(i)
    matches = defaultdict(dict)                        # organizer site -> kind -> [(app index, result)], best first
    extra_site = {}                                    # organizer site -> the app row its event matched at another site
    for kind in ("placeholder", "tm", "sheet", "other"):
        groups = defaultdict(list)
        for j, r in enumerate(app):
            if r["_kind"] == kind: groups[series_key(r)].append(j)
        cand = []
        for g, members in groups.items():
            for h, sites in sgroups.items():
                m = [(j, i, res) for j in members for i in sites for res in [pair(j, i)] if res]
                if m:
                    m.sort(key=lambda x: rank(x[0], x[1], x[2]), reverse=True)
                    cand.append((rank(*m[0]), g, h, m))
        cand.sort(key=lambda c: c[0], reverse=True)
        used_g, used_h = set(), set()
        for _, g, h, m in cand:
            if g in used_g or h in used_h: continue
            used_g.add(g); used_h.add(h)
            for i in sgroups[h]:
                mine = [(j, res) for j, ii, res in m if ii == i]
                if mine: matches[i][kind] = mine
            # a site whose place matches none of the app's rows, while a sibling site matched one at its own place, is a
            # venue the app doesn't list (The Ivy's Quinnipiac site next to the app's Yale row), not a doubtful match
            placed = {i for i in sgroups[h] if any(place_check(app[j], scr[i], res) is True for j, res in matches[i].get(kind, []))}
            if placed:
                for i in sgroups[h]:
                    if i not in placed and kind in matches[i]:
                        extra_site[i] = matches[i].pop(kind)[0][0]

    cols = ["status", "badge", "badge_description", "tags", "details", "organizer", "event", "start", "end", "place", "address",
            "address_source", "boys_girls", "age_divisions", "event_type", "vendor_contact", "contacts", "date_note",
            "organizer_url", "app_match", "app_event", "app_start", "app_end", "app_place", "app_address", "app_guessed_from",
            "last_year_here", "replaces", "days_off", "match_score",
            "evidence", "also_in_app", "data_flags", "app_dedupe_key", "app_listing_url"]
    lines, counts, checks = [], defaultdict(int), []
    linked, line_at = defaultdict(list), {}            # organizer site -> [(app row, status)]; -> its report line
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for i, r in enumerate(scr):
        m = matches.get(i, {})
        tags, flags = [], data_flags(r, today)
        if r.get("_state_from_venue"): flags.append(f"state {r['state']} taken from the app's venue list")
        conf = [k for k in ("placeholder", "sheet", "tm", "other") if k in m and m[k][0][1]["decision"] == "confirmed"]
        primary = (conf or [k for k in ("placeholder", "sheet", "tm", "other") if k in m] or [None])[0]
        status, means, j, res = "NEW", "On the organizer's website but not in the app. The weekly run adds it.", None, None
        if i in extra_site and not m:
            status = "NEW SITE"
            means = (f"Another site of an event the app already has ({app[extra_site[i]]['name']}, "
                     f"{place(app[extra_site[i]]) or 'no place'}); the app doesn't list this venue. The weekly run adds it.")
        if primary:
            # the member whose place agrees best stands for the group ("Fall Classic – Site 2" in the organizer's town)
            j, res = max(m[primary], key=lambda x: (place_check(app[x[0]], r, x[1]) is True, rank(x[0], i, x[1])))
            off = (r["start_date"] - A[j].start).days
            pc = place_check(app[j], r, res)
            status, means, extra = status_for(primary, app[j], r, res, off, pc)
            linked[i].append((j, status))
            tags += extra
            if status not in ("PLACE DIFFERS", "DATE CONFIRMED"):     # those statuses already say it
                if pc is not True and pc is not None: tags.append(f"VENUE DIFFERS ({pc})")
                elif pc is None and res["decision"] == "confirmed": tags.append("PLACE NOT CHECKED (no town on one side)")
            for k in conf:
                if k == primary: continue
                tags.append({"tm": "ALSO ON TOURNEY MACHINE", "sheet": "ALSO ON SHEET", "placeholder": "ALSO A PLACEHOLDER"}.get(k, "ALSO IN APP"))
                if abs((r["start_date"] - A[m[k][0][0]].start).days) > 2: tags.append(f"DATE CONFLICT WITH {KIND_LABEL[k].upper()}")
        if needs_check(flags, status): tags.append("CHECK DATA")
        counts[status] += 1
        a = app[j] if j is not None else {}
        others = [(k, jj) for k, mm in m.items() for jj, _ in mm if jj != j]
        bdg, bdesc = badge(status, r, a, res, (r["start_date"] - A[j].start).days if j is not None else 0,
                           place_check(app[j], r, res) if j is not None else None)
        lines.append({"status": status, "badge": bdg, "badge_description": bdesc, "tags": "; ".join(tags), "details": means,
                      "organizer": r.get("organizer_name"),
                      "event": r["name"], "start": r["start_date"], "end": r["end_date"], "place": place(r),
                      "address": r["_address"], "address_source": r["_address_source"],
                      **event_details(r),
                      "organizer_url": r.get("listing_url"), "app_match": KIND_LABEL.get(primary, primary) if primary else "",
                      "app_event": a.get("name", ""), "app_start": a.get("start_date", ""), "app_end": a.get("end_date", ""),
                      "app_place": place(a) if a else "", "app_address": app_address(a) if a else "",
                      "app_guessed_from": f"last year {a['_last_year']}" if a.get("_last_year") else "",
                      "days_off": (r["start_date"] - A[j].start).days if j is not None else "",
                      "match_score": res["score"] if res else "", "evidence": "; ".join(res["reasons"]) if res else "",
                      "also_in_app": "; ".join(f"{KIND_LABEL.get(k, k)}: {app[jj]['name']} ({app[jj]['start_date']})" for k, jj in others),
                      "data_flags": "; ".join(flags), "app_dedupe_key": a.get("dedupe_key", ""), "app_listing_url": a.get("listing_url", "")})
        line_at[i] = len(lines) - 1
        checks += check_records(status, bdg, bdesc, means, tags, r, a, res, A[j].start if j is not None else None, now)
        # the other halves of the same event ('Veterans Day Classic: SUNDAY ...' next to '...: SATURDAY ...') get their
        # own badge, unless they sit at a different place (another site of the event, matched by its own organizer row)
        if primary:
            for jj, rr in m[primary]:
                pcj = place_check(app[jj], r, rr)
                if jj == j or isinstance(pcj, str):
                    continue
                offj = (r["start_date"] - A[jj].start).days
                stj, meansj, extraj = status_for(primary, app[jj], r, rr, offj, pcj)
                linked[i].append((jj, stj))
                bj, dj = badge(stj, r, app[jj], rr, offj, pcj)
                checks += check_records(stj, bj, dj, meansj, extraj, r, app[jj], rr, A[jj].start, now, with_org=False)

    site_extra, replaced_by = event_links(scr, app, sgroups, linked, hrows)
    by_org_key = {f"{scr[i]['source']}:{scr[i]['source_event_id']}": x for i, x in site_extra.items()}
    for i, x in site_extra.items():
        lines[line_at[i]].update(last_year_here=last_year_text(x["last_year_venue"]) or x["last_year_venue_status"],
                                 replaces="; ".join(x["replaces"]))

    # sheet events of organizers we scrape that their own site does not list
    scraped_orgs = {canon_org(r.get("organizer_name")) for r in scr}
    used_sheet = {jj for v in matches.values() for jj, _ in v.get("sheet", [])}
    for j, r in enumerate(app):
        if r["_kind"] != "sheet" or j in used_sheet or canon_org(r.get("organizer_name")) not in scraped_orgs: continue
        counts["NOT ON ORGANIZER SITE"] += 1
        bdg, bdesc = badge("NOT ON ORGANIZER SITE", None, r, None, 0, None)
        lines.append({"status": "NOT ON ORGANIZER SITE", "badge": bdg, "badge_description": bdesc, "tags": "",
                      "organizer": r.get("organizer_name"),
                      "details": "In App data(manual-lacrosse-sheet), but the organizer's website doesn't list it (renamed, moved, cancelled "
                                 "or not posted yet). Don't show as confirmed.",
                      "app_match": KIND_LABEL["sheet"], "app_event": r["name"], "app_start": r.get("start_date"),
                      "app_end": r.get("end_date"), "app_place": place(r), "app_address": app_address(r),
                      "app_dedupe_key": r.get("dedupe_key"),
                      "app_listing_url": r.get("listing_url")})
        checks += check_records("NOT ON ORGANIZER SITE", bdg, bdesc, lines[-1]["details"], [], None, r, None, None, now)

    # organizer rows the app still has from earlier runs of a scraper that has since changed their id
    old_lines, old_recs = leftover_records(leftover_rows(export, scr, today), now)
    lines += old_lines; checks += old_recs
    for x in old_lines: counts[x["status"]] += 1

    order = ["CONFIRMED", "DATE CONFIRMED", "PLACE DIFFERS", "DATE MOVED", "DATE CONFLICT", "REVIEW", "NOT ON ORGANIZER SITE",
             "NEW SITE", "NEW", "OLD COPY", "GONE FROM ORGANIZER SITE"]
    lines.sort(key=lambda x: (order.index(x["status"]), str(x.get("organizer") or ""), str(x.get("start") or x.get("app_start"))))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(lines)
    n_ph = sum(1 for r in app if r["_kind"] == "placeholder")
    print(f"organizer events {len(scr)}; app rows {len(app)} ({n_ph} placeholders) from {export}")
    print("  " + ", ".join(f"{k} {counts[k]}" for k in order if counts[k]))
    print(f"wrote {out}")

    # the app's badges: one current check per app row; dry run always written, sending only when asked
    checks = best_per_event(checks)
    for c in checks:
        k = c["app_dedupe_key"]
        if k in replaced_by: c["replaced_by"] = replaced_by[k]
        if k in by_org_key: c.update(by_org_key[k])
    in_app = {json.loads(l).get("dedupe_key") for l in open(export / "event_candidates.jsonl")}
    ready = [c for c in checks if c["app_dedupe_key"] in in_app]
    held = len(checks) - len(ready)
    path = out.with_suffix(".checks.json")
    path.write_text(json.dumps(ready, indent=1, default=str))
    print(f"badge records: {len(ready)} ready ({held} held back: their organizer row isn't in the app yet) -> {path}")
    if args.send_one or args.send_event or args.send:
        if args.send_one:
            batch = [c for c in ready if c["app_dedupe_key"] == args.send_one or c.get("matched_dedupe_key") == args.send_one]
        elif args.send_event:
            batch = [c for c in ready if match_name(c.get("organizer_event_name") or "").lower() == args.send_event.strip().lower()]
        else:
            batch = ready
        if not batch:
            sys.exit(f"no badge record for {args.send_one or args.send_event}")
        res = send_checks(batch) or {}
        if res.get("failed") and os.environ.get("GITHUB_ACTIONS"):
            print(f"::warning::{len(res['failed'])} badge records saved but not applied; the next weekly run retries them")
    return out


STATUS_CODE = {"CONFIRMED": "confirmed", "DATE CONFIRMED": "date_confirmed", "PLACE DIFFERS": "place_differs",
               "DATE MOVED": "date_moved", "DATE CONFLICT": "date_conflict", "REVIEW": "review",
               "NOT ON ORGANIZER SITE": "not_on_organizer_site", "NEW": "new", "NEW SITE": "new"}
STATUS_RANK = ["confirmed", "date_confirmed", "place_differs", "date_moved", "date_conflict", "review", "new", "not_on_organizer_site",
               "superseded"]


def check_records(status, bdg, bdesc, details, tags, r, a, res, app_start, now, with_org=True):
    """The badge records for one report line: one for the app's row and one for the organizer's own wg- row, carrying the
    same badge so both copies of a confirmed event show green. An organizer event the app didn't have gets only its own
    wg- record (🔵 New); a sheet event the organizer doesn't list gets only the app's."""
    code = STATUS_CODE[status]
    org_key = f"{r['source']}:{r['source_event_id']}" if r else None
    city, st = _city_state(r) if r else (None, None)
    base = {"status": code, "badge": bdg, "badge_description": bdesc, "details": details,
            "tags": [t for t in tags if t], "checked_at": now,
            "organizer": (r or a or {}).get("organizer_name"),
            "organizer_event_name": r["name"] if r else None,
            "organizer_start": str(r["start_date"]) if r else None, "organizer_end": str(r.get("end_date") or r["start_date"]) if r else None,
            "organizer_venue": r.get("venue_name") if r else None, "organizer_city": city, "organizer_state": st,
            "organizer_address": (r.get("raw") or {}).get("address") if r else None,
            "organizer_url": r.get("listing_url") if r else None}
    out = []
    if a:
        out.append({**base, "app_dedupe_key": a["dedupe_key"], "organizer_dedupe_key": org_key,
                    "app_start": a.get("start_date"), "guessed_from": str(a["_last_year"]) if a.get("_last_year") else None,
                    "days_off": (r["start_date"] - app_start).days if r and app_start else None,
                    "match_score": res["score"] if res else None})
    if r and with_org:
        rec = {**base, "app_dedupe_key": org_key, "matched_dedupe_key": a["dedupe_key"] if a else None,
               "app_start": str(r["start_date"]), "days_off": (r["start_date"] - app_start).days if a and app_start else None,
               "match_score": res["score"] if res else None}
        if not a:                                    # the app had nothing for it
            rec.update(status="new", badge="🔵 New · organizer website",
                       badge_description="On the organizer's website; the app had no event for it")
        elif code != "review":
            # this row IS the organizer's website, so its own date and place are right whatever the app's copy got wrong
            fixed = {"date_moved": " and corrects the app's date", "date_conflict": " and corrects the app's date",
                     "place_differs": " and corrects the app's place"}.get(code, "")
            rec.update(status="confirmed", badge="🟢 Confirmed · organizer website",
                       badge_description=f"From the organizer's website; matches the app's event{fixed}")
        out.append(rec)
    return out


def best_per_event(checks):
    """One record per app_dedupe_key: when several organizer sites match one app row, the most certain status wins."""
    best = {}
    for c in checks:
        k = c["app_dedupe_key"]
        if k not in best or STATUS_RANK.index(c["status"]) < STATUS_RANK.index(best[k]["status"]):
            best[k] = c
    return list(best.values())


def ingest_settings():
    """INGEST_URL / INGEST_KEY from the environment, else ~/WoodGoodsAI/.ingest.env (never the repo)."""
    env = {k: os.environ[k] for k in ("INGEST_URL", "INGEST_KEY") if os.environ.get(k)}
    f = HOME / ".ingest.env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"'))
    if not env.get("INGEST_KEY"):
        sys.exit(f"INGEST_KEY missing: put INGEST_URL=... and INGEST_KEY=... in {f}")
    return env.get("INGEST_URL", "https://opswg.lovable.app/api/public/ingest"), env["INGEST_KEY"]


def send_checks(batch, size=10):
    """POST {"source": "organizer-check", "confirmations": [...]} in batches; print what the app says it saved. The app
    takes ~4 s per new record (links, copies, hiding; the slow recomputes are queued for its background job), so 10 per
    request stays well inside the request timeout."""
    import urllib.request, urllib.error, ssl
    url, key = ingest_settings()
    try:                                             # this Mac's Python has no system certificates; certifi's bundle works
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    saved, applied, failed, queued = 0, 0, [], 0
    by_key = {c["app_dedupe_key"]: c for c in batch}
    for i in range(0, len(batch), size):
        body = json.dumps({"source": "organizer-check", "confirmations": batch[i:i + size]}, default=str).encode()
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"x-ingest-key": key, "Content-Type": "application/json", "user-agent": "woodgoods-collectors"})
        try:
            with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
                reply = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            sys.exit(f"app refused the badge records: HTTP {e.code} {e.read()[:300]!r}")
        saved += int(reply.get("saved") or 0)
        applied += int(reply.get("applied") or 0)
        failed += [str(e) for e in reply.get("errors") or []]
        queued += len(reply.get("queued") or [])
        print(f"  sent {len(batch[i:i + size])}: saved {reply.get('saved')}, applied {reply.get('applied')}, "
              f"queued {len(reply.get('queued') or [])}, errors {len(reply.get('errors') or [])}")
    # a record the app saved but couldn't apply in time (its 30-second limit) gets one more try on its own
    retry = [by_key[k] for k in dict.fromkeys(str(e).split(": ", 1)[0] for e in failed) if k in by_key] if size > 1 else []
    if retry:
        print(f"  retrying {len(retry)} not applied, one at a time")
        failed = []
        for c in retry:
            r = send_checks([c], size=1)
            applied += r["applied"]; failed += r["failed"]
    # 'saved' = the badge is stored; 'applied' = the app also linked history / copied data / hid placeholders for it
    print(f"badge records saved: {saved} of {len(batch)}; applied: {applied}; queued for the app's background job: {queued}")
    for e in failed: print(f"  NOT APPLIED {e}")
    return {"saved": saved, "applied": applied, "failed": failed}


if __name__ == "__main__":
    main()
