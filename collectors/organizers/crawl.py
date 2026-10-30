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
from collections import Counter

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
NOT_EVENT = re.compile(r"\b(camps?|clinics?|training|tryouts?|lessons?|private|academy\s+program|membership|newsletter|blog|news|recap|results?|standings|"
                       r"prospect\s+days?|id\s+days?|recruiting)\b", re.I)
# pages that list other people's events or no events at all: a club's college-camp calendar (yjnorthlacrosse.com
# /recruiting-events listed 100+ college camps), news posts, tryouts
NOT_EVENT_PAGE = re.compile(r"/(news|blog|posts?|recruit\w*|college\w*|camps?|clinics?|tryouts?|press|media|articles?|popups?)(?:[-/]|$)", re.I)
# a club's or organizer's site lists practices, fundraisers and camps beside its tournaments: an event must say what it is
TOURNAMENT_WORD = re.compile(r"\b(tournaments?|tourney|classic|cup|showcase|invitational|shoot-?out|fest\w*|challenge|championships?|bash|"
                             r"series|jamboree|brawl|sixes|6v6|7v7|box|games|throw-?down|battle|duel|clash|rumble|blast|frenzy|madness|"
                             r"havoc|bowl|open|slam|summit|qualifier|playoffs?|league|divisions?|teams?|bracket|pool play|prix|tour|futures|lax)\b", re.I)
# events of another sport on a lacrosse organizer's site ('BCA Elite Lacrosse & Soccer'); lacrosse events never say these
OTHER_SPORT = re.compile(r"\b(soccer|futsal|basketball|baseball|softball|volleyball|hockey|football|pickleball|wrestling|track meet|cross country)\b", re.I)
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
                       r"(attending\s+)?college\s+coaches(\s+attending)?|coaches|sponsors?|directions|parking|hotels?|lodging)\b"
                       # deadlines listed on an event page ("Balances Due", "Submit schedule requests by", "Player Waivers Complete")
                       r"|\b(due|deadline)\b|^submit\b|\bwaivers?\s+(complete|due)|\bregistration\s+(opens|closes)|\brequests?\s+by\b"
                       # a share button or file name taken for a name ('Sharer.Php'), a tagline ('Dynamic | Fast-paced | High-rep')
                       r"|\.(php|html?|aspx?|jsp)\b|\bsharer\b|\|.*\|", re.I)


# a page heading, not an event, when it is the whole name ("Events", "Upcoming Events"); "Tournament of Champions" stays
HEADING_NAME = re.compile(r"^(?:all\s+|upcoming\s+|our\s+|featured\s+)?(?:events?|tournaments?|showcases?|calendar|schedule|programs?)$", re.I)


def clean_name(name):
    """'Laxtoberfest Oct. 25th, 2026' -> 'Laxtoberfest': names carry the date on listing pages but not on event pages."""
    n = X.DATE_TEXT.sub(" ", tidy_caps(X.clean(name)) or "")
    n = re.sub(r"\b(?:19|20)\d\d\b\s*$", " ", n)
    n = re.sub(r"^\s*(?:19|20)\d\d\s+", "", n)        # "2026 Boys Fall Grand Prix" -> "Boys Fall Grand Prix", as the scrapers do
    n = re.sub(r"\s*[(\[]\s*$", "", n)                 # "Laxgiving Shootout (" : the date inside the brackets was cut off
    n = re.sub(r"\s+(?:information|info)$", "", n.strip(), flags=re.I)   # "Hackfest 2026 Information"
    n = re.sub(r"^tournament\s+(?=\S+\s+\S)", "", n, flags=re.I)        # "Tournament Witches Brew Lax Fest" (a label before the name)
    n = re.sub(r"(?<![A-Z])\.$", "", n.strip())          # "Lehigh Laxfest." (not "Inc." handling: 'St.' never ends a name)
    if n[:1].islower() and re.search(r"\b[A-Z]{4,}\b", n):   # "northeast regional CHAMPIONSHIP"
        n = " ".join(w.capitalize() for w in n.lower().split())
    elif re.search(r"\b[a-z]{4,}\b", n) and re.search(r"\b[A-Z]{5,}\b", n):   # "Six Nations regional CHAMPIONSHIP"
        n = " ".join(w.capitalize() if (w.isupper() and len(w) >= 5) or (w.islower() and len(w) >= 4) else w for w in n.split())
    n = re.sub(r"^(?:bring your team to|join us (?:for|at)|register for|welcome to)\s+", "", n, flags=re.I)
    n = re.sub(r"\s+(?:WHEN|WHERE|DATES?|LOCATION|TIME)$", "", n)        # a label run into the name
    return re.sub(r"\s+", " ", n).strip(" -–—|:,·") or X.clean(name)


