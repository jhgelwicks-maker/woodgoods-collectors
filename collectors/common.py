"""Shared schema, date parsing, and Supabase upsert for every collector.

Every collector returns a list of dicts with these keys (missing = None):
  source            short slug, e.g. "tourneymachine"
  source_event_id   stable id on that source; dedupe_key = f"{source}:{source_event_id}"
  name, sport, organizer_name
  start_date, end_date   (date objects)
  venue_name, city, state, city_states (list of "City, ST")
  venue_count, split_venue (bool)
  team_count, division_count, divisions (text)
  vendor_contact_email, vendor_contact_name, age_groups (text)
  listing_url, official_url
  raw (dict, kept as jsonb for debugging)
"""
import os, re, json, time, datetime, logging, requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger("collectors")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

UA = {"user-agent": "WoodgoodsEventBot/1.0 (+contact: ops@woodgoods)"}
MON = {m: i for i, m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1)}

def parse_date_range(text, default_year=None):
    """Handles 'Oct 17, 2026' | 'Oct 3 - 4, 2026' | 'Sep 13 - Oct 18, 2026' |
    'Jun 17, 2025 - Jun 18, 2027' | 'October 10-12, 2026' | 'September 4-6, 2026'."""
    if not text: return None, None
    t = text.replace("\u2013", "-").replace("\u2014", "-").strip()
    t = re.sub(r"([A-Za-z]{3})[a-z]*", lambda m: m.group(1), t)  # October -> Oct
    t = re.sub(r"\s*-\s*", " - ", t)
    D = datetime.date
    m = re.match(r"(\w{3}) (\d+), (\d{4}) - (\w{3}) (\d+), (\d{4})$", t)
    if m: return D(int(m[3]), MON[m[1]], int(m[2])), D(int(m[6]), MON[m[4]], int(m[5]))
    m = re.match(r"(\w{3}) (\d+) - (\w{3}) (\d+), (\d{4})$", t)
    if m: return D(int(m[5]), MON[m[1]], int(m[2])), D(int(m[5]), MON[m[3]], int(m[4]))
    m = re.match(r"(\w{3}) (\d+) - (\d+), (\d{4})$", t)
    if m: return D(int(m[4]), MON[m[1]], int(m[2])), D(int(m[4]), MON[m[1]], int(m[3]))
    m = re.match(r"(\w{3}) (\d+), (\d{4})$", t)
    if m: x = D(int(m[3]), MON[m[1]], int(m[2])); return x, x
    if default_year:
        m = re.match(r"(\w{3}) (\d+) - (\d+)$", t)
        if m: return D(default_year, MON[m[1]], int(m[2])), D(default_year, MON[m[1]], int(m[3]))
        m = re.match(r"(\w{3}) (\d+)$", t)
        if m: x = D(default_year, MON[m[1]], int(m[2])); return x, x
    log.warning("unparsed date: %r", text)
    return None, None


# Tourney Machine's cityStates are user-entered and often malformed:
#   'Conshohocken, PA 19428, PA'  (zip embedded, state duplicated)   'Fort Washington, PA, PA'
ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
ST_RE = re.compile(r"^[A-Z]{2}$")
_STREET = re.compile(r"^\s*\d*\s*[\w.'-]+(?:\s+[\w.'-]+)*?\s+(?:rd|road|st|street|ave|avenue|dr|drive|blvd|hwy|highway|pike|ln|lane|way)\.?\s+", re.I)

def split_city_state(cs):
    """'Conshohocken, PA 19428, PA' -> ('Conshohocken', 'PA'); 'Devens, MA' -> ('Devens', 'MA')"""
    if not cs: return None, None
    txt = ZIP_RE.sub(" ", cs)
    parts = [p.strip(" ,") for p in txt.split(",") if p.strip(" ,")]
    state = None
    while parts and ST_RE.match(parts[-1].upper()) and len(parts) > 1:
        state = parts.pop().upper()
    if parts and not state:
        m = re.search(r"\b([A-Z]{2})\b\s*$", parts[-1])
        if m: state = m.group(1); parts[-1] = parts[-1][:m.start()].strip(" ,")
    city = clean_city(", ".join(p for p in parts if p)) or None
    if city and ST_RE.match(city.upper()): city, state = None, state or city.upper()
    return city, state

