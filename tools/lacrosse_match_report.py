"""Lacrosse match report: what the organizer websites say about each upcoming lacrosse event in the app.

    python tools/lacrosse_match_report.py                        # scrape the 14 organizer sites now
    python tools/lacrosse_match_report.py --scraped rows.jsonl    # reuse rows saved by an earlier run
    python tools/lacrosse_match_report.py --export-dir DIR        # app data (default: ~/WoodGoodsAI/backup/db/fresh, else current)

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
Tags add detail (VENUE DIFFERS, ALSO ON TOURNEY MACHINE, ...) and data_flags lists problems found in the scraped row."""
import sys, os, csv, json, re, argparse, pathlib, datetime as dt
from collections import defaultdict
from dataclasses import replace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dedupe import canon_org, normalize_venue, venue_match, is_compound_venue, level_tokens
from core.model import FULL_STATE
from collectors.event_match import score, core_tokens, CONFIRM_SCORE, ORG_WORDS
from collectors.organizers.match import to_event

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
    """Upcoming lacrosse rows the app shows, by kind. Rows the app already marked duplicate are left out."""
    rows = []
    for line in open(export_dir / "event_candidates.jsonl"):
        r = json.loads(line)
        if not (r.get("sport") or "").lower().startswith("lacrosse") or r.get("status") == "duplicate": continue
        s, e = _date(r.get("start_date")), _date(r.get("end_date"))
        if not s or (e or s) < today: continue
        r["_kind"] = SOURCE_KIND.get(r.get("source"), "other")
        rows.append(r)
    return rows


class Venues:
    """The app's venue table, to give scraped venue names the same ids and coordinates the app's rows carry."""
    def __init__(self, path):
        self.by_state = defaultdict(list)
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
    s0 = _date(r.get("start_date"))
    week = s0 - dt.timedelta(days=s0.weekday()) if s0 else None
    return r["_kind"], canon_org(r.get("organizer_name")), " ".join(sorted(core_tokens(n))), week