GENERIC_WORDS = {"lacrosse", "lax", "events", "event", "tournaments", "tournament", "the", "and", "of", "inc", "llc", "club", "series", "league"}
FACILITY_NAME = re.compile(r"\b(arena|complex|park|fields?|center|centre|\w*plex|stadium|dome|school|college|university|academy|turf|facility)\s*$", re.I)
LEVEL = re.compile(r"\b(youth|high\s*school|h\.?s\.?|varsity|jv|middle\s*school|college|adult|masters)\b", re.I)
GENDER_WORD = re.compile(r"\b(boys?|girls?|men'?s|women'?s)\b", re.I)
DIVISION_LINE = re.compile(r"\b(\d(?:st|nd|rd|th)\s*(?:/\s*\d(?:st|nd|rd|th))*\s*grade|grades?\b|division|varsity|pre-?k|kindergarten|u\d{1,2}\b|"
                           r"games\s+as\s+early|(?:19|20)\d\d\s*(?:/|&|-)\s*(?:19|20)?\d\d\b)", re.I)


def _words(name):
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in GENERIC_WORDS and not re.fullmatch(r"(?:19|20)\d\d", w)}


SEASON_TYPE = {"fall", "spring", "summer", "winter", "autumn", "annual", "invitational", "championship", "championships", "classic", "cup",
               "showcase", "open", "boys", "girls", "youth", "session", "regional", "national", "box", "sixes"}


def _org_words(org_name):
    """The organizer's words plus its initials ('Pinnacle Lacrosse Championships' -> ..., 'plc')."""
    caps = re.findall(r"[A-Za-z]+", org_name or "")
    return _words(org_name) | ({"".join(w[0] for w in caps).lower()} if len(caps) > 1 else set())


def strip_org(name, org_name):
    """'Deep Creek Lacrosse Tournament — Maryland Lacrosse League' -> 'Deep Creek Lacrosse Tournament': a page title's
    site-name part. None when the whole name is just the organizer ('101 Lacrosse Events' on the Gold Rush page)."""
    org = _words(org_name)
    parts = [x for x in re.split(r"\s+[|–—]\s+|\s+-\s+", name or "") if x.strip()]
    keep = [x for x in parts if not (_words(x) and _words(x) <= org | {"inc"})]
    if not keep: return None
    return " – ".join(keep) if len(keep) < len(parts) else name


def name_from_url(url):
    """'.../tournaments/gold-rush/' -> 'Gold Rush'."""
    seg = [x for x in urllib.parse.urlparse(url or "").path.split("/") if x]
    s = re.sub(r"\.\w{2,4}$", "", seg[-1]) if seg else ""
    s = re.sub(r"[-_]+", " ", s).strip()
    return s.title() if re.search(r"[a-z]{3}", s, re.I) and not re.fullmatch(r"[a-z]{0,3}\d+|event details.*|events?", s, re.I) else None


def _differ(na, nb):
    """Youth vs high school, boys vs girls: two events even on one day at one organizer."""
    for rx in (LEVEL, GENDER_WORD):
        la = {re.sub(r"\W|s$", "", x.lower()) for x in rx.findall(na)}; lb = {re.sub(r"\W|s$", "", x.lower()) for x in rx.findall(nb)}
        if la != lb: return True
    return False


def same_event(a, b):
    """Two listings of one event on one organizer's site: same start (and end, when both say), and the names share a
    word once the organizer's and generic words are set aside, with no youth/high-school or boys/girls difference
    ('PLC Fall Championships' = 'Fall Pinnacle Lacrosse Championships'; 'Youth Fall Invitational' != 'Fall High School
    Invitational')."""
    if a["start"] != b["start"] or (a.get("end") and b.get("end") and abs((a["end"] - b["end"]).days) > 3): return False
    na, nb = a["name"], b["name"]
    for rx in (LEVEL, GENDER_WORD):
        la = {re.sub(r"\W|s$", "", x.lower()) for x in rx.findall(na)}; lb = {re.sub(r"\W|s$", "", x.lower()) for x in rx.findall(nb)}
        if la and lb and la != lb: return False
    same_page = (a.get("detail") or a["page"]).rstrip("/").replace("www.", "") == (b.get("detail") or b["page"]).rstrip("/").replace("www.", "")
    org = _org_words(a.get("org", ""))
    wa, wb = _words(na) - org, _words(nb) - org
    # a season or a kind of event alone ('Fall', 'Invitational') is shared by different events of one organizer
    # or one name inside the other ('Boys Session' in 'NC LaxFest Session 2: BOYS')
    inside = min(len(wa), len(wb)) >= 2 and (wa <= wb or wb <= wa)
    return same_page or wa == wb or inside or bool((wa & wb) - SEASON_TYPE)


