"""Canonical event record + normalization helpers."""
from __future__ import annotations
import re, hashlib, datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Optional

MONTHS = ("january february march april may june july august september october "
          "november december").split()
ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS)}
ABBR.update({m: i + 1 for i, m in enumerate(MONTHS)})
ABBR.update({m[:4]: i + 1 for i, m in enumerate(MONTHS)})
ABBR["sept"] = 9

STATES = {
 'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS',
 'KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY',
 'NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV',
 'WI','WY','DC'}

FULL_STATE = {
 'alabama':'AL','alaska':'AK','arizona':'AZ','arkansas':'AR','california':'CA',
 'colorado':'CO','connecticut':'CT','delaware':'DE','florida':'FL','georgia':'GA',
 'hawaii':'HI','idaho':'ID','illinois':'IL','indiana':'IN','iowa':'IA','kansas':'KS',
 'kentucky':'KY','louisiana':'LA','maine':'ME','maryland':'MD','massachusetts':'MA',
 'michigan':'MI','minnesota':'MN','mississippi':'MS','missouri':'MO','montana':'MT',
 'nebraska':'NE','nevada':'NV','new hampshire':'NH','new jersey':'NJ',
 'new mexico':'NM','new york':'NY','north carolina':'NC','north dakota':'ND',
 'ohio':'OH','oklahoma':'OK','oregon':'OR','pennsylvania':'PA','rhode island':'RI',
 'south carolina':'SC','south dakota':'SD','tennessee':'TN','texas':'TX','utah':'UT',
 'vermont':'VT','virginia':'VA','washington':'WA','west virginia':'WV',
 'wisconsin':'WI','wyoming':'WY'}

REGIONS = {
 **{s: "Mid-Atlantic"   for s in ("DE","MD","VA")},
 **{s: "NJ Centralized" for s in ("NY","NJ","PA")},
 **{s: "New England"    for s in ("CT","MA","ME","NH","RI","VT")},
 **{s: "South"          for s in ("AL","FL","GA","NC","SC","TN","WV","KY","AR","MS","LA")},
 **{s: "CO/UT"          for s in ("CO","UT")},
 "TX": "Texas", "CA": "California",
}

GENDER_PATTERNS = [
 (r"co-?ed|boys?\s*(?:,|and|&|\+)\s*girls?|girls?\s*(?:,|and|&|\+)\s*boys?", "coed"),
 (r"\bgirls?\b|\bwomen", "girls"),
 (r"\bboys?\b|\bmen\b", "boys"),
]


def region_for(state):
    return REGIONS.get((state or "").upper(), "Other")


def normalize_gender(text):
    t = (text or "").lower()
    for pat, val in GENDER_PATTERNS:
        if re.search(pat, t):
            return val
    return "unknown"


def split_city_state(text):
    """'Farmingdale State College, NY' -> ('Farmingdale State College', 'NY')"""
    if not text:
        return None, None
    parts = [p.strip() for p in str(text).split(",")]
    tail = parts[-1] if parts else ""
    tok = tail.split()[0].upper() if tail.split() else ""
    if tok in STATES:
        return (", ".join(parts[:-1]).strip() or None), tok
    if tail.lower() in FULL_STATE:
        return (", ".join(parts[:-1]).strip() or None), FULL_STATE[tail.lower()]
    for p in reversed(parts):
        if p.upper() in STATES:
            return (", ".join(x for x in parts if x != p).strip() or None), p.upper()
        if p.lower() in FULL_STATE:
            return (", ".join(x for x in parts if x != p).strip() or None), FULL_STATE[p.lower()]
    return str(text).strip() or None, None


_ORD = re.compile(r"(\d+)(st|nd|rd|th)\b", re.I)


def parse_date_range(text, default_year=None):
    """Parse the messy date strings these sites publish.

    Handles 'Oct. 10th-11th, 2026', 'October 18th, 2026', 'Nov 21 - 22, 2026',
    'Jun 29-Jul 1, 2026', 'October 31 & November 1, 2026', 'Jul 27-Aug 2, 2026'.
    Returns (start_date, end_date) as datetime.date, or (None, None).
    """
    if not text:
        return None, None
    t = _ORD.sub(r"\1", str(text))
    t = t.replace("\u2013", "-").replace("\u2014", "-").replace("&", "-")
    t = re.sub(r"[.,]", " ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()

    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", t)]
    year_a = years[0] if years else default_year
    year_b = years[-1] if years else year_a
    if year_a is None:
        return None, None

    found = []
    for m in re.finditer(r"\b([a-z]{3,9})\b", t):
        w = m.group(1)
        mo = ABBR.get(w) or ABBR.get(w[:4]) or ABBR.get(w[:3])
        if mo:
            found.append((m.start(), mo))
    if not found:
        return None, None

    nums = [(m.start(), int(m.group())) for m in re.finditer(r"\b(\d{1,2})\b", t)]
    if not nums:
        return None, None

    def days_after(pos):
        return [n for p, n in nums if p > pos and 1 <= n <= 31]

    m1 = found[0][1]
    d1_list = days_after(found[0][0])
    if not d1_list:
        return None, None
    d1 = d1_list[0]

    if len(found) > 1:
        m2 = found[1][1]
        after = days_after(found[1][0])
        d2 = after[0] if after else d1
        y2 = year_b if m2 < m1 else year_a
    else:
        m2 = m1
        d2 = d1_list[1] if len(d1_list) > 1 else d1
        y2 = year_a

    try:
        start = dt.date(year_a, m1, d1)
        end = dt.date(y2, m2, d2)
    except ValueError:
        return None, None
    if end < start:
        end = start
    return start, end


def weekend_index(d, season_start):
    if not d:
        return None
    idx = (d - season_start).days // 7 + 1
    return str(idx) if idx >= 1 else "0"


def touches_weekend(start, end):
    """True if the event covers a Saturday or Sunday."""
    if not start:
        return True
    end = end or start
    cur = start
    while cur <= end:
        if cur.weekday() >= 5:
            return True
        cur += dt.timedelta(days=1)
    return False


@dataclass
class Event:
    organizer: str
    name: str
    sport: str = "lacrosse"   # every collector in this repo is lacrosse-only
    start_date: Optional[dt.date] = None
    end_date: Optional[dt.date] = None
    raw_date: str = ""
    venue: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    region: str = "Other"
    gender: str = "unknown"
    age_range: Optional[str] = None
    team_count: Optional[str] = None
    vendor_fee: Optional[str] = None
    event_type: str = "tournament"
    source_url: Optional[str] = None
    collector: str = ""
    fingerprint: str = field(default="", init=False)

    def __post_init__(self):
        if self.state:
            self.region = region_for(self.state)
        key = "|".join([
            (self.sport or "lacrosse").lower().strip(),
            (self.organizer or "").lower().strip(),
            re.sub(r"[^a-z0-9]", "", (self.name or "").lower()),
            self.start_date.isoformat() if self.start_date else (self.raw_date or "").lower(),
            (self.venue or "").lower().strip(),
        ])
        self.fingerprint = hashlib.sha1(key.encode()).hexdigest()[:16]

    def to_row(self):
        d = asdict(self)
        d["fingerprint"] = self.fingerprint
        for k in ("start_date", "end_date"):
            d[k] = d[k].isoformat() if d[k] else None
        return d