def history_starts(dirs, ids):
    """history id -> start date of last year's event, from event_history.jsonl in the first export that has it."""
    out = {}
    for d in dirs:
        p = d / "event_history.jsonl"
        if not p.exists(): continue
        for line in open(p):
            r = json.loads(line)
            if r.get("id") in ids: out[r["id"]] = _date(r.get("start_date"))
        break
    return out


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
    if status == "PLACE DIFFERS":
        return (f"🟠 Place differs · organizer says {place(r)}",
                "Same event and weekend, but the organizer's website lists a different place; the organizer's site is the "
                "real source")
    if status == "DATE MOVED":
        return f"🔴 Date wrong · organizer says {org_dates}", "Organizer might have changed the date"
    if status == "DATE CONFLICT":
        return (f"🔴 Date wrong · organizer says {org_dates}",
                f"{SOURCE_NAME.get(a.get('_kind'), 'The app')} disagrees with the organizer's website; the organizer's "
                f"site is the only real source")
    if status == "REVIEW":
        return "🟡 Possible match, check", review_reason(res, off, pc)
    if status == "NOT ON ORGANIZER SITE":
        src = "App data(manual-lacrosse-sheet)" if a.get("_kind") == "sheet" else "a historical event"
        return "⚪ Not on organizer site", f"Loaded from {src}, not on the organizer's website"
    if status == "NEW SITE":
        return "🔵 New site · organizer website", ("Another venue of an event the app already has; the app doesn't list this "
                                                  "site yet (the weekly run adds it)")
    return "🔵 New · organizer website", "On the organizer's website, not in the app yet (the weekly run adds it)"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export-dir", type=pathlib.Path)
    ap.add_argument("--scraped", type=pathlib.Path, help="jsonl of woodgoods_lacrosse rows from an earlier run")
    ap.add_argument("--out", type=pathlib.Path)
    a = ap.parse_args(argv)
    today = dt.date.today()
    export = a.export_dir or next(p for p in (HOME / "backup/db/fresh", HOME / "backup/db/current") if (p / "event_candidates.jsonl").exists())
    out = a.out or HOME / "reports" / f"lacrosse_match_{today}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    scr = scraped_rows(a.scraped, today)
    if not a.scraped:                                   # keep what was scraped so a rerun can skip the 5-minute crawl
        with open(out.with_suffix(".scraped.jsonl"), "w") as f:
            for r in scr: f.write(json.dumps(r, default=str) + "\n")
    app = load_app(export, today)
    venues = Venues(export / "venues.jsonl")
    for r in scr:
        city, st = _city_state(r)
        vid, pt, vst = venues.resolve(r.get("venue_name"), city, st)
        r["venue_ids"], r["venues"] = ([vid] if vid else []), ([{"lat": pt[0], "long": pt[1]}] if pt else [])
        if vst and not st: r["state"], r["_state_from_venue"] = vst, True
    for r in app:                                       # placeholders carry no organizer; their names often do
        if not r.get("organizer_name") and org_from_name(r.get("name")): r["organizer_name"] = org_from_name(r["name"])
    hist = history_starts([export, HOME / "backup/db/current"],
                          {(r.get("raw") or {}).get("history_id") for r in app if r["_kind"] == "placeholder"} - {None})
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

    cols = ["status", "badge", "badge_description", "tags", "details", "organizer", "event", "start", "end", "place", "organizer_url",
            "app_match", "app_event", "app_start", "app_end", "app_place", "app_guessed_from", "days_off", "match_score",
            "evidence", "also_in_app", "data_flags", "app_dedupe_key", "app_listing_url"]
    lines, counts = [], defaultdict(int)
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
            label = KIND_LABEL.get(primary, primary)
            if res["decision"] != "confirmed":
                status, means = "REVIEW", f"Possible match with {label}; the evidence is weak, check by hand."
            elif abs(off) <= 2 and pc is True:
                status = "CONFIRMED"                     # same event, same weekend, same place: everything matches
                means = {"placeholder": "The organizer site lists it the same weekend at the same place: the app's placeholder "
                                        "(guessed from last year) is real. Safe to show as confirmed.",
                         "tm": "Also listed on Tourney Machine; both agree on the weekend and the place.",
                         "sheet": "Matches App data(manual-lacrosse-sheet); the organizer site agrees on the weekend and the "
                                  "place."}.get(primary, "Matches an app row.")
            elif abs(off) <= 2 and pc:
                status = "PLACE DIFFERS"
                means = (f"Same event and weekend, but {label} has {place(app[j]) or 'another place'} and the organizer site says "
                         f"{place(r)} ({pc}). Show the organizer's place.")
            elif abs(off) <= 2:
                status = "DATE CONFIRMED"
                means = (f"The organizer site confirms the weekend, but one side gives no town ({place(app[j]) or 'nothing'} vs "
                         f"{place(r) or 'nothing'}), so the place could not be checked.")
            elif primary == "placeholder" and res["alt"] and abs((r["start_date"] - app[j]["_alt_start"]).days) <= 2:
                status = "DATE MOVED"
                tags.append("APP DATE A WEEK LATE (week-number rule)")
                means = (f"The app shows {app[j]['start_date']}: last year's date ({app[j]['_last_year']}) + 371 days, by week "
                         f"number. The organizer site says {r['start_date']}, the same weekend as last year. The app's date is "
                         f"wrong; show the organizer's.")
            elif primary == "placeholder":
                status = "DATE MOVED"
                tags.append("ORGANIZER CHANGED THE WEEKEND")
                means = (f"The app guessed {app[j]['start_date']} from last year's Tourney Machine event; the organizer site says "
                         f"{r['start_date']}. The guessed date is wrong; show the organizer's.")
            else:
                status = "DATE CONFLICT"
                means = (f"{label[0].upper() + label[1:]} says {app[j]['start_date']}, the organizer site says {r['start_date']}. "
                         f"Don't show either as certain until checked.")
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
                      "organizer_url": r.get("listing_url"), "app_match": KIND_LABEL.get(primary, primary) if primary else "",
                      "app_event": a.get("name", ""), "app_start": a.get("start_date", ""), "app_end": a.get("end_date", ""),
                      "app_place": place(a) if a else "",
                      "app_guessed_from": f"last year {a['_last_year']}" if a.get("_last_year") else "",
                      "days_off": (r["start_date"] - A[j].start).days if j is not None else "",
                      "match_score": res["score"] if res else "", "evidence": "; ".join(res["reasons"]) if res else "",
                      "also_in_app": "; ".join(f"{KIND_LABEL.get(k, k)}: {app[jj]['name']} ({app[jj]['start_date']})" for k, jj in others),
                      "data_flags": "; ".join(flags), "app_dedupe_key": a.get("dedupe_key", ""), "app_listing_url": a.get("listing_url", "")})

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
                      "app_end": r.get("end_date"), "app_place": place(r), "app_dedupe_key": r.get("dedupe_key"),
                      "app_listing_url": r.get("listing_url")})

    order = ["CONFIRMED", "DATE CONFIRMED", "PLACE DIFFERS", "DATE MOVED", "DATE CONFLICT", "REVIEW", "NOT ON ORGANIZER SITE",
             "NEW SITE", "NEW"]
    lines.sort(key=lambda x: (order.index(x["status"]), str(x.get("organizer") or ""), str(x.get("start") or x.get("app_start"))))
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(lines)
    n_ph = sum(1 for r in app if r["_kind"] == "placeholder")
    print(f"organizer events {len(scr)}; app rows {len(app)} ({n_ph} placeholders) from {export}")
    print("  " + ", ".join(f"{k} {counts[k]}" for k in order if counts[k]))
    print(f"wrote {out}")
    return out


if __name__ == "__main__":
    main()