def merge_events(events, page_name=None):
    """One event per real event: copies of one event on one site merged (the longer name kept, missing fields filled),
    and an event's division lines listed as separate dated items on one page ('7th/8th Grade Boys Games as early as
    12:30pm Saturdays', '3rd Grade Boys ...') folded into one event named after that page."""
    by_page = {}
    for ev in events:
        by_page.setdefault(ev["page"], []).append(ev)
    out = []
    for page, evs in by_page.items():
        lines = [e for e in evs if DIVISION_LINE.search(e["name"])]
        enough = 2 if sum(bool(re.search(r"\bdivisions?\b", e["name"], re.I)) for e in lines) >= 2 else 3   # '3/4 & 5/6 divisions: Saturday'
        if len(lines) >= enough and (max(e["start"] for e in lines) - min(e["start"] for e in lines)).days <= 7:
            first = min(lines, key=lambda e: e["start"])
            # the page's address names the event ('/indoor-league-at-turf-city/'); its heading is often a slogan
            one = {**first, "name": name_from_url(page) or (page_name(page) if page_name else None) or first["name"],
                   "start": first["start"], "end": max(e.get("end") or e["start"] for e in lines),
                   "text": " ".join(e.get("text") or "" for e in lines)[:3000]}
            evs = [e for e in evs if e not in lines] + [one]
        out += evs
    merged = []
    for ev in sorted(out, key=lambda e: -len(e["name"])):
        twin = next((m for m in merged if same_event(m, ev)), None)
        if twin is None:
            merged.append(ev); continue
        for k in ("detail", "venue_name", "address", "city", "state", "lat", "long", "detail_text"):
            if ev.get(k) and not twin.get(k): twin[k] = ev[k]
        twin["text"] = f"{twin.get('text') or ''} {ev.get('text') or ''}"[:3000]
    return sorted(merged, key=lambda e: e["start"])


def drop_conflicts(events):
    """-> (kept, dropped). One organizer listing an event under two spellings with two dates ('Founders Cup' Jan 9 and
    'Founders Cup- Box Lacrosse' Jan 30 on Fusion's site): which date is right can't be told, so neither is sent.
    The same name on two dates is two editions and stays."""
    bad = set()
    for i, a in enumerate(events):
        for b in events[i + 1:]:
            if a["start"] == b["start"] or abs((a["start"] - b["start"]).days) > 60: continue
            if name_key(a["name"]) == name_key(b["name"]): continue
            org = _org_words(a.get("org", ""))
            wa, wb = _words(a["name"]) - org, _words(b["name"]) - org
            if min(len(wa), len(wb)) >= 2 and (wa <= wb or wb <= wa) and not re.search(r"\d", " ".join(wa ^ wb)):
                bad |= {id(a), id(b)}
    return [e for e in events if id(e) not in bad], [e for e in events if id(e) in bad]


POSTAL = re.compile(r"\b\d{5}(?:-\d{4})?\b|\bP\.?\s?O\.?\s+Box\b|\bSuite\b", re.I)


def office_contexts(home_text):
    """{'Sparta, NJ': ['... PO Box 905 Sparta, NJ, 07871']}: towns the homepage prints in a postal line (a ZIP, PO Box or
    suite next to it), the organizer's office. Event towns ('Naples, Florida' in a 'Next event' banner) have none."""
    out = {}
    for c in X.find_city_states(home_text):
        town = c.split(",")[0]
        for m in re.finditer(re.escape(town), home_text):
            ctx = home_text[max(0, m.start() - 60): m.end() + 20]
            if POSTAL.search(ctx): out.setdefault(c, []).append(ctx)
    return out


def drop_office_town(ev, offices):
    """The office town off an event whose own listing doesn't name it: it came from the page footer (Ath-Ed's 'PO Box 905
    Sparta, NJ' on the Lehigh Laxfest page). No town beats a wrong one."""
    block = ev.get("text") or ""
    for c in offices:
        if c not in ev.get("city_states", []): continue
        town = c.split(",")[0]
        # the event names the town itself somewhere other than a postal line
        own = any(not POSTAL.search(block[max(0, m.start() - 40): m.end() + 20]) for m in re.finditer(re.escape(town), block))
        if not own and town not in (ev.get("address") or ""):
            ev["city_states"] = [x for x in ev["city_states"] if x != c]


