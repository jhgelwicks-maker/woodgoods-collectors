"""Crawl Organizer: read one organizer's own website -> upcoming events + the organizer record.

Stage B  find the right pages: from the homepage, follow links whose words say events/tournaments/schedule,
         contact/about, or vendor/sponsor. Same site only (platform links like Tourney Machine are recorded).
Stage C  extract: schema.org Event data first, then date-anchored blocks on listing pages, then each event's own
         page for venue/address/ages/gender/teams/times. Claude only with --ai, only for a listing page that
         clearly has dates but no readable event blocks.

Event rows use the shape the ingest endpoint already accepts (source "org-<slug>"), with venues[] so the app's
venue job maps facilities. The organizer record (website, main email/phone, vendor page, vendor contact) stays in a
local file until the app accepts an organizers payload. Contacts are never written inside this repo.

    python -m collectors.organizers.crawl --org apex                  # one organizer, writes ~/WoodGoodsAI/crawl/<date>/
    python -m collectors.organizers.crawl --known                     # the 14 organizers with scrapers (accuracy check)
    python -m collectors.organizers.crawl --list                      # organizer list (stage A) with sizes, no crawling

JavaScript-built pages are rendered with Playwright when it is installed (pip install playwright && python -m playwright
install chromium); --no-render turns that off. Without Playwright those sites simply return fewer events.
"""
from __future__ import annotations
import os, re, sys, json, time, argparse, pathlib, datetime as dt, urllib.parse
from urllib import robotparser

import requests
from bs4 import BeautifulSoup

from ..common import log
from . import extract as X
from .registry import build as build_registry, slugify

UA = {"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0 Safari/537.36",
      "accept": "text/html,application/xhtml+xml,*/*;q=0.8", "accept-language": "en-US,en;q=0.9"}
OUT_ROOT = pathlib.Path(os.path.expanduser("~/WoodGoodsAI/crawl"))
EXPORT_DIR = pathlib.Path(os.path.expanduser("~/WoodGoodsAI/backup/db/current"))

EVENT_HINT = re.compile(r"\b(events?|tournaments?|showcases?|schedule|calendar|upcoming|register|registration|competitions?|invitationals?|festivals?)\b", re.I)
CONTACT_HINT = re.compile(r"\b(contact|about|staff|our[-\s]team|who[-\s]we[-\s]are)\b", re.I)
VENDOR_HINT = re.compile(r"\b(vendors?|vending|sponsors?|sponsorship|exhibitors?|booths?|partners?)\b", re.I)
SKIP_LINK = re.compile(r"(facebook|instagram|twitter|x\.com|tiktok|youtube|linkedin|maps\.google|goo\.gl|apple\.com|mailto:|tel:|\.pdf$|\.jpe?g$|\.png$|"
                       r"/cart|/login|/account|/wp-admin|/privacy|/terms|/shop|/store|/product|/feed|#)", re.I)
PLATFORM = re.compile(r"(tourneymachine|leagueapps|sportsengine|sportngin|exposureevents|gotsport|playbook365|eventbrite|teamsnap|laxnumbers)\.", re.I)
NOT_EVENT = re.compile(r"\b(camps?|clinics?|training|tryouts?|lessons?|private|academy\s+program|membership|newsletter|blog|news|recap|results?|standings)\b", re.I)
MORE_LINK = re.compile(r"^(register|registration|more info|learn more|find (?:out|our) more|see (?:more|details)|event details|details|view|info|"
                       r"read more|click here|sign up|apply|buy tickets|get tickets|book now|join|enroll)\b", re.I)


def tidy_caps(s):
    """'NJ FALL CLASSIC' -> 'NJ Fall Classic'; mixed-case names are left as written."""
    if not s or not s.isupper(): return s
    small = {"of", "at", "in", "by", "to", "and", "the", "for", "on"}
    words = s.lower().split()
    return " ".join(w if i and w in small else (w.upper() if len(w) <= 2 and w.isalpha() else w.capitalize()) for i, w in enumerate(words))
