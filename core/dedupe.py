"""Cross-source dedupe, keyed on LOCATION + DATE + ORGANIZER.

The unit of record is an EVENT-SITE, not an event. One venue, one weekend,
one vendor fee, one operator. An organizer running three venues on one
weekend produces three rows, because that is three bookings.

Matching priority, strongest first:

    1. organizer   (canonicalized across naming variants)
    2. date        (overlap, +/- 1 day)
    3. venue       (normalized; the discriminator that splits sites)
    4. city/state  (fallback when a source omits the venue)
    5. name        (weak - tiebreaker only, never the primary key)

Name is deliberately demoted. Two events at the same venue on the same
weekend from the same organizer are the same booking whatever they are
called; and 'Fall Classic' appears ten times in one season across four
organizers, so name similarity is close to noise.

SITE-SET RECONCILIATION
-----------------------
Sources disagree about how many venues an event runs. Tourney Machine may
list two sites while the organizer site lists three. The missing site is a
bookable weekend nobody is staffing, so every row in that event group gets
flagged `site_set_mismatch` with the detail of who reported what.
"""
from __future__ import annotations
import re, hashlib, datetime as dt
from collections import defaultdict

# ------------------------------------------------------------------ organizer

ORGANIZER_ALIASES = {
    "nxt": "nxt", "nxt sports": "nxt", "nxt lacrosse": "nxt",
    "mlt": "mlt", "my lacrosse tournaments": "mlt",
    "pll": "pll", "pll play": "pll", "premier lacrosse league": "pll",
    "hogans": "hogans", "hogan s": "hogans", "hoganlax": "hogans",
    "hogan lacrosse": "hogans",
    "primetime": "primetime", "prime time lacrosse": "primetime",
    "primetime lacrosse": "primetime",
    "ptlacrosse": "ptlacrosse", "pt lacrosse": "ptlacrosse",
    "victory": "victory", "victory event series": "victory",
    "victory events": "victory",
    "nlf": "nlf", "national lacrosse federation": "nlf",
    "madlax": "madlax", "mdlx events": "madlax", "mdlx": "madlax",
    "alliance": "alliance", "alliance lacrosse league": "alliance",
    "adrenaline": "adrenaline", "adrenaline lacrosse": "adrenaline",
    "aloha": "aloha", "aloha tournaments": "aloha",
    "apex": "apex", "apex lacrosse events": "apex",
    "buku": "buku", "buku events": "buku",
    "trilogy": "trilogy", "trilogy lacrosse": "trilogy",
    "legends": "legends", "legends lacrosse": "legends",
    "ml8": "ml8", "ml8 events": "ml8",
    "battle at lax": "battleatlax", "battleatlax": "battleatlax",
}


def canon_org(name):
    n = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    n = re.sub(r"\s+", " ", n).strip()
    return ORGANIZER_ALIASES.get(n, n)


# --------------------------------------------------------------------- venue

_FACILITY_WORDS = {
    "sports", "sport", "athletic", "athletics", "complex", "park", "parks",
    "campus", "center", "centre", "arena", "field", "fields", "training",
    "institute", "club", "turf", "regional", "county", "recreation", "rec",
    "university", "college", "school", "high", "hs", "academy", "the", "of",
    "soccer", "equestrian", "sportsplex", "fieldhouse", "facility", "and",
    "at", "state", "memorial", "municipal", "community",
}

_MULTI_SITE_MARKERS = re.compile(
    r"multiple locations|various|^tbd|see field assignments|multi[- ]site", re.I)

# 'Farmingdale State College & Destination KP' is TWO venues crammed into one
# field by the source. It is not a venue and must not subset-match either of
# its halves - that would merge two distinct sites.
_COMPOUND_VENUE = re.compile(r"\s(?:&|and|/|\+)\s", re.I)


def is_compound_venue(v):
    return bool(v) and bool(_COMPOUND_VENUE.search(v))


def normalize_venue(v):
    """Reduce a venue string to its distinctive words.

    'United Sports Training Center' and 'United Sports Complex' must both
    reduce to 'united' - sources name the same facility differently and an
    inconsistent reduction silently blocks real merges.

    Placeholders ('Multiple Locations', 'TBD') return '' - they are the
    ABSENCE of a venue, and treating them as one would merge distinct sites.
    """
    if not v:
        return ""
    if _MULTI_SITE_MARKERS.search(v.strip()):
        return ""
    v = re.sub(r"\s*[-\u2013]\s*site\s*\d+\s*$", " ", v, flags=re.I)
    v = re.sub(r"[^a-z0-9 ]", " ", v.lower())
    toks = [t for t in v.split() if t and t not in _FACILITY_WORDS]
    return " ".join(toks)