def cross_org_duplicates(rows):
    """-> (kept, dropped). One event listed by two organizers (the USBOXLA Northeast Regional on US Box's site and on
    Fusion's): same start and end, no state conflict, and a distinctive word in common. The row with more details stays."""
    def state(r):
        cs = (r.get("city_states") or [""])[0]
        return cs.rpartition(", ")[2] or None
    def filled(r):
        return sum(bool(r.get(k)) for k in ("venue_name", "city_states", "age_groups", "vendor_contact_email", "team_count")) + \
               bool((r.get("raw") or {}).get("gender")) + bool((r.get("raw") or {}).get("contacts"))
    dropped = set()
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if a["source"] == b["source"] or id(a) in dropped or id(b) in dropped: continue
            if str(a["start_date"]) != str(b["start_date"]) or str(a["end_date"]) != str(b["end_date"]): continue
            if state(a) and state(b) and state(a) != state(b): continue
            wa = _words(a["name"]) - _org_words(a.get("organizer_name")) - SEASON_TYPE
            wb = _words(b["name"]) - _org_words(b.get("organizer_name")) - SEASON_TYPE
            if wa & wb:
                dropped.add(id(min((a, b), key=filled)))
    return [r for r in rows if id(r) not in dropped], [r for r in rows if id(r) in dropped]


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
    a = next((x for x in el.find_all("a", href=True) if not SKIP_LINK.search(x["href"]) and not re.search(r"share|\.(php|html?|aspx?)$", x["href"], re.I)), None)
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


def _tidy_dom(soup):
    """Read the page as a visitor sees it. Webflow keeps hidden copies of fields (class w-condition-invisible), and some
    sites split a date into one element per piece ('June' / '21' / '&' / '2027'); drop the hidden copies and join the
    pieces back into one date string so it can be found."""
    for t in soup.find_all(class_="w-condition-invisible"):
        if not getattr(t, "decomposed", False): t.decompose()
    for el in soup.find_all(True):
        if getattr(el, "decomposed", False): continue
        kids = el.find_all(True, recursive=False)
        if len(kids) < 3 or any(k.find(True) for k in kids): continue
        parts = [p for p in (X.clean(k.get_text(" ", strip=True)) for k in kids) if p]
        if not parts or any(len(p) > 12 for p in parts) or any(X.DATE_TEXT.search(p) for p in parts): continue
        joined = re.sub(r"\s*&\s*(?=(?:19|20)\d\d\b)", " ", " ".join(parts))      # "December 5 & 2026": the 2nd day was blank
        if X.DATE_TEXT.search(joined):
            el.clear(); el.string = joined
    return soup


def child_pages(html, page_url):
    """Links one level below a listing page ('/events/winter-events/' -> '/events/winter-events/nlf-futures/'): its event pages."""
    base = urllib.parse.urlparse(page_url).path.rstrip("/") + "/"
    out = []
    for u, _ in links(html, page_url):
        p = urllib.parse.urlparse(u)
        rest = p.path[len(base):].strip("/") if p.path.startswith(base) else ""
        if site_of(u) == site_of(page_url) and rest and "/" not in rest and not p.query: out.append(u.split("?")[0])
    return list(dict.fromkeys(out))


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
    _tidy_dom(soup)
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
    followed = set()
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
        # event pages to read on their own: every event's page when the listing shows only past dates (a stale listing,
        # NLF's still shows last season), and pages one level below a listing that no block already points to
        covered = {b.get("detail") for b in blocks} | set(pages)
        follow = []
        if blocks and all((b.get("end") or b["start"]) < today for b in blocks if b.get("start")):
            follow = [b["detail"] for b in blocks if b.get("detail") and site_of(b["detail"]) == site_of(home)]
            notes.append(f"listing shows only past dates, read its event pages: {page}")
        if page != home:
            follow += [u for u in child_pages(html, page) if u not in covered]
        for u in list(dict.fromkeys(follow))[:15]:
            if u in followed: continue
            followed.add(u)
            one = single_event(fetch.get(u) or "", u, today)
            if one: raw.append(one)
    seen = {}                                       # (name, start) -> event; the same event on the homepage and a listing page merges
    for ev in raw:
        if not ev.get("start") or JUNK_NAME.search(ev.get("name") or "") or OTHER_SPORT.search(ev.get("name") or ""): continue
        if NOT_EVENT_PAGE.search(urllib.parse.urlparse(ev.get("page") or "").path) or NOT_EVENT.search(ev.get("name") or ""): continue
        if (ev.get("end") or ev["start"]) < today - dt.timedelta(days=1) or ev["start"] > today + dt.timedelta(days=550): continue
        key = (name_key(ev["name"]).replace(" ", ""), ev["start"])      # "Main Stage" and "Mainstage" are one event
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
    # names: the site name dropped from page titles, the organizer's own name replaced by the event page's, a venue
    # taken for a name dropped ('Coppermine Du Burns Arena' was the venue line of the Christmas Classic card)
    for ev in events:
        ev["org"] = org["name"]
        n = strip_org(ev["name"], org["name"])
        ev["name"] = n or name_from_url(ev.get("detail") or ev["page"]) or ""
        # a one-event site's page heading ('Home') under an organizer named for its tournament (The Reindeer Games Lacrosse Tournament)
        if re.fullmatch(r"(?i)home|overview|about|registration|(?:event\s+)?website|schedule", ev["name"]) and NEW_NAME.search(org["name"]):
            ev["name"] = org["name"]
    events = [ev for ev in events if ev["name"] and not JUNK_NAME.search(ev["name"])
              and not (FACILITY_NAME.search(ev["name"]) and not TOURNAMENT_WORD.search(ev["name"]))]
    events = merge_events(events, lambda u: page_title(fetch.get(u)) if u else None)
    events, unsure = drop_conflicts(events)
    if unsure:
        notes.append("not sent, the site gives two dates: " + "; ".join(f"{e['name']} {e['start']}" for e in unsure))
    # its name, its own block or the top of its own page says tournament/showcase/teams... (lower down, menus name every
    # other event); a practice, fundraiser or camp on a club's calendar does not
    events = [ev for ev in events if TOURNAMENT_WORD.search(f"{ev.get('name')} {(ev.get('text') or '')[:600]} {(ev.get('detail_text') or '')[:2000]}")]
    offices = office_contexts(_text(BeautifulSoup(home_html, "lxml")))
    for ev in events:
        fields_from_text(ev)
        drop_office_town(ev, offices)
    # the organizer's own address (page footer) lands on every event: drop a city found on the homepage and on 80%+ of
    # events, unless it is an event's only city (ML8's Miami office was every event's first city)
    if len(events) >= 3:
        home_cs = set(X.find_city_states(_text(BeautifulSoup(home_html, "lxml"))))
        counts = Counter(c for ev in events for c in set(ev["city_states"]))
        office = {c for c in home_cs if counts[c] >= 0.8 * len(events)}
        for ev in events:
            rest = [c for c in ev["city_states"] if c not in office]
            if rest: ev["city_states"] = rest

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
    if org.get("qualifies_vendor") and not any(rec["vendor_contact"].values()):
        # no vendor page, or one without a contact: vendor/sponsor wording on the homepage or contact page
        # ('Vendors and sponsors: email info@...') still names the inbox
        for u, html in contact_html:
            vc = X.vendor_contact(BeautifulSoup(html or "", "lxml").get_text(" ")) if html else {}
            if vc.get("email") or vc.get("phone"):
                rec["vendor_contact"] = vc; rec["sources"]["vendor_contact"] = u
                break
        if not found["vendor"]:
            notes.append("no vendor/sponsor page linked from the homepage")
    return {"organizer": rec, "events": events, "pages": fetch.pages, "notes": notes}