def clean_city(city):
    """Strip street fragments organizer sites leak into the city: 'Wayside Rd. Tinton Falls' -> 'Tinton Falls'."""
    if not city: return city
    return re.sub(r"\s+", " ", _STREET.sub("", city)).strip(" ,") or None

def normalize_city_states(cs_list):
    out = []
    for cs in cs_list or []:
        city, st = split_city_state(cs)
        v = f"{city}, {st}" if city and st else (st or city)
        if v and v not in out: out.append(v)
    return out

# Age groups -> grad-year scale. "8th Grade (2031)", "U14", "14U", "2028 Boys", "HS", "Varsity", "Adult".
def parse_age_groups(text, season_year=None):
    """Returns (label, grad_min, grad_max, gender) or (None, None, None, None)."""
    if not text: return None, None, None, None
    y = season_year or datetime.date.today().year
    grads = set()
    for m in re.finditer(r"\b(20[2-4]\d)\b", text): grads.add(int(m.group(1)))
    for m in re.finditer(r"\b(?:U|u)(\d{1,2})\b|\b(\d{1,2})(?:U|u)\b", text):
        age = int(m.group(1) or m.group(2))
        if 5 <= age <= 19: grads.add(y + (18 - age) + 1)      # U14 in 2026 -> class of 2031
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)\s+grade\b", text, re.I):
        g = int(m.group(1))
        if 1 <= g <= 12: grads.add(y + (12 - g) + 1)
    if not grads and re.search(r"\b(HS|high school|varsity|JV)\b", text, re.I): grads.update({y + 1, y + 4})
    if re.search(r"\b(adult|men'?s|women'?s|masters|open)\b", text, re.I) and not grads: return "Adult", None, None, None
    gender = None
    if re.search(r"\b(boys?|men)\b", text, re.I) and not re.search(r"\b(girls?|women)\b", text, re.I): gender = "boys"
    elif re.search(r"\b(girls?|women)\b", text, re.I) and not re.search(r"\b(boys?|men)\b", text, re.I): gender = "girls"
    elif re.search(r"\b(girls?|women)\b", text, re.I) and re.search(r"\b(boys?|men)\b", text, re.I): gender = "coed"
    if not grads: return None, None, None, gender
    lo, hi = min(grads), max(grads)
    return (f"{lo}" if lo == hi else f"{lo}\u2013{hi}"), lo, hi, gender

import math
BIG_WORDS = re.compile(r"county fair|state fair|street fair|music fest|food fest|beer|wine|oktoberfest|balloon|air show|carnival|fireworks|parade|regatta|seafood|lobster|garlic|pumpkin fest|apple fest", re.I)
SMALL_WORDS = re.compile(r"craft fair|craft show|church|bazaar|holiday market|flea|yard sale|bake sale|tea|luncheon|school|pto|library|artisan market|makers market|vendor fair", re.I)
def _num(x):
    if x is None: return None
    m = re.search(r"\d[\d,]*", str(x))
    return int(m.group(0).replace(",", "")) if m else None

def festival_size_score(r):
    """0-100 size score for festival rows + the inputs used. Weighted mean of whatever signals exist."""
    raw = r.get("raw") or {}
    parts = []   # (score, weight, label)
    att = r.get("expected_attendance") or _num(raw.get("attendance_text"))
    if att: parts.append((max(0, min(100, 45 * math.log10(max(att, 1) / 300))), 5, f"attendance {att:,}"))
    fee = r.get("vendor_fee_max") or r.get("vendor_fee_min")
    if fee: parts.append((max(0, min(100, (fee - 25) / 575 * 100)), 2, f"booth fee ${fee:,.0f}"))
    vc = _num(raw.get("vendor_count"))   # Eventeny application_count is form count, not slots; ignored
    if vc:
        # competition, not size: 40-100 booths is ideal; beyond that every booth dilutes you
        comp = 100 if vc <= 100 else max(0, 100 - (vc - 100) * 0.45)
        if vc < 15: comp = 40
        parts.append((comp, 3, f"{vc} vendors (competition)"))
    if att and vc:
        apv = att / vc                    # shoppers per booth is the truest number we get
        parts.append((max(0, min(100, (apv - 30) / 170 * 100)), 4, f"{apv:,.0f} attendees per vendor"))
    yrs = _num(raw.get("years"))
    if yrs: parts.append((max(0, min(100, yrs / 40 * 100)), 1, f"{yrs} years running"))
    d = r.get("days")
    if d: parts.append(({1: 10, 2: 40}.get(d, 55 if d <= 4 else 30), 1, f"{d} day(s)"))
    if not parts: return None, 0.0, ["no size signals"]
    score = sum(p[0] * p[1] for p in parts) / sum(p[1] for p in parts)
    text = " ".join(str(x) for x in (r.get("name"), raw.get("description"), " ".join(r.get("tags") or [])) if x)
    adj = 0
    if BIG_WORDS.search(text): adj += 10
    if SMALL_WORDS.search(text): adj -= 12
    score = max(0, min(100, score + adj))
    conf = round(min(1.0, sum(p[1] for p in parts) / 13), 2)   # 13 ~ all signals present
    inputs = [p[2] for p in parts] + ([f"keywords {adj:+d}"] if adj else [])
    return round(score), conf, inputs

