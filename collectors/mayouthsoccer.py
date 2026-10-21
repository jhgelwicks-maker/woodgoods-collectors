"""Mass Youth Soccer sanctioned tournaments: plain HTML table, read directly (no AI, no API key).

The page lays tournaments out in tables, one tournament per column. Within a block of rows (blocks are separated
by a logo row) the first text row is the names, then the dates, then labelled cells ("Location:", "Contact:",
"Age Groups:", "Teams:") and a "Tournament Website" link. Each tournament's website is opened once; if it fails to
load or looks like an error page, the row records website_info = NO INFO AVAILABLE."""
import re, time, datetime
from bs4 import BeautifulSoup
from . import common
from .common import log

URL = "https://mayouthsoccer.org/events-and-programs/tournaments/"
NO_INFO = "NO INFO AVAILABLE"
ORDINAL = re.compile(r"(\d)(st|nd|rd|th)\b", re.I)          # "October 11th" -> "October 11"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ERROR_TITLE = re.compile(r"\b(404|not found|error|forbidden|access denied|unavailable)\b", re.I)

def _text(cell):
    return " ".join(cell.stripped_strings)

def _label(text, label):
    """'Location: Natick, MA' -> 'Natick, MA'. Tolerates a missing colon ('Teams S1, S2')."""
    m = re.match(rf"\s*{label}\b\s*:?\s*(.*)$", text, re.I | re.S)
    return m.group(1).strip() if m else None

def parse_tournaments(page_html):
    """One dict per tournament: name, dates_text, location, contact, age_groups, team_levels, website."""
    soup = BeautifulSoup(page_html, "lxml")
    out = []
    for table in soup.find_all("table"):
        if "Location:" not in table.get_text(): continue
        block = {}                                   # column index -> tournament in the current block
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if not any(_text(c) for c in cells):    # logo / spacer row: a new block of tournaments starts
                block = {}; continue
            for i, cell in enumerate(cells):
                text = _text(cell)
                if not text: continue
                t = block.get(i)
                if t is None:                        # first text row of a block = tournament names
                    t = block[i] = {"name": text}; out.append(t); continue
                link = cell.find("a", href=True)
                if link and "website" in text.lower(): t["website"] = link["href"].strip()
                elif (v := _label(text, "Location")) is not None: t["location"] = v
                elif (v := _label(text, "Contact")) is not None: t["contact"] = v
                elif (v := _label(text, "Age Groups")) is not None: t["age_groups"] = v
                elif (v := _label(text, "Teams")) is not None: t["team_levels"] = v
                elif "dates_text" not in t: t["dates_text"] = text
    return out

def website_info(url):
    """Title and a short summary of the tournament's own page, or NO INFO AVAILABLE if it won't load."""
    if not url: return {"website_info": NO_INFO}
    page = common.polite_get(url, timeout=25, tries=2)
    if not page: return {"website_info": NO_INFO}
    soup = BeautifulSoup(page, "lxml")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    desc = (meta.get("content") or "").strip() if meta else ""
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer"]): tag.decompose()
    clean = lambda s: re.sub(r"\s+", " ", re.sub(r"[​-‍﻿]", "", s)).strip()
    text = clean(soup.get_text(" ", strip=True))
    if len(text) < 200 or ERROR_TITLE.search(title): return {"website_info": NO_INFO}
    para = next((p for p in (clean(x.get_text(" ", strip=True)) for x in soup.find_all("p")) if len(p) >= 60), "")
    return {"website_info": "ok", "website_title": title[:200] or None, "website_summary": (desc or para or text)[:300]}

def collect():
    page = common.polite_get(URL)
    if not page: return [], 1
    items = parse_tournaments(page)
    if not items:
        log.error("mayouthsoccer: no tournaments found on the page; its layout may have changed")
        return [], 1
    rows, errors, with_info = [], 0, 0
    today = datetime.date.today()
    for it in items:
        dates = ORDINAL.sub(r"\1", it.get("dates_text") or "")
        s, e = common.parse_date_range(dates, default_year=today.year)
        if s and s < today - datetime.timedelta(days=30): s, e = common.parse_date_range(dates, default_year=today.year + 1)
        if not s: errors += 1
        loc = it.get("location") or ""
        city, _, st = loc.rpartition(",") if "," in loc else (loc, "", "MA")
        city, st = city.strip(), (st.strip() or "MA")
        email = EMAIL.search(it.get("contact") or "")
        info = website_info(it.get("website"))
        with_info += info["website_info"] != NO_INFO
        time.sleep(0.5)
        rows.append({
            "source": "mayouthsoccer", "source_event_id": re.sub(r"[^a-z0-9]+", "-", f"{it['name']}-{s}".lower()),
            "name": it["name"], "sport": "soccer", "organizer_name": None,
            "start_date": s, "end_date": e, "city_states": [f"{city}, {st}"] if city else [],
            "vendor_contact_email": email.group(0) if email else None, "vendor_contact_name": None,
            "age_groups": it.get("age_groups"), "listing_url": URL, "official_url": it.get("website"),
            "raw": {**it, **info},
        })
    log.info("mayouthsoccer: %d tournaments, %d websites with info, %d without", len(rows), with_info, len(rows) - with_info)
    return rows, errors