def clean_venue(v):
    """A venue name, or None for text that only sits where a venue would ('Finals will be 2/8 Soccer Dome')."""
    v = re.sub(r"[^\w\s&'’.,()\-–—/#]", "", v or "").strip(" ,-–—")
    v = re.split(r"\s+\d{3,}\s", v)[0].strip(" ,")       # 'Kirigan Cellars 11550 Watsonville Rd ...': the street goes in the address
    if re.match(r"\d", v): return None                   # a street address, not a venue name
    if not v or len(v) > 70 or re.search(r"\b(will|must|please|click|register|finals?|tbd|to be announced)\b|\d+/\d+", v, re.I): return None
    return v


GENDER = {"both": "coed"}       # the organizer scrapers' word (core.model.normalize_gender)


def row_contacts(rec):
    """raw.contacts in the organizer scrapers' shape (woodgoods_lacrosse.contact_updates reads it): the vendor contact
    first, then the organizer's main inbox/phone, which is where vendors write when there is no vendor contact."""
    if not rec: return []
    out, vc = [], rec.get("vendor_contact") or {}
    if vc.get("email") or vc.get("phone"):
        out.append({"role": "vendor", "name": vc.get("name"), "email": vc.get("email"), "phone": vc.get("phone")})
    if (rec.get("main_email") or rec.get("main_phone")) and rec.get("main_email") != vc.get("email"):
        out.append({"role": "organizer", "name": None, "email": rec.get("main_email"), "phone": rec.get("main_phone")})
    return out


