"""Field extractors for organizer websites: pure functions on page text/HTML, no network.

Per event:     dates, city/state, venue name + street address, age groups (grad years), gender, team count, times.
Per organizer: emails, phones, vendor contact (name/email/phone near "vendor"/"sponsor" wording).
Dates and age groups reuse collectors.common so every source parses them the same way."""
from __future__ import annotations
import re, json, datetime as dt

from .. import common

STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
          "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
          "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
          "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
          "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
          "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
          "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
          "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}
STATE_BY_NAME = {v.lower(): k for k, v in STATES.items()}
_ST = "|".join(STATES)
_STNAME = "|".join(sorted((re.escape(v) for v in STATES.values()), key=len, reverse=True))

MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
WEEKDAY = re.compile(r"\b(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|rsday|urday)?\b\.?,?\s*", re.I)
ORDINAL = re.compile(r"(\d)(?:st|nd|rd|th)\b", re.I)
DASH = re.compile(r"\s*[–—\-&/]\s*|\s+(?:to|thru|through|and)\s+", re.I)   # "to"/"and" only as words: never inside "October"
# "Nov 7-8, 2026" | "November 7 – November 8, 2026" | "Nov 7, 2026" | "Dec 30, 2026 - Jan 2, 2027" | "November 14-15"
# separators: "7-8", "7 – 8", "7 to 8", "5th & 6th", "14th / 15th", "26th and 27th"
DATE_TEXT = re.compile(rf"\b{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s*\d{{4}})?(?:\s*(?:–|—|-|&|/|\bto\b|\bthru\b|\bthrough\b|\band\b)\s*(?:{MONTH}\s+)?\d{{1,2}}(?:st|nd|rd|th)?)?(?:,?\s*\d{{4}})?", re.I)
NUM_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?:\s*(?:–|—|-|to)\s*(?:(\d{1,2})/)?(\d{1,2})(?:/(\d{2,4}))?)?\b")

CITY_ST = re.compile(rf"\b([A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){{0,3}}),\s*({_ST}|{_STNAME})\b(?:\s+\d{{5}})?")
STREET = (r"(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Pike|Highway|Hwy|Parkway|Pkwy|Court|Ct|Circle|Cir|"
          r"Place|Pl|Trail|Trl|Turnpike|Tpke|Route|Rte|Terrace|Ter|Square|Sq|Run|Crossing|Xing)")
ADDRESS = re.compile(rf"\b\d{{2,6}}\s+(?:[NSEW]\.?\s+)?[A-Z][\w.'’\-]*(?:\s+(?!\d+\b)[A-Za-z0-9][\w.'’\-]*){{0,3}}\s+{STREET}\b\.?"
                     rf"(?:\s*(?:,|#|Suite|Ste\.?)\s*[\w #.\-]{{0,20}})?,?\s+[A-Z][A-Za-z.'’\- ]{{1,30}},?\s*(?:{_ST})\b\.?(?:\s+\d{{5}}(?:-\d{{4}})?)?")