# rows that are page labels, not events ("Location:", "Attending College Coaches", "Dates")
JUNK_NAME = re.compile(r"(:\s*$)|^(location|locations|dates?|when|where|venue|address|schedule|times?|cost|price|fees?|faq|contact|"
                       r"(attending\s+)?college\s+coaches(\s+attending)?|coaches|sponsors?|directions|parking|hotels?|lodging)\b", re.I)


# a page heading, not an event, when it is the whole name ("Events", "Upcoming Events"); "Tournament of Champions" stays
HEADING_NAME = re.compile(r"^(?:all\s+|upcoming\s+|our\s+|featured\s+)?(?:events?|tournaments?|showcases?|calendar|schedule|programs?)$", re.I)


def clean_name(name):
    """'Laxtoberfest Oct. 25th, 2026' -> 'Laxtoberfest': names carry the date on listing pages but not on event pages."""
    n = X.DATE_TEXT.sub(" ", tidy_caps(X.clean(name)) or "")
    n = re.sub(r"\b(?:19|20)\d\d\b\s*$", " ", n)
    n = re.sub(r"^\s*(?:19|20)\d\d\s+", "", n)        # "2026 Boys Fall Grand Prix" -> "Boys Fall Grand Prix", as the scrapers do
    return re.sub(r"\s+", " ", n).strip(" -–—|:,·") or X.clean(name)


def name_key(name):
    return re.sub(r"[^a-z0-9]+", " ", clean_name(name).lower()).strip()


class Fetcher:
    """Polite GET with a per-run cache and robots.txt. rendered(url) loads the page in a headless Chromium (Playwright)
    so JavaScript-built lists exist; one browser per Fetcher, reused across pages, closed by close()."""
    def __init__(self, max_pages=40, sleep=1.0, render=False):
        self.s = requests.Session(); self.s.headers.update(UA)
        self.cache, self.robots, self.max_pages, self.sleep = {}, {}, max_pages, sleep
        self.render = render and playwright_available()
        self.pages = 0
        self._pw = self._browser = None

    def allowed(self, url):
        p = urllib.parse.urlparse(url); base = f"{p.scheme}://{p.netloc}"
        if base not in self.robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self.s.get(base + "/robots.txt", timeout=15)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            self.robots[base] = rp
        return self.robots[base].can_fetch("*", url)

    def get(self, url):
        if url in self.cache: return self.cache[url]
        if self.pages >= self.max_pages or not self.allowed(url):
            return None
        self.pages += 1
        html = None
        try:
            r = self.s.get(url, timeout=25, allow_redirects=True)
            if r.status_code == 200 and "html" in r.headers.get("content-type", "html"): html = r.text
            else: log.info("GET %s -> %s", url, r.status_code)
        except requests.RequestException as e:
            log.info("GET %s failed: %s", url, e)
        if html and self.render and len(BeautifulSoup(html, "lxml").get_text(" ", strip=True)) < 600:
            html = self.rendered(url) or html             # almost nothing in the page: it is built by JavaScript
        self.cache[url] = html
        time.sleep(self.sleep)
        return html

    def rendered(self, url):
        """The page after its JavaScript has run (event cards and their links filled in), or None."""
        key = ("rendered", url)
        if key in self.cache: return self.cache[key]
        if not self.render or self.pages >= self.max_pages or not self.allowed(url): return None
        self.pages += 1
        html = None
        try:
            if self._browser is None:
                from playwright.sync_api import sync_playwright
                self._pw = sync_playwright().start()
                self._browser = self._pw.chromium.launch()
            pg = self._browser.new_page(user_agent=UA["user-agent"])
            try:
                pg.goto(url, timeout=45000, wait_until="networkidle")
                pg.wait_for_timeout(1500)                     # late-loading cards
                html = pg.content()
            finally:
                pg.close()
        except Exception as e:  # noqa: BLE001
            log.info("render failed %s: %s", url, e)
        self.cache[key] = html
        return html

    def close(self):
        try:
            if self._browser: self._browser.close()
            if self._pw: self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._pw = self._browser = None