def to_row(org, ev, rec=None):
    """An event in the ingest rows shape. venues[] lets the app's venue job map it to an existing facility. rec (the
    organizer record) adds the vendor contact and the organizer's inbox."""
    city = (ev.get("city_states") or [""])[0]
    town, _, st = city.rpartition(", ")
    ev["venue_name"] = clean_venue(ev.get("venue_name"))
    bare = re.sub(r"\s+(?:\w*plex|complex|center|centre|stadium|fields?)$", "", town, flags=re.I)   # 'Rockville Sportsplex, MD'
    if bare != town and bare:
        town, city = bare, f"{bare}, {st}"
        ev["city_states"] = [city] + (ev.get("city_states") or [])[1:]
    venue = None
    if ev.get("venue_name") or ev.get("address"):
        venue = {"venue_name": ev.get("venue_name") or ev.get("address"), "address": ev.get("address"),
                 "city": town or None, "state": st or None, "lat": ev.get("lat"), "long": ev.get("long")}
    contacts = row_contacts(rec)
    vendor = next((c for c in contacts if c["role"] == "vendor"), None)
    return {"source": f"org-{org['slug']}", "source_event_id": slugify(f"{ev['name']}-{ev['start']}-{city}"),
            "name": ev["name"], "sport": "lacrosse", "organizer_name": org["name"],
            "start_date": ev["start"], "end_date": ev.get("end") or ev["start"], "city_states": ev.get("city_states") or [],
            "venue_name": ev.get("venue_name"), "venues": [venue] if venue else None, "team_count": ev.get("team_count"),
            "age_groups": ev.get("age_groups"), "grad_year_min": ev.get("grad_year_min"), "grad_year_max": ev.get("grad_year_max"),
            "listing_url": ev.get("page"), "official_url": ev.get("page"),
            "vendor_contact_email": vendor and vendor["email"], "vendor_contact_name": vendor and vendor["name"],
            "has_field_map": False,           # the app rejects new rows with has_field_map empty (2026-10-26)
            "raw": {"gender": GENDER.get(ev.get("gender"), ev.get("gender")), "start_time": ev.get("start_time"), "end_time": ev.get("end_time"),
                    "extracted_by": ev.get("how"), "organizer_website": org.get("website"), "address": ev.get("address"),
                    "vendor_page": (rec or {}).get("vendor_page"), "contacts": contacts or None}}


# a new event (one the app has nowhere) is sent only when it plainly is a tournament: its name says so, it is not a
# program/league/page label, it lasts a weekend, and it is not another organizer's event listed on a club's page
NEW_NAME = re.compile(r"\b(tournaments?|tourney|classic|cup|showcases?|invitational|shoot-?out|\w*fest|challenge|championships?|bash|jamboree|"
                      r"brawl|sixes|6ixes|6'?s|7v7|throw-?down|battle|duel|clash|rumble|blast|frenzy|madness|havoc|bowl|slam|summit|qualifier|"
                      r"mayhem|splash|games|derby|regional|face-?off|debut)\b", re.I)
PROGRAM = re.compile(r"\b(seasons?|programs?|sessions?|skills|lab|soft\s*sticks|prep|pick-?up|practices?|training|leagues?|clinics?|camps?|"
                     r"registration|schedules?|information|rules|guide|format|overview|about|pricing|waivers?|lodging|search|website|home|"
                     r"tryouts?|prospect|id\s+day|recruiting|all\s+teams|contact|sell\s+out|asap|sign\s+up|register|individual)\b", re.I)
OTHER_ORG = re.compile(r"\b(apex|mlt|nxt|primetime|prime\s*time|plc|iwlca|g365|five\s*tool|liberty|adrenaline|trilogy|victory|hogan'?s?|ml8|"
                       r"nlf|aloha|alliance|buku|pll|legends|carpetbagger|otlax|team\s*91|usboxla|3d)\b", re.I)


def good_new_event(r):
    """-> None if the row may go to the app as a new event, else why not."""
    name = r["name"]
    try: days = (dt.date.fromisoformat(str(r["end_date"])[:10]) - dt.date.fromisoformat(str(r["start_date"])[:10])).days
    except ValueError: days = 0
    if days > 3: return "longer than a weekend (a league or program)"
    if not NEW_NAME.search(name): return "name doesn't say tournament/classic/cup/showcase..."
    if PROGRAM.search(name): return "a program, league or page label"
    if re.search(r"[|:·]", name) or len(name.split()) > 8: return "a sentence or list, not a name"
    if len(name.split()) < 2: return "no name, only a kind of event"         # 'Tourney'
    # a club's site lists the tournaments its teams play in, mostly run by others (Team 91's 'Great Atlantic Lacrosse Championship')
    if re.search(r"^team\b|\bclub\b", r.get("organizer_name") or "", re.I): return "a club's site: may be another organizer's event"
    if sum(1 for w in name.split() if len(w) <= 2 and w.isalpha() and w.lower() not in ("of", "at", "in", "on", "la", "de", "vs", "a", "&")) >= 2:
        return "garbled text"                                # 'Autumn G ol d Showcase'
    return foreign(r)


PAGE_LABEL = re.compile(r"\b(registration|schedules?|information|rules|guide|format|overview|about|pricing|waivers?|lodging|search|"
                        r"website|home|contact|sell\s+out|asap|divisions?|all\s+teams)\b", re.I)


def label(r):
    """Why not, when the name is a page label or a sentence rather than an event name."""
    if re.search(r"[|:·]", r["name"]) or len(r["name"].split()) > 8 or PAGE_LABEL.search(r["name"]): return "a page label, not an event name"
    return None