def venue_match(v1, v2):
    """True / False / None (unknown - at least one side has no venue)."""
    if is_compound_venue(v1) != is_compound_venue(v2):
        # one side lists a multi-venue string, the other a single venue:
        # they describe different scopes, so this is a site-set problem,
        # not a match. Flagged downstream rather than silently merged.
        return False
    n1, n2 = normalize_venue(v1), normalize_venue(v2)
    if not n1 or not n2:
        return None
    if n1 == n2:
        return True
    s1, s2 = set(n1.split()), set(n2.split())
    if not s1 or not s2:
        return None
    if s1 <= s2 or s2 <= s1:
        return True
    return len(s1 & s2) / len(s1 | s2) >= 0.6


def city_match(c1, c2):
    a, b = (c1 or "").lower().strip(), (c2 or "").lower().strip()
    if not a or not b:
        return None
    return a == b or a in b or b in a


# ---------------------------------------------------------------------- name

_NOISE_WORDS = {
    "the", "a", "an", "of", "at", "in", "and", "for", "on",
    "lacrosse", "lax", "tournament", "tourney", "event", "events",
    "annual", "presented", "by", "series",
}
_GENERIC_EVENT_WORDS = {
    "invitational", "classic", "cup", "bash", "brawl", "jam", "showdown",
    "shootout", "challenge", "championship", "championships", "festival",
    "games", "game", "slam", "kickoff", "open", "clash", "blitz", "madness",
    "finals", "final", "session", "sessions", "round", "fest", "tilt",
    "rumble", "showcase", "summit", "league", "day", "weekend",
    "fall", "spring", "summer", "winter", "autumn",
}
_GENDER_TOKENS = {"boys", "girls", "coed", "mens", "womens", "men", "women"}
_LEVEL_TOKENS = {"youth", "hs", "highschool", "high", "school", "middle",
                 "ms", "varsity", "jv", "adult", "college"}
_LEVEL_ALIAS = {"highschool": "hs", "high": "hs", "school": "", "ms": "middle"}

_STRIP_PATTERNS = [
    r"^\s*20\d{2}\s+",
    r"\b\d{1,2}(?:st|nd|rd|th)\s+annual\b",
    r"\s*[-\u2013]\s*site\s*\d+\s*$",
    r"\s*\((?:boys|girls|coed|men|women)\)\s*",
    r"\bpowered by .*$",
    r"\bpresented by .*$",
    r"\s*\|\s*.*$",
]


def normalize_name(name):
    n = (name or "").lower()
    for pat in _STRIP_PATTERNS:
        n = re.sub(pat, " ", n, flags=re.I)
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def name_tokens(name):
    return set(normalize_name(name).split()) - _NOISE_WORDS - _GENDER_TOKENS


def identity_tokens(name):
    return name_tokens(name) - _LEVEL_TOKENS - _GENERIC_EVENT_WORDS


def level_tokens(name):
    out = set()
    for t in name_tokens(name) & _LEVEL_TOKENS:
        t = _LEVEL_ALIAS.get(t, t)
        if t:
            out.add(t)
    return out


def jaccard(a, b):
    return len(a & b) / len(a | b) if (a and b) else 0.0


# ---------------------------------------------------------------------- date

DATE_TOLERANCE = 1


def _d(row, key):
    v = row.get(key)
    if isinstance(v, str):
        try:
            return dt.date.fromisoformat(v)
        except ValueError:
            return None
    return v


def dates_overlap(a, b, tol=DATE_TOLERANCE):
    a1, a2 = a
    b1, b2 = b
    if not a1 or not b1:
        return False
    a2, b2 = a2 or a1, b2 or b1
    return (a1 - dt.timedelta(days=tol)) <= b2 and (b1 - dt.timedelta(days=tol)) <= a2


# ------------------------------------------------------------------ matching