def playwright_available():
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def site_of(url):
    h = urllib.parse.urlparse(url).netloc.lower().split(":")[0]
    parts = h.removeprefix("www.").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def links(html, base):
    """[(absolute url, anchor text)] for every <a href>, deduped, skipping junk."""
    soup = BeautifulSoup(html or "", "lxml"); out, seen = [], set()
    for a in soup.find_all("a", href=True):
        u = urllib.parse.urljoin(base, a["href"].strip()).split("#")[0]
        if not u.startswith("http") or SKIP_LINK.search(u) or u in seen: continue
        seen.add(u); out.append((u, X.clean(a.get_text(" ", strip=True))[:120]))
    return out


def discover(home_html, home_url):
    """-> {'events': [...], 'contact': [...], 'vendor': [...], 'platforms': [...]} ranked page lists."""
    site = site_of(home_url); res = {"events": [], "contact": [], "vendor": [], "platforms": []}
    for u, text in links(home_html, home_url):
        if PLATFORM.search(u):
            if u not in res["platforms"]: res["platforms"].append(u)
            continue
        if site_of(u) != site: continue
        path = urllib.parse.urlparse(u).path.lower().replace("-", " ").replace("_", " ")
        key = f"{path} {text}"
        if VENDOR_HINT.search(key): res["vendor"].append(u)
        elif CONTACT_HINT.search(key): res["contact"].append(u)
        elif EVENT_HINT.search(key) and not NOT_EVENT.search(key): res["events"].append(u)
    # list pages before single-event pages: "/trilogy-events-for-teams/" lists everything, "/events/autumn-drop/" is one event
    def rank(u):
        p = urllib.parse.urlparse(u)
        depth = len([s for s in p.path.split("/") if s])
        return (depth, bool(p.query), 0 if re.search(r"tournament|showcase|event", u, re.I) else 1, len(u))
    res["events"].sort(key=rank)
    return res


def _text(soup):
    for t in soup(["script", "style", "noscript", "svg"]): t.decompose()
    return X.clean(soup.get_text(" ", strip=True))


def _block_title(el, date_str):
    # headings first, then bold, then links: a card that is one big link must not hand over its whole text as the name
    for tags in (["h1", "h2", "h3", "h4", "h5"], ["strong", "b"], ["a"]):
        for tag in el.find_all(tags):
            t = X.clean(tag.get_text(" ", strip=True))
            if date_str and date_str in t: t = t.split(date_str)[0].strip(" -–|:,")    # name ends where the date starts
            if len(t) >= 5 and not MORE_LINK.match(t) and not X.DATE_TEXT.fullmatch(t) and t != date_str and not X.EMAIL.search(t):
                return t[:140]
    t = X.clean(el.get_text(" ", strip=True)).split(date_str)[0]
    t = X.WEEKDAY.sub(" ", t).strip(" -–|:,")              # "Saturday, November 7" has no name before the date
    if len(t) < 5 or MORE_LINK.match(t) or JUNK_NAME.search(t): return None
    return t[:140]


GENERIC_PATH = re.compile(r"^(uploads?|images?|img|media|files?|assets?|event logos?|logos?|wp-content|\d+|[a-f0-9]{8,})$", re.I)


def name_from_card(el):
    """A name for a card whose name is only in its logo: image alt/title, else the logo's folder or file name
    ('.../Event Logos/Fall Brawl/FB_Primary_FC.png' -> 'Fall Brawl'), else the button link's last path part."""
    for img in el.find_all("img"):
        for attr in ("alt", "title"):
            v = X.clean(img.get(attr))
            if len(v) >= 4 and not re.search(r"\b(logo|image|icon|photo|banner)\b", v, re.I): return clean_name(v)
        parts = [urllib.parse.unquote(p) for p in urllib.parse.urlparse(img.get("src") or "").path.split("/") if p]
        for p in reversed(parts[:-1]):                 # folders, innermost first
            if not GENERIC_PATH.match(p) and re.search(r"[A-Za-z]{3}", p): return clean_name(p.replace("_", " ").replace("-", " "))
    a = el.find("a", href=True)
    if a:
        slug = [p for p in urllib.parse.urlparse(a["href"]).path.split("/") if p]
        if slug and re.search(r"[A-Za-z]{4}", slug[-1]): return clean_name(slug[-1].replace("-", " ").replace("_", " ").title())
    return None