def foreign(r):
    """Why not, when a club's page lists another organizer's event ('Apex Fall Championships' on Team 91's site)."""
    name, org = r["name"], f"{r.get('organizer_name') or ''} {(r.get('raw') or {}).get('organizer_website') or ''}".lower()
    other = [m.group(0) for m in OTHER_ORG.finditer(name) if re.sub(r"\W", "", m.group(0).lower()) not in re.sub(r"\W", "", org)]
    if other: return f"another organizer's event ({other[0]})"
    return None


def select_for_send(rows, statuses):
    """-> (send, held: [(row, why)]). statuses: {(organizer, name, start): report status} from a dry run of
    tools/lacrosse_match_report.py. Events matching an app row go (their badges link them); a new event goes only
    through good_new_event() and when no matched event of this crawl sits within 30 days under a shared name word
    (US Box's 'Western Regional Championship' on Feb 5 next to Cali*Lax's confirmed 'USBOXLA Western Regional' Feb 14)."""
    key = lambda r: (r.get("organizer_name") or "", r["name"], str(r["start_date"])[:10])
    matched = [r for r in rows if statuses.get(key(r), "NEW") not in ("NEW", "NEW SITE")]
    # a matched event still needs a real name: a club's copy of another organizer's event, or a page label
    # ('Girls & Boys Divisions: U8-High School ...') that happened to land on an app row's date, is not sent
    held = [(r, foreign(r) or label(r)) for r in matched if foreign(r) or label(r)]
    matched = [r for r in matched if not (foreign(r) or label(r))]
    send = list(matched)
    dwords = lambda r: _words(r["name"]) - _org_words(r.get("organizer_name")) - SEASON_TYPE
    for r in rows:
        if r in matched: continue
        why = good_new_event(r)
        if not why:
            wr, s0 = dwords(r), dt.date.fromisoformat(str(r["start_date"])[:10])
            # one name's distinctive words inside the other's ('Western Regional Championship' / 'USBOXLA Western Regional'),
            # or the same organizer on the same day (WV Red Hawks' '7 on 7 tournament' is its Hackfest)
            near = next((m for m in matched if abs((dt.date.fromisoformat(str(m["start_date"])[:10]) - s0).days) <= 30 and (
                         (wr and dwords(m) and (wr <= dwords(m) or dwords(m) <= wr)) or
                         (m["source"] == r["source"] and str(m["start_date"])[:10] == str(r["start_date"])[:10]
                          and not _differ(m["name"], r["name"])))), None)
            if near: why = f"another listing of {near['name']} ({near['start_date']})"
        if why: held.append((r, why))
        else: send.append(r)
    return send, held