def same_site(r1, r2):
    """Are these two rows the same EVENT-SITE (one venue, one weekend)?

    Returns (bool, reason). Location-primary: organizer and date must agree,
    then venue decides. Name is consulted only when neither venue nor city
    is available.
    """
    if (r1.get("sport") or "lacrosse") != (r2.get("sport") or "lacrosse"):
        return False, "different_sport"

    if canon_org(r1.get("organizer")) != canon_org(r2.get("organizer")):
        return False, "different_organizer"

    if not dates_overlap((_d(r1, "start_date"), _d(r1, "end_date")),
                         (_d(r2, "start_date"), _d(r2, "end_date"))):
        return False, "dates_disjoint"

    st1, st2 = (r1.get("state") or "").upper(), (r2.get("state") or "").upper()
    if st1 and st2 and st1 != st2:
        return False, "different_state"

    # Gender-split weekends are separate bookings (girls Sat, boys Sun).
    g1, g2 = r1.get("gender"), r2.get("gender")
    if g1 and g2 and "unknown" not in (g1, g2) and g1 != g2:
        if not (g1 == "coed" or g2 == "coed"):
            return False, "different_gender"

    vm = venue_match(r1.get("venue"), r2.get("venue"))

    if vm is True:
        return _same_location_verdict(r1, r2, "venue_match")

    if vm is False:
        # Different venues, same organizer and weekend = MULTI-SITE.
        # Three venues is three bookings. Never merge.
        return False, "multi_site_distinct_venue"

    # --- venue unknown on at least one side; fall back to city
    cm = city_match(r1.get("city"), r2.get("city"))
    if cm is True:
        return _same_location_verdict(r1, r2, "city_match_venue_unknown")
    if cm is False:
        return False, "different_city"

    # --- no venue, no city. Name is the last resort, and a weak one.
    ok, reason = _same_location_verdict(r1, r2, "name_match_no_location")
    if not ok:
        return False, reason
    i1, i2 = identity_tokens(r1.get("name")), identity_tokens(r2.get("name"))
    if i1 and i2 and (i1 & i2):
        return True, "name_match_no_location"
    if jaccard(name_tokens(r1.get("name")), name_tokens(r2.get("name"))) >= 0.85:
        return True, "name_match_no_location"
    return False, "no_location_no_name_match"


def _same_location_verdict(r1, r2, base_reason):
    """Two rows share a location and a weekend. Are they one booking?

    Name is a weak signal for MERGING but a strong one for SPLITTING:
    organizers rarely run two events at one venue on one weekend, and when
    they do the names differ clearly ('Southern Aces' and 'Lucky Draw' at
    River City on the same Saturday are two bookings).
    """
    l1, l2 = level_tokens(r1.get("name")), level_tokens(r2.get("name"))
    if (l1 or l2) and not (l1 & l2):
        return False, f"{base_reason}_but_different_level"

    e1, e2 = r1.get("event_type"), r2.get("event_type")
    if e1 and e2 and e1 != e2:
        return False, f"{base_reason}_but_different_event_type"

    i1, i2 = identity_tokens(r1.get("name")), identity_tokens(r2.get("name"))
    if i1 and i2 and not (i1 & i2):
        # both names carry a distinctive identity and they share none
        return False, f"{base_reason}_but_different_event"

    return True, base_reason


# ------------------------------------------------------- event-group grouping