def page_title(html):
    """An event page's own name: its first heading, else <title> without the site name after ' | ' or ' - '."""
    soup = BeautifulSoup(html or "", "lxml")
    h = soup.find(["h1", "h2"])
    t = X.clean(h.get_text(" ", strip=True)) if h else ""
    if len(t) < 4 and soup.title: t = re.split(r"\s+[|–—-]\s+", X.clean(soup.title.get_text()))[0]
    t = clean_name(t)
    return t if 4 <= len(t) <= 100 and not JUNK_NAME.search(t) and not MORE_LINK.match(t) else None


def _detail_link(el, page_url):
    site = site_of(page_url)
    for a in el.find_all("a", href=True):
        u = urllib.parse.urljoin(page_url, a["href"]).split("#")[0]
        if u.startswith("http") and not SKIP_LINK.search(u) and u.rstrip("/") != page_url.rstrip("/") and (site_of(u) == site or PLATFORM.search(u)):
            return u
    return None


def event_blocks(html, page_url, today=None):
    """Upcoming events on a listing page: schema.org Events, else the smallest page blocks that carry a date and a title."""
    today = today or dt.date.today()
    out = []
    for e in X.jsonld_events(html):
        if e["start"] and e["name"]:
            out.append({**e, "name": clean_name(e["name"]), "page": page_url, "detail": e.get("url"), "text": e.get("description") or "", "how": "schema.org"})
    if out: return out
    soup = BeautifulSoup(html or "", "lxml")
    for t in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]): t.decompose()
    chosen = []
    for node in soup.find_all(string=X.DATE_TEXT):
        el = node.parent
        date_str = X.DATE_TEXT.search(X.clean(node)).group(0)
        for _ in range(6):                              # climb until the block holds a real name (not just the date and a button)
            txt = X.clean(el.get_text(" ", strip=True))
            if el.name in ("article", "li", "tr") or _block_title(el, date_str): break
            if el.parent is None or el.parent.name in ("body", "html", "[document]"): break
            # never climb into a box that holds another event's title: two same-day events side by side must stay two events
            heads = len(el.find_all(["h1", "h2", "h3", "h4", "h5"]))
            if heads and len(el.parent.find_all(["h1", "h2", "h3", "h4", "h5"])) > heads: break
            # a card with a logo or a button is one event's box even when its name is only in the logo
            if (el.find("img") or el.find("a", href=True)) and \
                    len(X.DATE_TEXT.findall(X.clean(el.parent.get_text(" ", strip=True)))) > len(X.DATE_TEXT.findall(txt)): break
            el = el.parent
        # widen from the name to the whole card (location, ages, address lines) while the card holds no other event
        for _ in range(3):
            par = el.parent
            if par is None or par.name in ("body", "html", "[document]"): break
            ptxt = X.clean(par.get_text(" ", strip=True))
            if (len(ptxt) > 800 or len(X.DATE_TEXT.findall(ptxt)) > len(X.DATE_TEXT.findall(X.clean(el.get_text(" ", strip=True))))
                    or len(par.find_all(["h1", "h2", "h3", "h4", "h5"])) > max(1, len(el.find_all(["h1", "h2", "h3", "h4", "h5"])))):
                break
            el = par
        if len(X.clean(el.get_text(" ", strip=True))) > 1500: continue
        if any(el is c or el in c.descendants for c in chosen): continue
        chosen = [c for c in chosen if c not in el.descendants] + [el]
    for el in chosen:
        txt = X.clean(el.get_text(" ", strip=True))
        m = X.DATE_TEXT.search(txt)
        dates = X.find_dates(txt, today)
        if not m or not dates: continue
        name = _block_title(el, m.group(0))
        name = clean_name(name) if name else None
        needs_name = False
        if not name:                                   # name only in the logo: take it from the image, fix it from the event page later
            name, needs_name = name_from_card(el), True
        if not name or NOT_EVENT.search(name) or JUNK_NAME.search(name) or HEADING_NAME.match(name) or re.fullmatch(r"[a-z0-9._\-]{10,}", name): continue   # all-lowercase "apexgirlslaxevents" is a logo; "Laxtoberfest" is an event
        if len(name.split()) > 10: continue            # a sentence ("Melt the ice and prep your squad ..."), not an event name
        out.append({"name": name, "start": dates[0][0], "end": dates[0][1], "page": page_url, "detail": _detail_link(el, page_url),
                    "text": txt, "how": "page block", "needs_name": needs_name})
    return out