# Attendance estimate for tournaments: teams x roster x (1 + adults per player).
ADULTS_PER_PLAYER = 1.33
ROSTER = {"lacrosse": 21, "lacrosse_girls": 20, "soccer": 15, "baseball": 13, "softball": 12, "basketball": 10, "volleyball": 11,
          "hockey": 16, "football": 25, "flag football": 9, "field hockey": 17, "cheer": 20}
def roster_for(sport, gender=None, grad_min=None, grad_max=None, season_year=None):
    sp = (sport or "").lower()
    y = season_year or datetime.date.today().year
    age_hi = (18 - (grad_min - y - 1)) if grad_min else None      # oldest age band in the event
    if sp == "soccer" and age_hi:
        return 11 if age_hi <= 10 else 14 if age_hi <= 12 else 16
    if sp == "lacrosse":
        if age_hi and age_hi <= 10: return 11
        return ROSTER["lacrosse_girls"] if gender == "girls" else ROSTER["lacrosse"]
    return ROSTER.get(sp)

def estimate_attendance(r):
    teams = r.get("team_count") or r.get("event_team_count")
    if not teams: return None, None
    ros = roster_for(r.get("sport"), (r.get("raw") or {}).get("gender"), r.get("grad_year_min"), r.get("grad_year_max"))
    if not ros: return None, None
    return int(round(teams * ros * (1 + ADULTS_PER_PLAYER))), f"{teams} teams x {ros} roster x {1 + ADULTS_PER_PLAYER:.2f}"

# ---- Festival signals -> tags + a mild destination score -------------------------------------
_SIG = [  # (tag, regex, destination delta)
    ("venue:fairgrounds",  r"fairground|expo center|exposition|racetrack|speedway|arena", +25),
    ("venue:farm",         r"\bfarm\b|orchard|vineyard|winery|brewery|cidery|ranch|apple pick", +20),
    ("venue:park",         r"\bpark\b|campground|lake|beach|ski|mountain|reservoir|state forest", +12),
    ("venue:field-complex",r"athletic complex|sports complex|fields?\b.*complex|stadium", +12),
    ("venue:downtown",     r"downtown|main st\b|main street|town center|town common|village green|plaza|square\b|city hall|sidewalk", -18),
    ("street-closure",     r"street closure|closed to traffic|block party|street fair|street festival", -12),
    ("venue:mall",         r"\bmall\b|shopping center|plaza parking", -15),
    ("ticketed",           r"admission[:\s]*\$|tickets?\s*(?:from\s*)?\$|gate fee|entry fee|\$\d+\s*(?:per person|admission|entry|at the gate)|wristband", +15),
    ("free-admission",     r"free admission|admission is free|free (?:to|and) (?:attend|open)|no admission|free entry|free event", -8),
    ("paid-parking",       r"parking[:\s]*\$|paid parking|parking fee|shuttle", +10),
    ("rural",              r"\b(?:route|rt\.?|hwy|highway|county rd|county road)\s*\d+", +8),
    ("juried",             r"\bjuried\b|jury fee|jurying", 0),
    ("handmade-only",      r"handmade only|handcrafted only|no (?:resale|buy[- ]sell|commercial|manufactured|imported)|artisan-only|must be made by", 0),
    ("category-limits",    r"one vendor per category|limited (?:number of )?(?:apparel|clothing|hat)|category exclusiv|exclusive rights", 0),
    ("commission-fee",     r"\d{1,2}\s?% of (?:gross|sales|total)", 0),
    ("coi-required",       r"certificate of insurance|\bcoi\b|proof of insurance|liability insurance required", 0),
    ("permit-required",    r"(?:sales tax|resale|vendor|peddler|business) (?:permit|license|certificate)|tax id required", 0),
    ("tent-required",      r"bring your own tent|tent required|must provide (?:your own )?tent|10\s?x\s?10", 0),
    ("indoor",             r"\bindoor|inside the|gymnasium|convention center|pavilion", 0),
    ("rain-or-shine",      r"rain or shine|held regardless", 0),
    ("rain-date",          r"rain date", 0),
    ("alcohol",            r"beer|wine|spirits|brew|oktoberfest|cocktail|tasting", 0),
    ("family",             r"family|kids|children|petting zoo|hayride|pumpkin|trick[- ]or[- ]treat|bounce", 0),
    ("live-music",         r"live music|bands?\b|concert|stage", 0),
    ("food-trucks",        r"food trucks?", 0),
    ("holiday-market",     r"holiday market|christmas market|craft fair|craft show|bazaar", 0),
]
_SIG = [(t, re.compile(rx, re.I), d) for t, rx, d in _SIG]