def event_group_key(row):
    """Identifies the EVENT across its sites, not the site.

    Used only for site-set reconciliation - never for merging. Keyed on
    organizer + week + event identity, deliberately ignoring venue.
    """
    d = _d(row, "start_date")
    if not d:
        return None
    week = (d - dt.timedelta(days=d.weekday())).isoformat()
    ident = " ".join(sorted(identity_tokens(row.get("name")))) or \
            " ".join(sorted(name_tokens(row.get("name"))))
    raw = f"{row.get('sport') or 'lacrosse'}|{canon_org(row.get('organizer'))}|{week}|{ident}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def reconcile_site_sets(rows):
    """Flag events where sources disagree about which venues are in play.

    If Tourney Machine lists two sites and the organizer site lists three,
    the third is a bookable weekend nobody is staffing. Every row in the
    group is flagged and the detail records who reported what.
    """
    groups = defaultdict(list)
    for r in rows:
        k = event_group_key(r)
        if k:
            groups[k].append(r)

    reports = []
    for gkey, grp in groups.items():
        sites = {normalize_venue(x.get("venue")) or "(unspecified)" for x in grp}
        for r in grp:
            r["event_group"] = gkey
            r["site_count_in_group"] = len(sites)

        by_source = defaultdict(set)
        for r in grp:
            for s in (r.get("sources") or [{"source_type": r.get("source_type"),
                                            "venue_as_published": r.get("venue")}]):
                src = s.get("source_type") or s.get("collector") or "unknown"
                by_source[src].add(
                    normalize_venue(s.get("venue_as_published") or r.get("venue"))
                    or "(unspecified)")

        if len(by_source) < 2:
            continue
        sets = list(by_source.values())
        if any(s != sets[0] for s in sets[1:]):
            union = set().union(*sets)
            detail = {src: sorted(v) for src, v in by_source.items()}
            missing = {src: sorted(union - v) for src, v in by_source.items()
                       if union - v}
            for r in grp:
                flags = r.get("flags")
                flags = list(flags) if isinstance(flags, list) else \
                    [x for x in (flags or "").split(",") if x]
                if "site_set_mismatch" not in flags:
                    flags.append("site_set_mismatch")
                r["flags"] = flags
                r["site_set_detail"] = detail
            reports.append({
                "event_group": gkey,
                "organizer": grp[0].get("organizer"),
                "name": grp[0].get("name"),
                "start_date": grp[0].get("start_date"),
                "by_source": detail,
                "missing_from": missing,
            })
    return reports


# ----------------------------------------------------------------- clustering

SOURCE_PRIORITY = {"organizer_site": 2, "tourney_machine": 1}


def _rank(row):
    return SOURCE_PRIORITY.get(row.get("source_type", "organizer_site"), 0)


def merge_rows(rows):
    rows = sorted(rows, key=_rank, reverse=True)
    base = dict(rows[0])
    base["sources"], seen = [], set()
    for r in rows:
        tag = f"{r.get('collector') or r.get('source_type')}:{r.get('fingerprint','')}"
        if tag not in seen:
            seen.add(tag)
            base["sources"].append({
                "collector": r.get("collector"),
                "source_type": r.get("source_type"),
                "organizer": r.get("organizer"),
                "fingerprint": r.get("fingerprint"),
                "source_url": r.get("source_url"),
                "name_as_published": r.get("name"),
                "venue_as_published": r.get("venue"),
                "team_count": r.get("team_count"),
            })
        for k, v in r.items():
            if k in ("sources", "flags", "site_set_detail"):
                continue
            if not base.get(k) and v:
                base[k] = v

    starts = [d for d in (_d(r, "start_date") for r in rows) if d]
    ends = [d for d in (_d(r, "end_date") for r in rows) if d]
    if starts:
        base["start_date"] = min(starts).isoformat()
    if ends:
        base["end_date"] = max(ends).isoformat()

    counts = [r.get("team_count") for r in rows if r.get("team_count")]
    if counts:
        base["team_count"] = counts[0]
        if len({str(c) for c in counts}) > 1:
            base["team_count_variants"] = sorted({str(c) for c in counts})

    flags = set()
    for r in rows:
        f = r.get("flags")
        flags.update(f if isinstance(f, list) else
                     [x for x in (f or "").split(",") if x])
    if len(base["sources"]) > 1:
        flags.add("corroborated")
    base["flags"] = sorted(flags)
    base["source_count"] = len(base["sources"])
    return base


def blocking_key(row):
    d = _d(row, "start_date")
    return f"{canon_org(row.get('organizer'))}|{d.strftime('%Y-%m') if d else 'nodate'}"


def cluster(rows, explain=False):
    """Group rows into canonical EVENT-SITES, then reconcile site sets.

    Returns (merged_rows, decisions, site_reports).
    """
    buckets = defaultdict(list)
    for r in rows:
        buckets[blocking_key(r)].append(r)

    merged, decisions = [], []
    for bucket in buckets.values():
        clusters = []
        for row in bucket:
            placed = False
            for cl in clusters:
                ok, reason = same_site(cl[0], row)
                if explain:
                    decisions.append({
                        "a": cl[0].get("name"), "a_venue": cl[0].get("venue"),
                        "b": row.get("name"), "b_venue": row.get("venue"),
                        "merged": ok, "reason": reason})
                if ok:
                    cl.append(row)
                    placed = True
                    break
            if not placed:
                clusters.append([row])
        merged.extend(merge_rows(cl) for cl in clusters)

    site_reports = reconcile_site_sets(merged)
    return merged, decisions, site_reports
