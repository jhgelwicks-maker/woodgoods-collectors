"""Eventeny (eventeny.com) — festival/market listings, plain HTML, paginated per state.
Event page gives organizer, date, description (often states attendance); the vendor application page
gives booth fees, application deadline, rain date and categories. All free, no login."""
import re, html, time
from concurrent.futures import ThreadPoolExecutor
from . import common
from .common import log
from .fairsandfestivals import STATES, ATTEND_RE

BASE = "https://www.eventeny.com"
CARD_RE = re.compile(r'data-href="(https://www\.eventeny\.com//?events/[^"]+)"')
FEE_RE = re.compile(r"\$\s?([\d,]+(?:\.\d+)?)")

def _txt(s):
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " | ", s); s = re.sub(r"(\s*\|\s*)+", " | ", s)
    return html.unescape(re.sub(r"\s+", " ", s))

def _between(t, a, b, maxlen=600):
    i = t.find(a)
    if i < 0: return None
    j = t.find(b, i + len(a)) if b else -1
    seg = t[i + len(a): j if 0 < j < i + len(a) + maxlen else i + len(a) + maxlen]
    return seg.strip(" |")

def listing(state):
    urls = []
    for page in range(1, 40):
        h = common.polite_get(f"{BASE}/events/?state={state}&page={page}")
        if not h: break
        found = [u.replace(".com//", ".com/") for u in CARD_RE.findall(h)]
        if not found: break
        new = [u for u in found if u not in urls]
        if not new: break
        urls += new; time.sleep(0.4)
    return urls

def event(url):
    d = common.polite_get(url)
    if not d: return None
    t = _txt(d)
    name = html.unescape(re.search(r"<title>(.*?)\s*-\s*Eventeny", d, re.S).group(1)).strip() if re.search(r"<title>(.*?)- Eventeny", d) else None
    org = _between(t, "Hosted by |", "|", 120)
    date_txt = _between(t, "Starts on |", "|", 80)
    m = re.search(r"(\w+),\s+(\w+)\s+(\d+)\w*,\s+(\d{4})", date_txt or "")
    start, _ = common.parse_date_range(f"{m[2][:3]} {m[3]}, {m[4]}") if m else (None, None)
    loc = _between(t, date_txt + " |", "|", 120) if date_txt else None
    about = _between(t, "About the event |", "| Show more", 3000) or ""
    att = ATTEND_RE.search(about)
    tags = sorted(set(x.lower() for x in re.findall(r'class="card-tag">([^<]+)<', d)))
    vend_ids = list(dict.fromkeys(re.findall(r'/events/vendor/\?id=(\d+)', d)))
    v = {}
    if vend_ids:
        vd = common.polite_get(f"{BASE}/events/vendor/?id={vend_ids[0]}")
        if vd:
            vt = _txt(vd)
            v["deadline_text"] = _between(vt, "Deadline: ", "|", 60)
            v["fees_text"] = _between(vt, "Fees |", "| About", 200)
            v["rain_date"] = _between(vt, "Rain Date: ", "|", 40)
            dl = re.search(r"(\w{3}) (\d+), (\d{4})", v["deadline_text"] or "")
            v["deadline"], _ = common.parse_date_range(f"{dl[1]} {dl[2]}, {dl[3]}") if dl else (None, None)
            std = re.search(r"Standard fees?:\s*([^|]*)", v["fees_text"] or "")
            nums = [float(x.replace(",", "")) for x in FEE_RE.findall(std.group(1) if std else (v["fees_text"] or ""))]
            v["fee_min"], v["fee_max"] = (min(nums), max(nums)) if nums else (None, None)
            v["app_count"] = len(vend_ids)
    end_m = re.search(r"Date: \w{3} \d+, \d{4} [^|]*?- (\w{3}) (\d+), (\d{4})", (v.get("fees_text") or "") + " " + t)
    end, _ = common.parse_date_range(f"{end_m[1]} {end_m[2]}, {end_m[3]}") if end_m else (start, None)
    city_state = None
    if loc:
        parts = [p.strip() for p in loc.split(",")]
        if len(parts) >= 2: city_state = f"{parts[0]}, {common_state(parts[1])}"
    return {"name": name, "organizer": org, "start": start, "end": end or start, "loc": loc, "city_state": city_state,
            "about": about, "attendance": int(att.group(1).replace(",", "")) if att else None,
            "attendance_text": att.group(0) if att else None, "tags": tags, "vendor": v,
            "vendor_url": f"{BASE}/events/vendor/?id={vend_ids[0]}" if vend_ids else None}

_ABBR = {"Massachusetts":"MA","New Hampshire":"NH","Connecticut":"CT","Rhode Island":"RI","New York":"NY","New Jersey":"NJ","Pennsylvania":"PA",
         "Maryland":"MD","Vermont":"VT","Maine":"ME","Virginia":"VA","Ohio":"OH","Florida":"FL","Texas":"TX","California":"CA","Illinois":"IL",
         "Georgia":"GA","North Carolina":"NC","South Carolina":"SC","Michigan":"MI","Indiana":"IN","Wisconsin":"WI","Minnesota":"MN","Colorado":"CO",
         "Arizona":"AZ","Washington":"WA","Oregon":"OR","Tennessee":"TN","Kentucky":"KY","Missouri":"MO","Delaware":"DE","Alabama":"AL","Louisiana":"LA",
         "Oklahoma":"OK","Iowa":"IA","Kansas":"KS","Nebraska":"NE","Nevada":"NV","Utah":"UT","New Mexico":"NM","Arkansas":"AR","Mississippi":"MS",
         "West Virginia":"WV","Idaho":"ID","Montana":"MT","Wyoming":"WY","North Dakota":"ND","South Dakota":"SD","Alaska":"AK","Hawaii":"HI","District of Columbia":"DC"}
def common_state(s): return _ABBR.get(s.strip(), s.strip()[:2].upper())

def collect(states=STATES):
    rows, errors = [], 0
    for st in states:
        urls = listing(st)
        log.info("eventeny %s: %d events", st, len(urls))
        with ThreadPoolExecutor(6) as ex:
            evs = list(ex.map(event, urls))
        for u, ev in zip(urls, evs):
            if not ev or not ev["name"]: errors += 1; continue
            v = ev["vendor"]
            tags = list(ev["tags"])
            for kw in ("family", "kids", "children", "21+", "adult", "craft", "art", "music", "food", "beer", "wine", "holiday", "harvest", "pumpkin", "market"):
                if re.search(r"\b" + re.escape(kw) + r"\b", ev["about"], re.I) and kw not in tags: tags.append(kw)
            rows.append({
                "source": "eventeny", "source_event_id": u.rstrip("/").rsplit("-", 1)[-1],
                "name": ev["name"], "sport": "festival", "organizer_name": ev["organizer"],
                "start_date": ev["start"], "end_date": ev["end"], "venue_name": None,
                "city_states": [ev["city_state"]] if ev["city_state"] else [],
                "listing_url": u, "official_url": u,
                "vendor_fee_min": v.get("fee_min"), "vendor_fee_max": v.get("fee_max"),
                "application_deadline": v.get("deadline"), "application_url": ev["vendor_url"],
                "expected_attendance": ev["attendance"], "rain_date_text": v.get("rain_date"),
                "tags": tags, "category": "festival",
                "raw": {"description": ev["about"][:2000], "location_text": ev["loc"], "attendance_text": ev["attendance_text"],
                        "fees_text": v.get("fees_text"), "deadline_text": v.get("deadline_text"), "application_count": v.get("app_count")},
            })
    return rows, errors