def festival_signals(r):
    """-> (tags, destination_score, inputs). Score is deliberately mild: 20..85."""
    raw = r.get("raw") or {}
    text = " ".join(str(x) for x in (r.get("name"), r.get("venue_name"), raw.get("address"), raw.get("location_text"),
                                     raw.get("description"), raw.get("fees_text"), raw.get("deadline_text")) if x)
    tags, delta, inputs = [], 0, []
    for tag, rx, d in _SIG:
        if rx.search(text):
            tags.append(tag); delta += d
            if d: inputs.append(f"{tag} {d:+d}")
    if r.get("vendor_fee_min") == 0 and "free-admission" not in tags and not any(t.startswith("venue:") for t in tags):
        pass
    score = max(20, min(85, 50 + delta))
    return tags, score, inputs

# ---- Festival ICP fit --------------------------------------------------------------------
_PRIVATE = re.compile(r"happy hour|reception|gala|fundrais(?:er|ing) (?:dinner|breakfast|lunch)|luncheon|conference|summit|webinar|wedding|corporate|networking|mixer|awards|banquet|open house|grand opening|membership|members only|invite only|private event|golf outing|5k|10k|run/walk|fun run|road race|trivia|bingo|yoga|workshop|class\b|seminar|book signing|meeting", re.I)
_CRAFTY = re.compile(r"craft fair|craft show|craft festival|holiday market|christmas market|bazaar|flea|yard sale|rummage|artisan market|makers market|art show|art fair|arts festival|fine art", re.I)
_SEASONAL_GOOD = re.compile(r"fall fest|harvest|pumpkin|apple|oktoberfest|town day|family day|community day|founders|heritage|county fair|state fair|street fair|car show|truck show|cruise|carnival|fireworks|seafood|lobster|garlic|balloon|air show|rodeo|regatta|celebration|jubilee|homecoming|touch[- ]a[- ]truck", re.I)