def single_event(html, page_url, today=None):
    """An event's own page: title + first date + everything the text says about venue, ages, gender, teams, times."""
    soup = BeautifulSoup(html or "", "lxml")
    title = X.clean((soup.find("h1") or soup.find("title") or soup).get_text(" ", strip=True))[:140]
    js = X.jsonld_events(html)
    if js and js[0]["start"]: return {**js[0], "page": page_url, "text": js[0].get("description") or _text(soup), "how": "schema.org"}
    text = _text(soup)
    dates = X.find_dates(text[:4000], today)
    if not dates: return None
    title = clean_name(title)
    if not title or JUNK_NAME.search(title) or HEADING_NAME.match(title) or NOT_EVENT.search(title): return None
    return {"name": title, "start": dates[0][0], "end": dates[0][1], "page": page_url, "text": text, "how": "event page"}


def _first(fn, *texts):
    for t in texts:
        v = fn(t) if t else None
        if v: return v
    return None


def fields_from_text(ev):
    """Fill venue/address/city/ages/gender/teams/times without overwriting what structured data gave.
    The event's own listing block is trusted before its detail page, whose menus mention every other event."""
    block, detail = ev.get("text") or "", ev.get("detail_text") or ""
    season = (ev.get("start") or dt.date.today()).year
    ev["address"] = ev.get("address") or _first(lambda t: next(iter(X.find_addresses(t)), None), block, detail)
    src = block if ev["address"] and ev["address"] in block else detail
    ev["venue_name"] = (ev.get("venue_name") or (X.venue_before(src, ev["address"]) if ev["address"] else None)
                        or _first(lambda t: next(iter(X.find_venue_names(t)), None), block, detail))
    cs = X.find_city_states(ev["address"] or "") + X.find_city_states(block) + X.find_city_states(detail)
    if ev.get("city") and ev.get("state"): cs = [f"{ev['city']}, {ev['state']}"] + cs
    ev["city_states"] = list(dict.fromkeys((ev.get("city_states") or []) + cs))[:4]
    label = None
    for t in (block, detail):
        label, lo, hi, g = X.ages_gender(t, season)
        if label: break
    ev["age_groups"], ev["grad_year_min"], ev["grad_year_max"] = (label, lo, hi) if label else (None, None, None)
    name_g = X.ages_gender(ev.get("name", ""), season)[3]
    ev["gender"] = name_g if name_g in ("boys", "girls") else _first(lambda t: X.ages_gender(t, season)[3], block, detail)
    ev["team_count"] = _first(X.find_team_count, block, detail)
    ev["start_time"], ev["end_time"] = X.find_times(detail) if X.find_times(detail)[0] else X.find_times(block)
    return ev


AI_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["events"], "properties": {"events": {"type": "array", "items": {
    "type": "object", "additionalProperties": False,
    "required": ["name", "start_date", "end_date", "venue", "city_state", "ages", "gender", "url"],
    "properties": {"name": {"type": "string"}, "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                   "end_date": {"type": "string", "description": "YYYY-MM-DD, same as start if one day"},
                   "venue": {"type": "string", "description": "facility name and street address if shown, else empty"},
                   "city_state": {"type": "string", "description": "City, ST or empty"}, "ages": {"type": "string"},
                   "gender": {"type": "string", "description": "boys | girls | both | empty"}, "url": {"type": "string"}}}}}}


