"""Decide whether a current listing is THIS YEAR'S EDITION of a past event.

Used to confirm (or correct the date of) the app's "expected" recurrence placeholders, and to attach
events found on organizer websites to the right series.

Why this is evidence-based instead of a key:
  * Names collide. "Fall Brawl" is run by unrelated organizers in MD, NY, VA, OH and CA.
  * Names change. "2024 Canandaigua Fall Brawl" became "2025 Fall Brawl".
  * Venues change. The MD Fall Brawl moved from South River HS to Bell Branch Park.
  * Busy complexes host several unrelated events on one weekend, so venue + date alone is not proof.

So every pair gets points for each independent piece of evidence (organizer, name, venue, date,
gender, team count) and is only CONFIRMED when the score is high AND at least two independent
anchors agree, one of which must be the organizer or the name. Venue + date alone never confirms.

Pure functions, no I/O. Inputs are plain dicts (see Event below). Tested in tests/test_event_match.py
against real cases from the history.
"""
from __future__ import annotations
import math, re, datetime as dt
from dataclasses import dataclass, field

try:
    from core.dedupe import canon_org as _canon_org
except Exception:  # noqa: BLE001  (lets the module run standalone)
    def _canon_org(name):
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())).strip()

CONFIRM_SCORE = 6.0
REVIEW_SCORE = 3.5
MAX_DAYS = 56          # editions further apart than 8 weeks are never compared

# Words that say nothing about WHICH event it is. "Fall Brawl" has no distinctive words left.
GENERIC = set("""
fall spring summer winter autumn classic brawl shootout showcase tournament tourney invitational invite
lax lacrosse festival fest boys girls youth hs high school annual cup open series championship
championships challenge jam bash sixes 6s the of at and weekend day sat sun saturday sunday event
events league prelim national nationals games grade grades middle ms elite select club vs st nd rd th
site session division divisions varsity jv box indoor outdoor presented by edition new
jan feb mar apr may jun jul aug sep sept oct nov dec
""".split())
# Organizer names are evidence of WHO, not WHICH; strip them from names and score them separately.
ORG_WORDS = set("apex nxt mlt hogans hogan primetime pt victory trilogy adrenaline buku legends ml8 nlf "
                "madlax mdlx alliance aloha pll otl flg".split())
ABBREV = {"pt": "primetime", "prime time": "primetime", "lax": "lacrosse"}
SITE_SUFFIX = re.compile(r"\s*[-–|:]\s*site\s*\d+\s*$|\(\s*site\s*\d+\s*\)", re.I)
GIRLS = re.compile(r"\b(girls?|women'?s|ladies|lady)\b", re.I)
BOYS = re.compile(r"\b(boys?|men'?s)\b", re.I)


@dataclass
class Event:
    name: str
    start: dt.date
    state: str | None = None
    organizer: str | None = None
    sites: set = field(default_factory=set)        # site/venue ids already resolved by the app
    points: list = field(default_factory=list)     # [(lat, lng), ...] for distance when ids differ
    teams: int | None = None
    gender: str | None = None                      # boys | girls | coed | None
    name_series: int = 1                           # how many different series share this exact name

    @classmethod
    def from_dict(cls, d, prefix=""):
        g = lambda k: d.get(prefix + k)
        s = g("date") or g("start")
        return cls(name=g("name") or "", start=dt.date.fromisoformat(s) if isinstance(s, str) else s,
                   state=(g("state") or "").upper() or None, organizer=g("org"),
                   sites=set(g("sites") or []), points=[tuple(p) for p in (g("pt") or []) if p and None not in p],
                   teams=g("teams"), gender=g("gender"), name_series=g("ns") or 1)


