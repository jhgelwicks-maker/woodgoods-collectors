"""Fairs and Festivals (fairsandfestivals.net) — plain HTML, one page per state.
Free fields: name, start date, city/state/venue, description, vendor types.
End date, street address and attendance (when stated) come from the free detail page.
Vendor fees, contact, expected attendance and application deadlines are PAID-MEMBER fields:
set FF_COOKIE (the site's login cookie string) as a secret to unlock them; without it they stay null."""
import os, re, html, time, datetime
from concurrent.futures import ThreadPoolExecutor
from . import common
from .common import log

STATES = ["AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO",
          "MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY","DC"]
BASE = "https://www.fairsandfestivals.net"
EVENT_RE = re.compile(r'<div class="event">(.*?)<div class="clear"></div>\s*</div>', re.S)
ATTEND_RE = re.compile(r'(\d{1,3}(?:,\d{3})+|\d{4,6})\s*\+?\s*(?:expected\s+)?(?:attendees|attendance|visitors|people|guests|shoppers)', re.I)

def _txt(s): return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()

def _field(detail, label):
    m = re.search(re.escape(label) + r"\s*</td>\s*<td[^>]*>(.*?)</td>", detail, re.S | re.I)
    v = _txt(m.group(1)) if m else None
    return None if not v or "Paid Members Only" in v else v

def parse_listing(page_html, state):
    out = []
    for block in EVENT_RE.findall(page_html):
        name = _txt(re.search(r"<h4>(.*?)</h4>", block, re.S).group(1))
        m = re.search(r'<span class="month">(\w+)</span>\s*(\d+)\s*<span class="year">(\d{4})</span>', block)
        start, _ = common.parse_date_range(f"{m[1]} {m[2]}, {m[3]}") if m else (None, None)
        city = re.search(r'<span class="city">(.*?)</span>', block); st = re.search(r'<span class="state">(.*?)</span>', block)
        loc = re.search(r'<td class="location">(.*?)</td>', block, re.S)
        venue = _txt(re.sub(r"<span.*?</span>,?", "", loc.group(1), flags=re.S)) if loc else None
        desc_m = re.search(r"Description:</td>\s*<td>(.*?)<a ", block, re.S)
        desc = _txt(desc_m.group(1)) if desc_m else ""
        link = re.search(r'href="(/events/details/[^"]+)"', block)
        vtypes = [_txt(x) for x in re.findall(r'<li class="allowed[^"]*">(.*?)</li>', block)]
        out.append({"name": name, "start": start, "city": _txt(city[1]) if city else None, "state": _txt(st[1]) if st else state,
                    "venue": venue or None, "desc": desc, "url": BASE + link[1] if link else None, "vendor_types": vtypes})
    return out

def enrich_detail(url):
    d = common.polite_get(url, headers={"cookie": os.environ.get("FF_COOKIE", "")})
    if not d: return {}
    dm = re.search(r'id="event-date">.*?<p>(.*?)</p>', d, re.S)
    dates = _txt(dm.group(1)) if dm else ""
    s, e = None, None
    parts = [p.strip() for p in dates.split("-") if p.strip()]
    if parts:
        s, _ = common.parse_date_range(parts[0]); e, _ = common.parse_date_range(parts[-1]); e = e or s
    addr_m = re.search(r'id="event-location">.*?<p>(.*?)</p>', d, re.S)
    desc_m = re.search(r'id="event-description">.*?<p>(.*?)</p>', d, re.S)
    full_desc = _txt(desc_m.group(1)) if desc_m else ""
    att = ATTEND_RE.search(full_desc)
    fees = _field(d, "Vendor Booth Fees:")
    return {"start": s, "end": e, "address": _txt(addr_m.group(1)).replace("Get Directions »", "").strip() if addr_m else None,
            "description": full_desc, "attendance_text": att.group(0) if att else None,
            "attendance": int(att.group(1).replace(",", "")) if att else None,
            "expected_attendance": _field(d, "Expected Attendance:"), "website": _field(d, "Event Website:"),
            "contact_name": _field(d, "Name:"), "contact_email": _field(d, "Email:"), "contact_phone": _field(d, "Phone:"),
            "years": _field(d, "Years In Existence:"), "fees_text": fees, "deadline_text": _field(d, "Application Due dates:"),
            "vendor_count": _field(d, "Estimated Total Number of Vendors:"), "booth_size": _field(d, "Smallest Booth Size:")}

def collect(states=STATES, detail=True):
    rows, errors = [], 0
    for st in states:
        page = common.polite_get(f"{BASE}/states/{st}/")
        if not page: errors += 1; continue
        items = parse_listing(page, st)
        log.info("fairsandfestivals %s: %d listings", st, len(items))
        with ThreadPoolExecutor(6) as ex:
            dets = list(ex.map(lambda it: enrich_detail(it["url"]) if (detail and it["url"]) else {}, items))
        for it, det in zip(items, dets):
            start = det.get("start") or it["start"]; end = det.get("end") or start
            fee_lo = fee_hi = None
            if det.get("fees_text"):
                nums = [float(x.replace(",", "")) for x in re.findall(r"\$\s?([\d,]+(?:\.\d+)?)", det["fees_text"])]
                if nums: fee_lo, fee_hi = min(nums), max(nums)
            tags = [t.lower() for t in it["vendor_types"]]
            desc = det.get("description") or it["desc"]
            for kw in ("family", "kids", "children", "21+", "adult", "craft", "art", "music", "food", "beer", "wine", "holiday", "harvest", "pumpkin"):
                if re.search(r"\b" + re.escape(kw) + r"\b", desc, re.I) and kw not in tags: tags.append(kw)
            rows.append({
                "source": "fairsandfestivals", "source_event_id": (it["url"] or f"{it['name']}-{start}").rsplit("/", 1)[-1],
                "name": it["name"], "sport": "festival", "organizer_name": None,
                "start_date": start, "end_date": end, "venue_name": it["venue"],
                "city_states": [f"{it['city']}, {it['state']}"] if it["city"] else [],
                "vendor_contact_name": det.get("contact_name"), "vendor_contact_email": det.get("contact_email"),
                "listing_url": it["url"], "official_url": det.get("website"),
                "vendor_fee_min": fee_lo, "vendor_fee_max": fee_hi, "expected_attendance": det.get("attendance"),
                "tags": tags, "category": "festival",
                "raw": {"description": desc, "address": det.get("address"), "vendor_types": it["vendor_types"],
                        "attendance_text": det.get("attendance_text") or det.get("expected_attendance"),
                        "fees_text": det.get("fees_text"), "deadline_text": det.get("deadline_text"),
                        "vendor_count": det.get("vendor_count"), "booth_size": det.get("booth_size"), "years": det.get("years"),
                        "member_fields_unlocked": bool(os.environ.get("FF_COOKIE"))},
            })
    return rows, errors