def festival_fit(r):
    """-> (fit_score 0-100 or None, reasons, excluded_reason or None). Only private/non-public events are excluded;
    everything else is tagged and penalized so it stays visible."""
    raw = r.get("raw") or {}; tags = list(r.get("tags") or [])
    name = r.get("name") or ""; text = f"{name} {raw.get('description') or ''}"
    if _PRIVATE.search(name): return None, [], "not a public fair (private/corporate/race/class)"
    if _CRAFTY.search(name) and not _SEASONAL_GOOD.search(name) and "craft-market" not in tags: tags.append("craft-market")
    fee = r.get("vendor_fee_max") or r.get("vendor_fee_min")
    if fee and fee > 400 and "fee-over-400" not in tags: tags.append("fee-over-400")
    sd, ed = r.get("start_date"), r.get("end_date")
    try:
        d0 = datetime.date.fromisoformat(sd) if isinstance(sd, str) else sd
        d1 = datetime.date.fromisoformat(ed) if isinstance(ed, str) else ed
        if d0 and d1 and d1 >= d0 and (d1 - d0).days <= 6 and not any((d0 + datetime.timedelta(i)).weekday() >= 5 for i in range((d1 - d0).days + 1)):
            if "weekday-only" not in tags: tags.append("weekday-only")
    except Exception: pass
    dl = r.get("application_deadline")
    try:
        dl_d = datetime.date.fromisoformat(dl) if isinstance(dl, str) else dl
        if dl_d and dl_d < datetime.date.today() and "deadline-passed" not in tags: tags.append("deadline-passed")
    except Exception: pass
    r["tags"] = tags; tags = set(tags)
    score, why = 40, []
    def add(n, label): 
        nonlocal score; score += n; why.append(f"{label} {n:+d}")
    if "family" in tags: add(15, "family")
    if tags & {"venue:fairgrounds", "venue:farm", "venue:field-complex"}: add(12, "destination venue")
    if "ticketed" in tags: add(8, "ticketed")
    if "paid-parking" in tags: add(5, "paid parking")
    if "live-music" in tags or "food-trucks" in tags: add(5, "music/food")
    if _SEASONAL_GOOD.search(name): add(10, "community/seasonal fair")
    if re.search(r"\b\d{1,3}(?:st|nd|rd|th)\s+annual\b", text, re.I): add(6, "recurring")
    if (r.get("days") or 1) >= 2: add(5, "multi-day")
    if "alcohol" in tags and "family" not in tags: add(-10, "alcohol-only crowd")
    if "craft-market" in tags: add(-15, "craft/holiday market")
    if "handmade-only" in tags: add(-25, "handmade only")
    if "juried" in tags: add(-15, "juried")
    if "commission-fee" in tags: add(-10, "commission on sales")
    if "fee-over-400" in tags: add(-12, "booth fee > $400")
    if "weekday-only" in tags: add(-20, "weekday only")
    if "deadline-passed" in tags: add(-10, "deadline passed")
    if "venue:downtown" in tags and "venue:park" not in tags: add(-6, "downtown pass-through")
    if "indoor" in tags: add(-8, "indoor")
    ss = r.get("size_score")
    if ss is not None: add(int((ss - 50) * 0.3), f"size {ss}")
    ds = r.get("destination_score")
    if ds is not None: add(int((ds - 50) * 0.2), f"destination {ds}")
    return max(0, min(100, score)), why, None

# ---- Cross-source identity ----------------------------------------------------------------
ORG_ALIASES = {"ptlacrosse": "primetime", "prime time": "primetime", "primetime lacrosse": "primetime", "apex lacrosse events": "apex",
               "apex lacrosse": "apex", "nxt sports": "nxt", "nxt lacrosse": "nxt", "mlt": "mlt", "madlax": "madlax",
               "nh tomahawks": "tomahawks", "new hampshire tomahawks": "tomahawks", "hogan's lacrosse": "hogans", "hogans lacrosse": "hogans",
               "laxachusetts girls": "laxachusetts", "laxachusetts boys": "laxachusetts", "adrenaline lacrosse": "adrenaline",
               "victory event series": "victory", "trilogy lacrosse": "trilogy", "legends lacrosse": "legends", "buku lacrosse": "buku",
               "aloha tournaments": "aloha", "alliance lacrosse league": "alliance", "premier lacrosse league": "pll", "pll": "pll"}
_STOP = re.compile(r"\b(20\d\d|\d{1,3}(?:st|nd|rd|th)|annual|the|presented by.*$|powered by.*$|tournament|tourney|invitational|classic|showcase|event|lacrosse|lax|soccer|baseball|softball|volleyball|hockey|basketball|festival|fest|fair|boys|girls|youth|hs|high school|men'?s|women'?s)\b", re.I)

def canon_org(name):
    if not name: return None
    n = re.sub(r"[^a-z0-9 ]+", " ", name.lower()); n = re.sub(r"\s+", " ", n).strip()
    n = re.sub(r"\b(llc|inc|events?|sports?|group|series)\b", "", n).strip()
    return ORG_ALIASES.get(n) or ORG_ALIASES.get(name.lower().strip()) or n or None

def name_core(name):
    """'2026 Boys Jersey Fall Invitational' -> 'jersey fall'"""
    if not name: return None
    n = _STOP.sub(" ", name.lower()); n = re.sub(r"[^a-z0-9 ]+", " ", n); n = re.sub(r"\s+", " ", n).strip()
    return n or re.sub(r"[^a-z0-9 ]+", " ", name.lower()).strip()