def ai_events(text, page_url, org_name, client, model):
    """Last resort for a listing page with dates but no readable blocks: Claude reads a trimmed excerpt."""
    lines = [l for l in re.split(r"(?<=[.!?])\s+|\s{2,}", text) if X.DATE_TEXT.search(l) or re.search(r"tournament|showcase|invitational|cup|classic", l, re.I)]
    excerpt = "\n".join(lines)[:8000]
    if not excerpt: return []
    msg = client.messages.create(model=model, max_tokens=4000, output_config={"effort": "low", "format": {"type": "json_schema", "schema": AI_SCHEMA}},
        system="List the upcoming youth lacrosse tournaments on this organizer's page. Only events actually listed in the text; never invent. Empty strings for unknown fields.",
        messages=[{"role": "user", "content": f"Organizer: {org_name}\nPage: {page_url}\n\n{excerpt}"}])
    data = json.loads("".join(b.text for b in msg.content if b.type == "text") or "{}")
    out = []
    for e in data.get("events", []):
        try: s = dt.date.fromisoformat(e["start_date"]); en = dt.date.fromisoformat(e["end_date"] or e["start_date"])
        except (ValueError, KeyError): continue
        out.append({"name": X.clean(e["name"]), "start": s, "end": en, "page": page_url, "detail": e.get("url") or None,
                    "venue_name": e.get("venue") or None, "city_states": [e["city_state"]] if e.get("city_state") else [],
                    "text": f"{e.get('ages', '')} {e.get('gender', '')} {e.get('venue', '')}", "how": "ai"})
    return out


def crawl_organizer(org, fetch=None, today=None, ai_client=None, ai_model="claude-opus-5-5", max_details=25):
    """-> {'organizer': record, 'events': [event dicts], 'pages': n, 'notes': [...]}"""
    today = today or dt.date.today()
    fetch = fetch or Fetcher()
    home = org["website"]
    rec = {"slug": org["slug"], "name": org["name"], "canon": org.get("canon"), "website": home, "main_email": None, "main_phone": None,
           "vendor_page": None, "vendor_contact": {"name": None, "email": None, "phone": None}, "platforms": [], "sources": {},
           "crawled_at": dt.datetime.utcnow().isoformat() + "Z"}
    notes, events = [], []
    home_html = fetch.get(home)
    if not home_html:
        return {"organizer": rec, "events": [], "pages": fetch.pages, "notes": ["homepage did not load"]}
    found = discover(home_html, home)
    rec["platforms"] = found["platforms"][:10]

    # events: the homepage itself plus the best few listing pages
    raw = []
    can_render = getattr(fetch, "render", False)
    pages = list(dict.fromkeys((org.get("start_pages") or []) + [home] + found["events"][:6]))
    for page in pages:
        html = home_html if page == home else fetch.get(page)
        if not html: continue
        blocks = event_blocks(html, page, today)
        if not blocks and can_render:                 # list built by JavaScript: read the page as a browser shows it
            rhtml = fetch.rendered(page)
            if rhtml:
                html = rhtml
                blocks = event_blocks(html, page, today)
        if not blocks and ai_client and len(X.find_dates(_text(BeautifulSoup(html, "lxml")), today)) >= 2:
            try: blocks = ai_events(_text(BeautifulSoup(html, "lxml")), page, org["name"], ai_client, ai_model)
            except Exception as e: notes.append(f"ai failed on {page}: {e}")  # noqa: BLE001
        if not blocks and page != home:
            one = single_event(html, page, today)
            if one: blocks = [one]
        raw += blocks
    seen = {}                                       # (name, start) -> event; the same event on the homepage and a listing page merges
    for ev in raw:
        if not ev.get("start") or JUNK_NAME.search(ev.get("name") or ""): continue
        if (ev.get("end") or ev["start"]) < today - dt.timedelta(days=1) or ev["start"] > today + dt.timedelta(days=550): continue
        key = (name_key(ev["name"]), ev["start"])
        if key in seen:
            keep = seen[key]
            if (ev.get("end") or ev["start"]) > (keep.get("end") or keep["start"]): keep["end"] = ev["end"]   # "Nov 14 / 15" beats "Nov 14"
            for k in ("detail", "venue_name", "address", "city", "state", "lat", "long"):
                if ev.get(k) and not keep.get(k): keep[k] = ev[k]
            if len(ev.get("text") or "") > len(keep.get("text") or ""): keep["text"] = ev["text"]
            continue
        seen[key] = ev; events.append(ev)

    # each event's own page fills venue/address/ages/gender/teams/times
    for ev in events[:max_details]:
        detail = ev.get("detail")
        if detail and site_of(detail) == site_of(home) and detail != ev["page"]:
            html = fetch.get(detail)
            if html and can_render and not X.find_dates(_text(BeautifulSoup(html, "lxml"))[:4000], today):
                html = fetch.rendered(detail) or html
            if html:
                one = single_event(html, detail, today)
                ev["detail_text"] = one["text"] if one else _text(BeautifulSoup(html, "lxml"))
                for k in ("venue_name", "address", "city", "state", "lat", "long"):
                    if one and one.get(k) and not ev.get(k): ev[k] = one[k]
                if ev.get("needs_name"):
                    title = page_title(html)
                    if title: ev["name"] = title
                ev["page"] = detail
    for ev in events:
        fields_from_text(ev)

    # organizer contacts: homepage + contact pages; vendor contact only when the organizer qualifies
    contact_html = [(home, home_html)] + [(u, fetch.get(u)) for u in found["contact"][:2]]
    for u, html in contact_html:
        if not html: continue
        emails = X.find_emails(html); phones = X.find_phones(BeautifulSoup(html, "lxml").get_text(" "))
        own = [e for e in emails if e.split("@")[1].endswith(site_of(home))]
        if not rec["main_email"] and (own or emails): rec["main_email"] = (own or emails)[0]; rec["sources"]["main_email"] = u
        if not rec["main_phone"] and phones: rec["main_phone"] = phones[0]; rec["sources"]["main_phone"] = u
    if found["vendor"]:
        rec["vendor_page"] = found["vendor"][0]; rec["sources"]["vendor_page"] = home
        if org.get("qualifies_vendor"):
            vhtml = fetch.get(found["vendor"][0])
            if vhtml:
                vc = X.vendor_contact(BeautifulSoup(vhtml, "lxml").get_text(" "))
                rec["vendor_contact"] = vc
                if any(vc.values()): rec["sources"]["vendor_contact"] = found["vendor"][0]
    elif org.get("qualifies_vendor"):
        notes.append("no vendor/sponsor page linked from the homepage")
    return {"organizer": rec, "events": events, "pages": fetch.pages, "notes": notes}