def _norm(name):
    n = SITE_SUFFIX.sub("", name or "")
    n = re.sub(r"\b(19|20)\d\d\b", " ", n.lower())
    n = re.sub(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", " ", n)          # 4/26/2026
    for k, v in ABBREV.items():
        n = re.sub(rf"\b{k}\b", v, n)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", n)).strip()


def core_tokens(name):
    """Every meaningful word: used for exact-name comparison of generic names."""
    return {w for w in _norm(name).split() if len(w) > 1 and not w.isdigit()} - {"the", "of", "at", "and", "site"}


def distinctive_tokens(name):
    """Words that identify WHICH event: no generic words, no organizer names."""
    return {w for w in core_tokens(name) if w not in GENERIC and w not in ORG_WORDS and not re.fullmatch(r"\d+(st|nd|rd|th)?", w)}


def gender_of(ev: Event):
    if ev.gender in ("boys", "girls"): return ev.gender
    g, b = bool(GIRLS.search(ev.name)), bool(BOYS.search(ev.name))
    return "girls" if g and not b else "boys" if b and not g else None


def _km(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    h = math.sin((la2 - la1) * p / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def min_km(a: Event, b: Event):
    if not a.points or not b.points: return None
    return min(_km(p, q) for p in a.points for q in b.points)


def score(past: Event, cur: Event):
    """-> dict(score, decision, anchors, reasons). decision: confirmed | review | no_match."""
    reasons, s, anchors = [], 0.0, set()
    if not past.start or not cur.start:
        return {"score": 0, "decision": "no_match", "anchors": [], "reasons": ["missing date"]}
    dd = abs((cur.start - past.start).days)
    if dd > MAX_DAYS:
        return {"score": -9, "decision": "no_match", "anchors": [], "reasons": [f"{dd} days apart"]}

    # organizer: strongest single signal when both sides know it
    po, co = _canon_org(past.organizer), _canon_org(cur.organizer)
    if po and co:
        if po == co: s += 4; anchors.add("organizer"); reasons.append("same organizer +4")
        else: s -= 6; reasons.append(f"different organizer ({po} vs {co}) -6")

    # state: different state only allowed when the organizer matches
    if past.state and cur.state and past.state != cur.state and "organizer" not in anchors:
        return {"score": -9, "decision": "no_match", "anchors": [], "reasons": ["different state"]}

    # venue
    km = min_km(past, cur)
    same_site = bool(past.sites & cur.sites)
    near = same_site or (km is not None and km <= 2)
    if same_site: s += 2.5; reasons.append("same facility +2.5")
    elif km is not None and km <= 2: s += 2; reasons.append(f"{km:.1f} km apart +2")
    elif km is not None and km <= 15: s += 0.5; reasons.append(f"{km:.0f} km apart +0.5")
    elif km is not None and km > 40: s -= 1.5; reasons.append(f"{km:.0f} km apart -1.5")
    if near: anchors.add("venue")

    # name
    pd_, cd_ = distinctive_tokens(past.name), distinctive_tokens(cur.name)
    pc, cc = core_tokens(past.name), core_tokens(cur.name)
    if pd_ and cd_:
        j = len(pd_ & cd_) / len(pd_ | cd_)
        sub = pd_ <= cd_ or cd_ <= pd_
        shared = len(pd_ & cd_)
        # one shared word out of three ("Polar Bear League Charlotte" vs "Charlotte Fall Classic") is a place name,
        # not the same event; a subset only counts when the shared words are most of the longer name
        if j >= 0.6 or (sub and (shared >= 2 or shared / max(len(pd_), len(cd_)) >= 0.5)):
            s += 4; anchors.add("name"); reasons.append(f"distinctive name match ({', '.join(sorted(pd_ & cd_))}) +4")
        elif j >= 0.3:
            s += 2; anchors.add("name"); reasons.append(f"partial name match ({', '.join(sorted(pd_ & cd_))}) +2")
        else:
            s -= 3; reasons.append(f"different names ({' '.join(sorted(pd_))} vs {' '.join(sorted(cd_))}) -3")
    elif pc and cc and (pc == cc or ((pc <= cc or cc <= pc) and len(pc & cc) >= 2)):
        # generic name ("Fall Brawl"): only meaningful when it is rare or the venue matches
        w = 3 if past.name_series <= 1 else 1.5 if past.name_series <= 3 else 0.5
        if near: w = max(w, 3)
        s += w; reasons.append(f"same generic name +{w}")
        if w >= 3: anchors.add("name")
    elif (pd_ or cd_) and not (pd_ & cd_):
        s -= 1.5; reasons.append("names share nothing distinctive -1.5")

    # date
    if dd <= 2: s += 2; reasons.append("same weekend +2")
    elif dd <= 9: s += 1; reasons.append(f"{dd} days apart +1")
    elif dd > 30: s -= 1.5; reasons.append(f"{dd} days apart -1.5")

    # gender
    pg, cg = gender_of(past), gender_of(cur)
    if pg and cg and pg != cg: s -= 5; reasons.append(f"{pg} vs {cg} -5")

    # teams
    if past.teams and cur.teams and min(past.teams, cur.teams) >= 8:
        r = max(past.teams, cur.teams) / min(past.teams, cur.teams)
        if r <= 2: s += 0.5; reasons.append("similar size +0.5")
        elif r >= 4: s -= 1; reasons.append("very different size -1")

    strong = anchors & {"organizer", "name"}
    if s >= CONFIRM_SCORE and len(anchors) >= 2 and strong: decision = "confirmed"
    elif s >= REVIEW_SCORE and strong: decision = "review"
    else: decision = "no_match"
    return {"score": round(s, 2), "decision": decision, "anchors": sorted(anchors), "reasons": reasons,
            "days_apart": dd, "moved": decision != "no_match" and dd > 2}


def site_group_key(ev: Event):
    """'Spring Thaw – Site 1' and '– Site 2' are one edition; group them before assignment."""
    return (_canon_org(ev.organizer), _norm(ev.name), ev.start)


def assign(placeholders, listings, pair_scores):
    """One-to-one: each listing group answers at most one placeholder, best score first.
    pair_scores: {(p_index, l_index): result}. Returns {p_index: (l_indexes, result)}."""
    groups = {}
    for i, l in enumerate(listings): groups.setdefault(site_group_key(l), []).append(i)
    gid = {i: k for k, v in groups.items() for i in v}
    best = {}
    for (pi, li), r in pair_scores.items():
        if r["decision"] == "no_match": continue
        k = (pi, gid[li])
        if k not in best or r["score"] > best[k][1]["score"]: best[k] = (li, r)
    out, used_p, used_g = {}, set(), set()
    for (pi, g), (li, r) in sorted(best.items(), key=lambda kv: -kv[1][1]["score"]):
        if pi in used_p or g in used_g: continue
        used_p.add(pi); used_g.add(g); out[pi] = (groups[g], r)
    return out