def fingerprint(name, sport, state):
    core = name_core(name)
    return f"{(sport or '').lower()}|{(state or '').upper()}|{core}" if core else None

# ---- Tourney Machine documents + club parsing ---------------------------------------------
DOC_RE = re.compile(r'https://assets\.tourneymachine\.com/Tournament/[^"\'\s]+\.(?:pdf|png|jpe?g|gif|docx?|xlsx?)', re.I)
MAP_WORDS = re.compile(r"map|layout|site|field[- ]?plan|parking|venue|complex|directions", re.I)
RULES_WORDS = re.compile(r"rule|regulation|policy|waiver|code of conduct", re.I)
def parse_documents(page_html):
    out = []
    for u in dict.fromkeys(DOC_RE.findall(page_html or "")):
        fn = u.rsplit("/", 1)[-1]
        ext = fn.rsplit(".", 1)[-1].lower()
        kind = "field_map" if MAP_WORDS.search(fn) else "rules" if RULES_WORDS.search(fn) else ("image" if ext in ("png", "jpg", "jpeg", "gif") else "document")
        if kind == "image" and re.search(r"140x140|logo|icon", fn, re.I): kind = "logo"
        out.append({"url": u, "filename": fn, "type": ext, "kind": kind})
    return out or None

_CLUB_COLORS = r"black|white|red|blue|green|gold|silver|orange|purple|navy|grey|gray|maroon|teal|yellow|pink|royal|carolina|columbia"
_CLUB_STRIP = re.compile(r"\b(20[2-4]\d|\d{1,2}(?:u|th|st|nd|rd)|u\d{1,2}|boys?|girls?|" + _CLUB_COLORS + r"|select|premier|national|aa|aaa|hs|varsity|jv|youth)\b|[-\u2013/]", re.I)
_TRAILING_NUM = re.compile(r"(?<=\s)\d{1,2}$|(?<=\s)\d{1,2}(?=\s)")   # standalone 1-2 digit tokens after the first word
def club_from_team(team_name):
    """'3d NE 2028 Red' -> '3d NE'; 'CT Lightning Gold-Wright' -> 'CT Lightning'; 'Aces 18-Cahill' -> 'Aces'"""
    if not team_name: return None
    t = team_name.split(" - ")[0]
    t = re.split(r"[-\u2013](?=[A-Z][a-z])", t)[0]           # drop coach suffix like -Cahill
    t = _CLUB_STRIP.sub(" ", t)
    lead = re.match(r"^\s*(\d{1,2}\s)", t)                     # keep a leading number ('4 Leaf', '3d NE'); drop '18' etc. elsewhere
    body = t[lead.end():] if lead else t
    body = _TRAILING_NUM.sub(" ", " " + body)
    t = (lead.group(1) if lead else "") + body
    t = re.sub(r"\s+", " ", t).strip(" -")
    return t or None

def clubs_from_options(opts):
    """opts = [(team_id, team_name, division)] -> sorted list of {club, teams}"""
    c = {}
    for _, name, _d in opts:
        club = club_from_team(name)
        if club: c[club] = c.get(club, 0) + 1
    return [{"club": k, "teams": v} for k, v in sorted(c.items(), key=lambda kv: -kv[1])] or None

def states_from(city_states):
    return sorted({c.split(", ")[-1] for c in (city_states or []) if ", " in c})