def to_row(org, ev):
    """An event in the ingest rows shape. venues[] lets the app's venue job map it to an existing facility."""
    city = (ev.get("city_states") or [""])[0]
    venue = None
    if ev.get("venue_name") or ev.get("address"):
        venue = {"venue_name": ev.get("venue_name") or ev.get("address"), "address": ev.get("address"),
                 "lat": ev.get("lat"), "long": ev.get("long")}
    return {"source": f"org-{org['slug']}", "source_event_id": slugify(f"{ev['name']}-{ev['start']}-{city}"),
            "name": ev["name"], "sport": "lacrosse", "organizer_name": org["name"],
            "start_date": ev["start"], "end_date": ev.get("end") or ev["start"], "city_states": ev.get("city_states") or [],
            "venue_name": ev.get("venue_name"), "venues": [venue] if venue else None, "team_count": ev.get("team_count"),
            "age_groups": ev.get("age_groups"), "grad_year_min": ev.get("grad_year_min"), "grad_year_max": ev.get("grad_year_max"),
            "listing_url": ev.get("page"), "official_url": ev.get("page"),
            "raw": {"gender": ev.get("gender"), "start_time": ev.get("start_time"), "end_time": ev.get("end_time"),
                    "extracted_by": ev.get("how"), "organizer_website": org.get("website")}}