def _json_default(o):
    return o.isoformat() if isinstance(o, (dt.date, dt.datetime)) else str(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--org", nargs="*", default=[], help="organizer slugs (see --list)")
    ap.add_argument("--known", action="store_true", help="the 14 organizers that have scrapers")
    ap.add_argument("--with-websites", action="store_true", help="every organizer with a known website")
    ap.add_argument("--seed-only", action="store_true", help="only the organizers in lacrosse_websites.json (found-sites-crawl.yml)")
    ap.add_argument("--shard", default=None, help="i/N: crawl every Nth organizer starting at the i-th (parallel jobs)")
    ap.add_argument("--all-vendors", action="store_true", help="look for a vendor contact at every organizer, not only 20+ team ones")
    ap.add_argument("--max-details", type=int, default=25, help="event pages read per organizer")
    ap.add_argument("--stop-after", type=float, default=None, help="minutes: start no new organizer after this (leaves time to send)")
    ap.add_argument("--list", action="store_true", help="print the organizer list and stop")
    ap.add_argument("--export-dir", default=str(EXPORT_DIR), help="app data export (tools/backup_db.py output)")
    ap.add_argument("--out", default=None, help="output folder (default ~/WoodGoodsAI/crawl/<today>)")
    ap.add_argument("--max-pages", type=int, default=40)
    ap.add_argument("--no-render", action="store_true", help="never use the headless browser for JavaScript-built pages")
    ap.add_argument("--ai", action="store_true", help="let Claude read listing pages the rules cannot (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--ai-model", default="claude-opus-5-5")
    ap.add_argument("--no-match", action="store_true", help="skip matching against the app's data")
    ap.add_argument("--send", action="store_true", help="post the crawled events (organizers without a scraper) and their contacts")
    ap.add_argument("--send-file", default=None, help="send rows from an earlier crawl's events.jsonl instead of crawling (with --match-csv)")
    ap.add_argument("--match-csv", default=None, help="dry-run report of those rows: matched events go, new ones only if plainly tournaments")
    a = ap.parse_args(argv)

    if a.send_file:
        import csv
        rows = [json.loads(l) for l in open(a.send_file)]
        statuses = {(l["organizer"] or "", l["event"], l["start"][:10]): l["status"] for l in csv.DictReader(open(a.match_csv))} if a.match_csv else {}
        send, held = select_for_send(rows, statuses)
        for r, why in held:
            log.info("held back: %s | %s %s | %s", r.get("organizer_name"), r["name"], r["start_date"], why)
        out = pathlib.Path(a.out or pathlib.Path(a.send_file).parent); out.mkdir(parents=True, exist_ok=True)
        (out / "sent.jsonl").write_text("".join(json.dumps(r, default=_json_default) + "\n" for r in send))
        log.info("%d of %d events pass (%d held back)", len(send), len(rows), len(held))
        if a.send and send:
            _send(send)
        return 0
    orgs = build_registry(a.export_dir)
    if a.list:
        print(f"{'slug':34} {'website':45} {'scraper':10} {'events':>6} {'max teams':>9}  vendor?  states")
        for o in orgs:
            print(f"{o['slug'][:34]:34} {(o['website'] or '-')[:45]:45} {(o['scraper'] or '-'):10} {o['events']:6} {o['max_teams']:9}  "
                  f"{'yes' if o['qualifies_vendor'] else 'no':7}  {','.join(o['states'])[:30]}")
        print(f"\n{len(orgs)} organizers, {sum(1 for o in orgs if o['website'])} with a website, "
              f"{sum(1 for o in orgs if o['qualifies_vendor'])} qualify for vendor contact (>= 20 teams by precedent)")
        return 0
    pick = [o for o in orgs if o["website"] and (o["slug"] in a.org or (a.known and o["scraper"]) or a.with_websites
                                                  or (a.seed_only and o.get("seeded") and not o["scraper"]))]
    if a.shard:
        i, n = (int(x) for x in a.shard.split("/"))
        pick = sorted(pick, key=lambda o: o["slug"])[i - 1::n]
    if a.all_vendors:
        for o in pick: o["qualifies_vendor"] = True
    if not pick:
        print("Nothing to crawl: pass --org <slug>, --known, --seed-only or --with-websites (see --list)."); return 1

    client = None
    if a.ai:
        from ..anthropic_client import make_client
        client = make_client()
    out = pathlib.Path(a.out or OUT_ROOT / dt.date.today().isoformat()); out.mkdir(parents=True, exist_ok=True)
    rows, records, started = [], [], time.time()
    for k, o in enumerate(pick, 1):
        if a.stop_after and time.time() - started > a.stop_after * 60:
            log.warning("time is up after %d of %d organizers; not crawled: %s", k - 1, len(pick), ", ".join(x["slug"] for x in pick[k - 1:]))
            break
        t0 = time.time()
        fetch = Fetcher(max_pages=a.max_pages, render=not a.no_render)
        try:
            res = crawl_organizer(o, fetch=fetch, ai_client=client, ai_model=a.ai_model, max_details=a.max_details)
        except Exception as e:  # noqa: BLE001  one broken site must not end the run
            log.warning("[%d/%d] %s failed: %s", k, len(pick), o["slug"], e); continue
        finally:
            fetch.close()
        orow = [to_row(o, e, res["organizer"]) for e in res["events"]]
        rows += orow; records.append(res["organizer"])
        r = res["organizer"]
        log.info("[%d/%d] %-12s %3d events, %2d pages, %4.0fs | email %s, phone %s, vendor page %s, vendor contact %s%s", k, len(pick), o["slug"], len(orow), res["pages"],
                 time.time() - t0, "yes" if r["main_email"] else "no", "yes" if r["main_phone"] else "no", "yes" if r["vendor_page"] else "no",
                 "yes" if any(r["vendor_contact"].values()) else "no", f" | {'; '.join(res['notes'])}" if res["notes"] else "")
    rows, twins = cross_org_duplicates(rows)
    for r in twins:
        log.info("same event on another organizer's site, not sent twice: %s (%s) %s", r["name"], r["source"], r["start_date"])
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
        _send([r for r in rows if r["source"].removeprefix("org-") not in {o["slug"] for o in pick if o["scraper"]}])
    return 0


def _send(rows):
    """Post rows, then the vendor contact + organizer inbox of each as the app's contacts payload."""
    from ..common import Supabase
    from ..woodgoods_lacrosse import contact_updates
    if not rows:
        log.info("nothing to send"); return
    db = Supabase()
    log.info("sent %d events", db.upsert_candidates(rows))
    cs = contact_updates(rows)
    try:
        for i in range(0, len(cs), 200):
            db._post({"contacts": cs[i:i + 200]})
        log.info("sent %d contacts", len(cs))
    except Exception as e:  # noqa: BLE001
        log.warning("contacts step failed (events are already saved): %s", e)


if __name__ == "__main__":
    sys.exit(main())