VENUE_LABEL = re.compile(r"\b(?:location|venue|facility|facilities|site|fields?|complex|where|held at|played at|hosted at)\s*[:\-–]\s*([^\n|•]{3,90})", re.I)
TEAMS = re.compile(r"\b(?:(?:limited|capped)\s+to\s+|team\s+cap\s*(?:of|:)?\s*|max(?:imum)?\s*(?:of)?\s*)?(\d{2,3})\s*(?:\+\s*)?(?:-\s*)?teams?\b", re.I)
TEAM_CAP = re.compile(r"\b(?:team\s+cap|teams?\s+max(?:imum)?|max(?:imum)?\s+teams?)\s*(?:of|:|is)?\s*(\d{2,3})\b", re.I)
TIME = re.compile(r"\b(1[0-2]|0?[1-9])(?::([0-5]\d))?\s*([ap])\.?\s*m\.?\b", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?([2-9]\d{2})[\s.\-]?(\d{4})(?!\d)")
VENDOR_WORDS = re.compile(r"\b(vendors?|vending|sponsors?|sponsorship|exhibitors?|booths?|merchandise|retail)\b", re.I)
NAME = r"[A-Z][a-z]+(?:\s+[A-Z]\.)?\s+[A-Z][a-zA-Z'’\-]+"
VENDOR_PERSON = re.compile(rf"(?:(?:vendor|sponsor(?:ship)?|exhibitor)s?\s+(?:coordinator|contact|manager|director|inquiries|questions)[^A-Za-z]{{0,6}}({NAME}))"
                           rf"|(?:({NAME})\s*[,\-–(]\s*(?:vendor|sponsor(?:ship)?|exhibitor)s?\s+(?:coordinator|contact|manager|director))", re.I)
JUNK_EMAIL = re.compile(r"(\.png|\.jpg|\.gif|\.webp|\.svg|example\.(com|org)|sentry|wixpress|godaddy|domain\.com|@2x)", re.I)


def clean(text):
    return re.sub(r"\s+", " ", (text or "").replace("​", " ")).strip()


def _norm_date_text(s):
    s = re.sub(MONTH, lambda m: m.group(0).capitalize(), s, flags=re.I)    # "MAY 9" -> "May 9": the shared parser is case-sensitive
    s = WEEKDAY.sub("", s)
    s = ORDINAL.sub(r"\1", s)
    s = re.sub(r"(\d{1,2})\s+(20\d\d)\b", r"\1, \2", s)          # "January 10 2027" -> "January 10, 2027"
    s = re.sub(r"(\d),(20\d\d)\b", r"\1, \2", s)                  # "May 30 - 31,2026" -> "May 30 - 31, 2026"
    return DASH.sub(" - ", s).replace(".", "")


def find_dates(text, today=None):
    """All (start, end) date ranges in the text, in order. Years default to the next occurrence after today."""
    today = today or dt.date.today()
    out = []
    t = WEEKDAY.sub("", text or "")
    t = re.sub(r"(?:\s*[-–—]\s*){2,}", " - ", t)        # "January 9 - - January 10" -> "January 9 - January 10"
    for m in DATE_TEXT.finditer(t):
        raw = _norm_date_text(m.group(0))
        has_year = bool(re.search(r"\b20\d\d\b", raw))
        try:
            s, e = common.parse_date_range(raw, default_year=today.year)
            if s and not has_year and s < today - dt.timedelta(days=60):
                s, e = common.parse_date_range(raw, default_year=today.year + 1)
        except (KeyError, ValueError, IndexError):            # one unreadable date never stops a crawl
            continue
        if s: out.append((s, e or s, has_year))
    # a card's short date ('Dec 14') next to its full one ('Sunday, December 14, 2025') takes the full one's year: rolled
    # forward on its own, last year's event became next month's
    dated = {(s.month, s.day): (s, e) for s, e, y in out if y}
    out = [dated.get((s.month, s.day), (s, e)) if not y else (s, e) for s, e, y in out]
    if not out:
        for m in NUM_DATE.finditer(t):
            mo, d1, y1, mo2, d2, y2 = m.groups()
            try:
                y = int(y1 or y2 or today.year); y = y + 2000 if y < 100 else y
                s = dt.date(y, int(mo), int(d1))
                e = dt.date(int(y2 or y) + (2000 if y2 and int(y2) < 100 else 0) if y2 else y, int(mo2 or mo), int(d2)) if d2 else s
                if not (y1 or y2) and s < today - dt.timedelta(days=60): s, e = s.replace(year=y + 1), e.replace(year=e.year + 1)
                if e < s or (e - s).days > 14: e = s
                out.append((s, e))
            except ValueError:
                continue
    return out


def _st(code_or_name):
    v = (code_or_name or "").strip()
    return v.upper() if v.upper() in STATES else STATE_BY_NAME.get(v.lower())


STREET_WORD = re.compile(rf"\b{STREET}\b\.?", re.I)


LABEL_WORDS = re.compile(r"^(?:dates?|when|where|location|locations|venue|address|city|site|sites|time|times|in|at|near|the|and|held|played|"
                         r"lacrosse|tournaments?|teams?|showcases?|events?|register|boys|girls|coed)$", re.I)   # tag text before a city


def _city_only(words):
    """'1100 Cedar Ln Bel Air' -> 'Bel Air', 'Dates Arlington' -> 'Arlington': keep what follows the last street word,
    drop web-ish tokens and leading label words."""
    w = re.split(r"\s+", words.strip())
    # menu words in capitals before the town ('SOLUTIONS CAMPS CONTACT Philadelphia'), a compass word ('NE Naples')
    if any(not x.isupper() for x in w):
        while len(w) > 1 and (w[0].isupper() and len(w[0]) > 1 and not w[0].rstrip(".").upper() in ("ST", "MT", "FT")):
            w = w[1:]
    while w and LABEL_WORDS.match(w[0].strip(":")): w = w[1:]
    # a street word ends the street part, except "St."/"Mt."/"Ft." opening the city name ("St. George", "Mt. Airy"),
    # also right after a facility ("Eastern University St. Davids")
    last = max((i for i, x in enumerate(w) if STREET_WORD.fullmatch(x)
                and not ((i == 0 or FACILITY_WORD.fullmatch(w[i - 1])) and i < len(w) - 1 and re.fullmatch(r"(?i)(st|mt|ft)\.?", x))),
               default=-1)
    # 'Complex. Naples': a word ending a sentence is not part of the town
    dot = max((i for i, x in enumerate(w) if i > last and x.endswith(".") and not re.fullmatch(r"(?i)(st|ste|ft|mt|pt)\.", x)), default=-1)
    last = max(last, dot) if dot < len(w) - 1 else last
    w = [x for x in w[last + 1:] if ("." not in x.rstrip(".") or re.fullmatch(r"(?i)(st|ste|ft|mt|pt)\.", x))
         and not any(ch.isdigit() for ch in x)]
    fac = max((i for i, x in enumerate(w) if FACILITY_WORD.fullmatch(x.rstrip(".,")) or x.lower() == "world"), default=-1)
    if 0 <= fac < len(w) - 1: w = w[fac + 1:]          # 'Bryant University Providence' -> 'Providence' ('College Park' stays)
    return " ".join(w)


def city_state_spans(text):
    """[(city, ST, start, end), ...] for every 'City, ST' in order, repeats kept ('Providence, RI' twice for two sites);
    start is where the town's own words begin, so the text before it can be read as that site's venue."""
    out = []
    for m in CITY_ST.finditer(text or ""):
        raw = re.sub(r"^(?:in|at|near|the|and|location|venue)\s+", "", m.group(1), flags=re.I)
        city, st = _city_only(raw), _st(m.group(2))
        w = city.split()
        if len(w) % 2 == 0 and w[:len(w) // 2] == w[len(w) // 2:]:
            city = " ".join(w[:len(w) // 2])                # 'Pingry School - Basking Ridge Basking Ridge, NJ'
        if not st or len(city) < 3 or city.lower() in STATE_BY_NAME: continue
        i = m.group(1).lower().rfind(city.lower())
        start = m.start(1) + (i if i >= 0 else 0)
        if city.isupper(): city = " ".join(x.capitalize() for x in city.lower().split())
        out.append((city, st, start, m.end()))
    return out


def find_city_states(text):
    """['Crofton, MD', ...] in order of appearance, deduped."""
    seen = []
    for m in CITY_ST.finditer(text or ""):
        city = _city_only(re.sub(r"^(?:in|at|near|the|and|location|venue)\s+", "", m.group(1), flags=re.I))
        st = _st(m.group(2))
        if not st or len(city) < 3 or city.lower() in STATE_BY_NAME: continue
        if city.isupper(): city = " ".join(x.capitalize() for x in city.lower().split())   # "ST. GEORGE" -> "St. George"
        v = f"{city}, {st}"
        if v not in seen: seen.append(v)
    return seen


def find_addresses(text):
    return list(dict.fromkeys(clean(m.group(0)).rstrip(".,") for m in ADDRESS.finditer(text or "")))


FACILITY_WORD = re.compile(r"\b(park|parks|complex|fields?|center|centre|\w*plex|stadium|university|college|academy|school|club|"
                           r"grounds|nook|arena|farm|sports|athletic|athletics|recreation|rec|turf|dome|campus|commons|preserve|meadows)\b", re.I)


NOISE = re.compile(r"\b(submit|apply|application|register|registration|purchase|buy|video|map|maps|click|details|info|tickets?|"
                   r"directions|parking|view|more|learn|here|world-class|venue|location|address|showcase|tournament)\b", re.I)


def venue_before(text, address):
    """The facility name printed just before its street address: 'Capelli Sport Complex 1659 Wayside Rd ...'."""
    i = (text or "").find(address or "\0")
    if i <= 0: return None
    before = re.split(r"(?:\d{4}|[.:!?|•()@])\s*", text[max(0, i - 120):i])[-1].strip(" ,-–")
    words = before.split()[-7:]
    cut = max((k for k, x in enumerate(words) if NOISE.fullmatch(x)), default=-1)
    words = words[cut + 1:]                           # 'Purchase Video Proving Grounds' -> 'Proving Grounds' 
    while words and not words[0][:1].isupper(): words = words[1:]
    name = " ".join(words)
    return name if 3 <= len(name) <= 80 and FACILITY_WORD.search(name) else None


def find_venue_names(text):
    out = []
    for m in VENUE_LABEL.finditer(text or ""):
        v = clean(m.group(1))
        v = re.split(r"\s{2,}|\s+(?:date|dates|time|times|cost|price|register|ages?|divisions?)\s*:", v, flags=re.I)[0].strip(" ,.-–")
        if not (3 <= len(v) <= 80) or EMAIL.search(v) or NOISE.search(v) or re.match(r"^(tbd|tba|various)\b", v, re.I): continue
        if v not in out: out.append(v)
    return out


def ages_gender(text, season_year):
    """-> (age_label, grad_min, grad_max, gender) where gender is boys | girls | both | None."""
    label, lo, hi, gender = common.parse_age_groups(DATE_TEXT.sub(" ", text or ""), season_year)   # the event's own date is not a grad year
    g = {"coed": "both"}.get(gender, gender)
    if not g:
        b, gi = re.search(r"\bboys?\b", text or "", re.I), re.search(r"\bgirls?\b", text or "", re.I)
        g = "both" if b and gi else "boys" if b else "girls" if gi else None
    return label, lo, hi, g


def find_team_count(text):
    m = TEAM_CAP.search(text or "") or TEAMS.search(text or "")
    if not m: return None
    n = int(m.group(1))
    return n if 4 <= n <= 600 else None


def find_times(text):
    """-> ('8:00 AM', '5:30 PM') first and last clock times, or (None, None)."""
    ts = []
    for m in TIME.finditer(text or ""):
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        ts.append((h % 12 + (12 if ap == "p" else 0), mi, f"{h}:{mi:02d} {ap.upper()}M"))
    if not ts: return None, None
    ts_sorted = sorted(ts)
    return ts_sorted[0][2], (ts_sorted[-1][2] if len(ts_sorted) > 1 else None)


def find_emails(html_or_text):
    found = re.findall(r"mailto:([^\"'?>\s]+)", html_or_text or "", re.I) + EMAIL.findall(html_or_text or "")
    out = []
    for e in found:
        e = e.strip().strip(".,;:").lower()
        if EMAIL.fullmatch(e) and not JUNK_EMAIL.search(e) and e not in out: out.append(e)
    return out


def find_phones(html_or_text):
    out = []
    for m in PHONE.finditer(re.sub(r"tel:", " ", html_or_text or "", flags=re.I)):
        p = f"({m.group(1)}) {m.group(2)}-{m.group(3)}"
        if p not in out: out.append(p)
    return out


def vendor_contact(text):
    """Name/email/phone closest to vendor/sponsor wording on a page. Any part may be None."""
    t = text or ""
    spots = [m.start() for m in VENDOR_WORDS.finditer(t)]
    if not spots: return {"name": None, "email": None, "phone": None}
    def nearest(rx, conv):
        best = None
        for m in rx.finditer(t):
            d = min(abs(m.start() - s) for s in spots)
            if d <= 400 and (best is None or d < best[0]): best = (d, conv(m))
        return best[1] if best else None
    pm = VENDOR_PERSON.search(t)
    return {"name": clean(pm.group(1) or pm.group(2)) if pm else None,
            "email": nearest(EMAIL, lambda m: m.group(0).lower()),
            "phone": nearest(PHONE, lambda m: f"({m.group(1)}) {m.group(2)}-{m.group(3)}")}


def jsonld_events(html):
    """schema.org Event objects embedded in the page (most CMS event plugins emit these)."""
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "", re.S | re.I):
        try: data = json.loads(m.group(1).strip())
        except Exception: continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            it = stack.pop(0)
            if not isinstance(it, dict): continue
            stack += it.get("@graph", []) if isinstance(it.get("@graph"), list) else []
            stack += [x.get("item", x) for x in it.get("itemListElement", []) if isinstance(x, dict)] if isinstance(it.get("itemListElement"), list) else []
            t = it.get("@type"); t = t if isinstance(t, list) else [t]
            if not ({"Event", "SportsEvent", "SocialEvent", "Festival"} & set(map(str, t))): continue
            loc = it.get("location") if isinstance(it.get("location"), dict) else {}
            addr = loc.get("address") if isinstance(loc.get("address"), dict) else {}
            geo = loc.get("geo") if isinstance(loc.get("geo"), dict) else {}
            def d(x):
                try: return dt.date.fromisoformat(str(x)[:10])
                except Exception: return None
            out.append({"name": clean(it.get("name")), "start": d(it.get("startDate")), "end": d(it.get("endDate")) or d(it.get("startDate")),
                        "url": it.get("url"), "venue_name": clean(loc.get("name")) or None,
                        "address": clean(" ".join(str(addr.get(k) or "") for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode"))) or None,
                        "city": addr.get("addressLocality"), "state": _st(addr.get("addressRegion")),
                        "lat": geo.get("latitude"), "long": geo.get("longitude"), "description": clean(it.get("description"))[:1500]})
    return out