def _json_default(o):
    return o.isoformat() if isinstance(o, (dt.date, dt.datetime)) else str(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", nargs="*", default=[], help="organizer slugs (see --list)")
    ap.add_argument("--known", action="store_true", help="the 14 organizers that have scrapers")
    ap.add_argument("--with-websites", action="store_true", help="every organizer with a known website")
    ap.add_argument("--list", action="store_true", help="print the organizer list and stop")
    ap.add_argument("--export-dir", default=str(EXPORT_DIR), help="app data export (tools/backup_db.py output)")
    ap.add_argument("--out", default=None, help="output folder (default ~/WoodGoodsAI/crawl/<today>)")
    ap.add_argument("--max-pages", type=int, default=40)
    ap.add_argument("--no-render", action="store_true", help="never use the headless browser for JavaScript-built pages")
    ap.add_argument("--ai", action="store_true", help="let Claude read listing pages the rules cannot (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--ai-model", default="claude-opus-5-5")
    ap.add_argument("--no-match", action="store_true", help="skip matching against the app's data")
    ap.add_argument("--send", action="store_true", help="post NEW events (not duplicates) for organizers without a scraper")
    a = ap.parse_args(argv)

    orgs = build_registry(a.export_dir)
    if a.list:
        print(f"{'slug':34} {'website':45} {'scraper':10} {'events':>6} {'max teams':>9}  vendor?  states")
        for o in orgs:
            print(f"{o['slug'][:34]:34} {(o['website'] or '-')[:45]:45} {(o['scraper'] or '-'):10} {o['events']:6} {o['max_teams']:9}  "
                  f"{'yes' if o['qualifies_vendor'] else 'no':7}  {','.join(o['states'])[:30]}")
        print(f"\n{len(orgs)} organizers, {sum(1 for o in orgs if o['website'])} with a website, "
              f"{sum(1 for o in orgs if o['qualifies_vendor'])} qualify for vendor contact (>= 20 teams by precedent)")
        return 0
    pick = [o for o in orgs if o["website"] and (o["slug"] in a.org or (a.known and o["scraper"]) or a.with_websites)]
    if not pick:
        print("Nothing to crawl: pass --org <slug>, --known or --with-websites (see --list)."); return 1

    client = None
    if a.ai:
        from ..anthropic_client import make_client
        client = make_client()
    out = pathlib.Path(a.out or OUT_ROOT / dt.date.today().isoformat()); out.mkdir(parents=True, exist_ok=True)
    rows, records = [], []
    for o in pick:
        t0 = time.time()
        fetch = Fetcher(max_pages=a.max_pages, render=not a.no_render)
        try:
            res = crawl_organizer(o, fetch=fetch, ai_client=client, ai_model=a.ai_model)
        finally:
            fetch.close()
        orow = [to_row(o, e) for e in res["events"]]
        rows += orow; records.append(res["organizer"])
        r = res["organizer"]
        log.info("%-12s %3d events, %2d pages, %4.0fs | email %s, phone %s, vendor page %s, vendor contact %s%s", o["slug"], len(orow), res["pages"],
                 time.time() - t0, "yes" if r["main_email"] else "no", "yes" if r["main_phone"] else "no", "yes" if r["vendor_page"] else "no",
                 "yes" if any(r["vendor_contact"].values()) else "no", f" | {'; '.join(res['notes'])}" if res["notes"] else "")
    (out / "events.jsonl").write_text("".join(json.dumps(r, default=_json_default) + "\n" for r in rows))
    (out / "organizers.jsonl").write_text("".join(json.dumps(r, default=_json_default) + "\n" for r in records))
    log.info("wrote %d events and %d organizer records to %s (organizer records hold contacts: keep them out of the repo)", len(rows), len(records), out)

    matches = []
    if not a.no_match:
        from .match import match_events
        matches, summary = match_events(rows, a.export_dir)
        (out / "matches.jsonl").write_text("".join(json.dumps(m, default=_json_default) + "\n" for m in matches))
        log.info("matching: %s", summary)

    if a.send:
        from ..common import Supabase
        scraped = {o["slug"] for o in pick if o["scraper"]}
        dup = {m["dedupe_key"] for m in matches if m.get("duplicate_of")}
        new = [r for r in rows if r["source"].removeprefix("org-") not in scraped and f"{r['source']}:{r['source_event_id']}" not in dup]
        if new:
            n = Supabase().upsert_candidates(new)
            log.info("sent %d new events (%d skipped as duplicates or covered by a scraper); organizer records and confirmations "
                     "wait for the app's organizers/confirmations payloads", n, len(rows) - len(new))
        else:
            log.info("nothing new to send")
    return 0


if __name__ == "__main__":
    sys.exit(main())