def normalize(row):
    r = dict(row)
    r["dedupe_key"] = f"{r['source']}:{r['source_event_id']}"
    cs = normalize_city_states(r.get("city_states") or [])
    r["venue_count"] = r.get("venue_count") or (len(cs) or None)
    r["split_venue"] = bool(r.get("split_venue") or (len(cs) > 1))
    if not r.get("state") and cs: r["state"] = ", ".join(states_from(cs))
    if not r.get("city") and cs: r["city"] = cs[0].split(", ")[0]
    s, e = r.get("start_date"), r.get("end_date")
    r["days"] = (e - s).days + 1 if s and e else None
    r["is_league"] = bool(r["days"] and r["days"] > 3)
    src = " ".join(str(x) for x in (r.get("divisions"), r.get("age_groups"), r.get("name")) if x)
    label, lo, hi, gender = parse_age_groups(src, (s.year if s else None))
    if lo: r["grad_year_min"], r["grad_year_max"] = lo, hi
    if label and not r.get("age_groups"): r["age_groups"] = label
    if gender and not (r.get("raw") or {}).get("gender"): r.setdefault("raw", {})["gender"] = gender
    if r.get("sport") not in ("festival",) and r.get("category") != "festival":
        est, how = estimate_attendance(r)
        if est:
            r["expected_attendance"] = est
            r.setdefault("raw", {})["attendance_basis"] = how
    if r.get("sport") == "festival" or r.get("category") == "festival":
        if not r.get("expected_attendance"):
            r["expected_attendance"] = _num((r.get("raw") or {}).get("attendance_text"))
        sig_tags, dest, dest_in = festival_signals(r)
        r["tags"] = list(dict.fromkeys((r.get("tags") or []) + sig_tags))
        r["destination_score"] = dest
        r.setdefault("raw", {})["destination_inputs"] = dest_in or ["no venue/admission signals; neutral 50"]
        sc, conf, inputs = festival_size_score(r)
        if sc is not None:
            r["size_score"], r["size_confidence"] = sc, conf
            r.setdefault("raw", {})["size_inputs"] = inputs
        fit, why, excl = festival_fit(r)
        r["fit_score"] = fit
        r.setdefault("raw", {})["fit_inputs"] = why
        if excl: r["raw"]["fit_excluded"] = excl
    for k in ("start_date", "end_date"):
        if isinstance(r.get(k), datetime.date): r[k] = r[k].isoformat()
    r["city_states"] = cs
    r["organizer_canon"] = canon_org(r.get("organizer_name"))
    r["name_core"] = name_core(r.get("name"))
    r["event_fingerprint"] = fingerprint(r.get("name"), r.get("sport"), (r.get("state") or "").split(",")[0])
    try:
        d = datetime.date.fromisoformat(r["start_date"]) if isinstance(r.get("start_date"), str) else r.get("start_date")
        r["calendar_week"] = d.isocalendar()[1] if d else None
    except Exception: r["calendar_week"] = None
    r["last_seen_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return r

class Supabase:
    """Writes go through the app's locked ingest endpoint (Lovable Cloud does not expose a service key)."""
    def __init__(self):
        self.url = os.environ["INGEST_URL"].rstrip("/")
        self.h = {"x-ingest-key": os.environ["INGEST_KEY"], "Content-Type": "application/json"}

    def _post(self, body):
        res = requests.post(self.url, headers=self.h, data=json.dumps(body, default=str), timeout=120)
        if res.status_code >= 300:
            raise RuntimeError(f"ingest failed {res.status_code}: {res.text[:500]}")
        return res.json() if res.text else {}

    def upsert_candidates(self, rows, chunk=400):
        merged = {}
        for r in rows:
            nr = {k: v for k, v in normalize(r).items() if v is not None}
            key = nr["dedupe_key"]
            if key in merged:
                merged[key].update(nr)          # later row fills gaps, never blanks a value
            else:
                merged[key] = nr
        rows = list(merged.values())
        n = 0
        for i in range(0, len(rows), chunk):
            batch = rows[i:i+chunk]
            self._post({"source": batch[0]["source"] if batch else None, "rows": batch})
            n += len(batch)
        return n

    def log_run(self, source, rows_seen, rows_upserted, errors, started, note=""):
        self._post({"source": source, "run": {"started_at": started, "finished_at": datetime.datetime.utcnow().isoformat()+"Z",
                    "rows_seen": rows_seen, "rows_upserted": rows_upserted, "errors": errors, "note": note[:2000]}})

INSECURE_HOSTS = {"mayouthsoccer.org"}   # sites with a broken certificate chain

def polite_get(url, headers=None, timeout=40, tries=4, sleep=1.0):
    h = {**UA, **(headers or {})}
    verify = not any(host in url for host in INSECURE_HOSTS)
    for i in range(tries):
        try:
            r = requests.get(url, headers=h, timeout=timeout, verify=verify)
            if r.status_code == 200: return r.text
            log.warning("GET %s -> %s", url, r.status_code)
            if r.status_code in (429, 403, 503):
                time.sleep(5 * (i + 1)); continue       # back off when rate-limited
        except requests.RequestException as e:
            log.warning("GET %s failed: %s", url, e)
        time.sleep(sleep * (i + 1))
    return None
