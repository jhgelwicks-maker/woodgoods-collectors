"""Tier 1-2 collectors for the remaining organizers."""
from __future__ import annotations
import re, time, datetime, urllib.parse
from bs4 import BeautifulSoup
from core.base import BaseCollector
from core.model import Event, parse_date_range, split_city_state, normalize_gender

DATE_INLINE = re.compile(
    r"((?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Za-z]*\.?\s+\d{1,2}"
    r"(?:\s*(?:[-\u2013+&/]|to|and)\s*(?:(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"[A-Za-z]*\.?\s*)?\d{1,2})?,?\s*20\d{2})", re.I)


def place_from_text(text):
    """(venue, city, state) from a page's location text ('Iron Peak Sports & Events Hillsborough, NJ ...',
    'Round Rock Multipurpose Complex 2001 N. Kenney Fort Blvd Round Rock, TX 78665'), with the crawler's readers.
    The venue is the text before the street address, else before the first town."""
    from collectors.organizers import extract as X
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"\b([A-Z])\.([A-Z])\.", r"\1\2", text)                  # 'N.Y.' -> 'NY'
    text = re.sub(r"^Greater\s+[A-Z][\w .'-]*?\s+Area\s+", "", text)     # 'Greater Philadelphia Area West Chester, PA': no venue
    towns = X.find_city_states(text)
    if not towns and X.find_city_states(comma_states(text)):
        text = comma_states(text)
        towns = X.find_city_states(text)
    if not towns:
        return None, None, None
    city, state = towns[0].rsplit(", ", 1)
    addrs = X.find_addresses(text)
    if addrs and 3 <= text.find(addrs[0]) <= 70:
        venue = text[:text.find(addrs[0])].strip(" ,-|")          # the location box opens with the venue: 'nCino Sports Parks'
    elif addrs:
        venue = X.venue_before(text, addrs[0])
    else:
        # the town as written with its state ('Farmingdale, NY'), not the same word inside the venue ('Farmingdale State ...')
        tm = re.search(rf"{re.escape(city)}\s*,\s*(?:{state}\b|[A-Z][a-z]+)", text)
        i = tm.start() if tm else text.find(city)
        venue = text[:i].strip(" ,-|") if i > 0 else None
    if venue:
        venue = re.sub(r"^(?:location|venue|where)\s*:?\s*", "", venue, flags=re.I)
        venue = re.sub(r"\s*[-–]?\s*field map\b.*$|\s+map linked here.*$", "", venue, flags=re.I)
        venue = re.sub(r",?\s+\d{1,6}\b.*$", "", venue)            # 'Santa Barbara Polo Club 3300 Via Real' -> the club
        venue = re.sub(r"\s+(?:in|at)$", "", venue).strip(" ,-|")
    if venue and (len(venue) > 70 or len(venue) < 3):
        venue = None
    return venue or None, city, state


def comma_states(text):
    """'Towson University Towson MD' -> 'Towson University Towson, MD': a state code written without its comma."""
    from core.model import STATES
    codes = "|".join(sorted(STATES))
    # 'Towson MD', 'West Windsor Township NJ, 08550' (a comma after the code but none before it)
    return re.sub(rf"\b([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){{0,3}})\s+({codes})\b(?=\s|,\s*\d|$)", r"\1, \2", text or "")


# '3300 Via Real, Carpinteria, CA 93013', '2350 NY-110, Farmingdale, NY 11735': number ... town, ST zip, any street word
ADDR_LOOSE = re.compile(r"\b\d{1,6}\s+[A-Z0-9][\w.'’\-]*(?:\s+[\w.'’\-]+){0,5}?,?\s+[A-Z][A-Za-z.'’\- ]{1,40}?,\s*[A-Z]{2}\.?\s+\d{5}(?:-\d{4})?\b")


def _norm_addr_text(text):
    t = re.sub(r"\b([A-Z])\.([A-Z])\.", r"\1\2", re.sub(r"\s+", " ", text or ""))
    return re.sub(r",\s*([A-Z][a-z])\s+(\d{5})\b", lambda m: f", {m.group(1).upper()} {m.group(2)}", t)   # 'Kingston, Ri 02881'


def addresses_in(text):
    """[(position, address), ...] for every street address in the text, in order; overlapping finds keep the longer."""
    from collectors.organizers import extract as X
    t = _norm_addr_text(text)
    strict = [(m.start(), m.end(), m.group(0)) for m in X.ADDRESS.finditer(t)]
    # the loose pattern only adds addresses the strict one missed ('3300 Via Real, ...'); where they overlap the strict
    # reading wins, so '2026 Location DE Turf ... 4000 Bay Road ...' doesn't take the year for a house number
    loose = [(m.start(), m.end(), m.group(0)) for m in ADDR_LOOSE.finditer(t)
             if not any(m.start() < e and s < m.end() for s, e, _ in strict)]
    loose = [(s0, e0, a) for s0, e0, a in loose if not re.match(r"(?:19|20)\d\d\s+[A-Z][a-z]+\s", a)]
    return [(s0, a.strip(" ,.")) for s0, _, a in sorted(strict + loose)]


def map_addresses(soup):
    """Addresses inside map links ('google.com/maps/place/19000+Grand+Park+Blvd,+Westfield,+IN+46074')."""
    out = []
    for a in soup.find_all("a", href=True) if soup else []:
        m = re.search(r"maps/(?:place|search)/(?:\?api=1&query=)?([^/?@]+)", a["href"])
        if m:
            out.append(urllib.parse.unquote_plus(m.group(1)))
    return out


def pick_address(text, city=None, state=None, venue=None):
    """The venue's street address on a page that may also list hotels: the first address within 250 characters after
    the venue's name, else the one address in the event's town. None when it can't be told apart."""
    t = _norm_addr_text(text)
    cands = [(i, a) for i, a in addresses_in(t) if not state or re.search(rf"\b{state}\b", a)]
    if venue:
        # every mention of the venue counts: the first is often in the description, the address sits by a later one
        starts = [m.start() for m in re.finditer(re.escape(venue.lower()), t.lower())]
        after = list(dict.fromkeys(a for i in starts for j, a in cands if 0 <= j - i <= 250 + len(venue)))
        if city:                                        # 'University of RI ... Kingston': not Cross Point Park's North Kingstown
            after = [a for a in after if re.search(rf"\b{re.escape(city)}\b", a, re.I)] or \
                    ([] if any(re.search(rf"\b{re.escape(city)}\b", a, re.I) for _, a in cands) else after)
        if after:
            return after[0]
    if city:
        in_town = {re.sub(r"[^a-z0-9]", "", a.lower()): a for _, a in cands if re.search(rf"\b{re.escape(city)}\b", a, re.I)}
        if len(in_town) == 1:
            return next(iter(in_town.values()))
    if len({re.sub(r"[^a-z0-9]", "", a.lower()) for _, a in cands}) == 1 and not venue and not city:
        return cands[0][1]
    return None


SITE_SECTION = re.compile(r"(?i)\b(?:locations?\s*&\s*directions|maps?\s*&\s*(?:locations?|directions)|field\s+locations?(?:\s*\(s\))?|"
                          r"facility\s+information|event\s+locations?\s*\(s\))\b")
SECTION_END = re.compile(r"(?i)\b(?:schedule|hotels?|event headquarters|tourney quick links|event resources|event details|cost|"
                         r"register|policies|team list|shop|media center|elevate your game)\b|\s0\d\s+[A-Z]")


def section_sites(text):
    """Every (venue, city, state, address) in a page's venue section ('LOCATIONS & DIRECTIONS South River High School 201
    Central Ave, Edgewater, MD 21037 Annapolis High School 2700 Riva Road, ...', 'Maps & Locations Westtown School- FIELD
    MAP 975 Westtown Rd, ...', 'Field Location(s) NORTH ELBA ATHLETIC FIELDS (1-7 + HQ) ...'). Each venue is the text
    between the previous address and its own, without map links, field numbers or division notes. Same address once."""
    from collectors.organizers import extract as X
    t = comma_states(_norm_addr_text(text))
    out, seen = [], set()
    for m in SITE_SECTION.finditer(t):
        end = SECTION_END.search(t, m.end() + 5)
        sec = t[m.end(): min(end.start() if end else len(t), m.end() + 900)]
        prev = 0
        for pos, addr in addresses_in(sec):
            venue = sec[prev:pos]
            prev = pos + len(addr)
            # a town left over from the previous entry ('..., Lutherville-Timonium, MD FAIRGROUNDS SITE MAP Copperplex')
            tail = list(re.finditer(r",\s*[A-Z]{2}\b\.?", venue))
            if tail:
                venue = venue[tail[-1].end():]
            venue = re.sub(r"(?i)\b(?:[A-Z]+\s+)?(?:field|site)\s+map\b|\b\d{4}(?:\s*(?:&|,|-|and)\s*\d{4})*\s+divisions?\b|"
                           r"\b\d{4}\s*-\s*\d{4}\s+semi\s*final\s+games\b|\([^)]*\)|\b\d{5}\b|"
                           r"[-–]\s*(?:mon|tues|wednes|thurs|fri|satur|sun)day\b", " ", venue)
            venue = re.sub(r"\s+", " ", venue).strip(" :,.-–|")
            if len(venue) > 70:                          # '... have different parking areas. Mercer County Park:'
                venue = re.split(r"[.!?]\s+", venue)[-1].strip(" :,.-–|")
            towns = X.find_city_states(addr)
            if not towns:
                continue
            city, st = towns[-1].rsplit(", ", 1)
            key = addr_key(addr)
            if key in seen:
                continue
            seen.add(key)
            out.append((venue if 3 <= len(venue) <= 70 and not JUNK_VENUE.search(venue) else None, city, st, addr))
    return out


STREET_ABBR = {"lane": "ln", "road": "rd", "drive": "dr", "street": "st", "avenue": "ave", "boulevard": "blvd", "court": "ct",
               "parkway": "pkwy", "highway": "hwy", "place": "pl", "circle": "cir", "terrace": "ter"}


def addr_key(addr):
    """'1100 Cedar Lane, Bel Air, MD' and '1100 Cedar Ln, Bel Air, MD 21015' are one place: number + street, abbreviated."""
    words = re.findall(r"[a-z0-9]+", (addr or "").lower())[:4]
    return " ".join(STREET_ABBR.get(w, w) for w in words[:3])


def more_sites(text, sites, state):
    """section_sites when they name more venues (in the event's state) than the scraper already has, else the sites given."""
    found = [x for x in section_sites(text) if not state or x[2] == state]
    return found if len(found) > max(1, len(sites)) else sites


def page_text(collector, url):
    """An event page's visible text without menus, header and footer ('' when it can't be fetched)."""
    try:
        soup = collector.soup(url)
    except Exception:  # noqa: BLE001
        return ""
    for t in soup(["script", "style", "noscript", "nav", "header", "footer"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


VENUE_LABELS = [r"Venue\s*&\s*Locations?", r"Maps?\s*&\s*Locations?", r"\bWhere\s*:", r"Date\s*&\s*Time.{0,60}?\bLocation\b",
                r"\bLocation\s*:", r"\bVenue\s*:", r"\bLocation\b"]


def venue_from_page(text):
    """(venue, city, state) from an event page, trying the layouts these sites use:
    'Texas Draw June 5-6, 2027 | Beacon Park, Melissa, TX | Boys' (Victory header), 'Venue & Location Santa Barbara Polo
    Club 3300 Via Real, Carpinteria, CA' (Buku), 'Maps & Locations DE Turf Sports Complex- FIELD MAP 4000 Bay Road
    Frederica, DE' (Victory), 'Where: Farmingdale State College, 2350 NY-110, Farmingdale, NY' (NLF), 'Location DE Turf
    Sports Complex 4000 Bay Road Frederica, DE' (Adrenaline). The first label followed by a town wins."""
    m = re.search(r"\|\s*([^|]{3,90}?,\s*[A-Z]{2})\s*\|", text or "")
    if m:
        p = place_from_text(m.group(1))
        if p[1]:
            return p
    for label in VENUE_LABELS:
        for m in re.finditer(label, text or "", re.I):
            p = place_from_text(text[m.end(): m.end() + 260])
            if p[1]:
                return p
    return None, None, None


JUNK_VENUE = re.compile(r"(?i)event links|\bhotels?\b|\bsponsors?\b|\bregister|\bclick\b|multiple locations|see field|\btbd\b|"
                        r"\btba\b|tour begins|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d|^\W*$")


def _town_words(c):
    return re.sub(r"[^a-z ]", " ", (c or "").lower().replace("saint ", "st ")).split()


def merge_page_place(city, state, page):
    """Combine the listing's town with what an event page says. The page's venue is kept only when its town agrees with
    the listing's ('Rogues Path Huntington Station' agrees with 'Huntington Station'; 'Rehoboth Beach' - a party venue on
    the Black Card page - does not agree with Frederica), it doesn't look like page text, and the state matches. The
    listing's town always wins; the page's town fills in only when the listing has none."""
    venue, c2, st2 = page
    venue = re.sub(r"^\W+", "", venue or "") or None
    if venue and JUNK_VENUE.search(venue):
        venue = None
    if not c2 or (state and st2 and st2 != state):
        return None, city, state
    if city:
        a, b = _town_words(city), _town_words(c2)
        short, long_ = (a, b) if len(a) <= len(b) else (b, a)
        agree = a == b or (len(short) >= 2 and long_[-len(short):] == short) or (len(short) == 1 and long_ == short)
        return (venue if agree else None), city, state
    return venue, c2, st2 or state


LINK_STOP = {"the", "of", "and", "a", "at", "boys", "girls", "lacrosse", "lax", "tournament", "tournaments", "event", "events"}


def _gender_word(words):
    g = {"girls" if w in ("girl", "girls", "women", "womens") else "boys" for w in words if w in ("boy", "boys", "girl", "girls", "men", "mens", "women", "womens")}
    return g.pop() if len(g) == 1 else None


def best_link(name, hrefs):
    """The event page for a listing card: the link whose last path part holds most of the name's words
    ('Texas Draw' -> /event/texas-draw/, 'Lake Tahoe Summit' -> /tournaments/lake-tahoe-summit-classic/).
    Boys/girls must agree: 'Fall Classic (Boys)' never takes /primetime-fall-classic-girls/, and a name without a
    gender prefers the link without one."""
    all_words = re.findall(r"[a-z0-9]+", (name or "").lower())
    words = {w for w in all_words if w not in LINK_STOP}
    g_name = _gender_word(all_words)
    best = None
    for h in dict.fromkeys(hrefs):
        slug = urllib.parse.urlparse(h).path.rstrip("/").rsplit("/", 1)[-1].lower()
        slug_words = [w for w in re.split(r"[-_]", slug) if w]
        sw = {w for w in slug_words if w not in LINK_STOP}
        g_slug = _gender_word(slug_words)
        if not words or not sw or (g_name and g_slug and g_name != g_slug):
            continue
        share = len(words & sw) / len(words)
        gender_fit = 1 if g_name == g_slug else 0                      # same gender, or neither names one
        if share >= 0.6 and (best is None or (share, gender_fit, -len(sw)) > best[0]):
            best = ((share, gender_fit, -len(sw)), h)
    return best[1] if best else None


def site_links(collector, url, pattern):
    """Absolute links on a listing page that match an event-page pattern."""
    try:
        soup = collector.soup(url)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for a in soup.find_all("a", href=True):
        h = urllib.parse.urljoin(url, a["href"]).split("#")[0]
        if re.search(pattern, h) and h not in out:
            out.append(h)
    return out


def places_from_text(text):
    """Every (venue, city, state) in a location block, in order: 'Iron Peak Sports & Events Hillsborough, NJ NOTRE DAME
    High School Lawrenceville, NJ' -> two sites. Each venue is the text between the previous town and this one."""
    from collectors.organizers import extract as X
    text = re.sub(r"\s+", " ", text or "").strip()
    if not X.find_city_states(text):
        text = comma_states(text)                                    # 'Towson University Towson MD'
    out, pos = [], 0
    for city, state, start, end in X.city_state_spans(text):       # every town, repeats too (two sites in Providence)
        venue = re.sub(r"^(?:\d+(?:st|nd|rd|th)\s+location\s*:?\s*)", "", text[pos:start].strip(" ,-|"), flags=re.I)
        out.append((venue if 3 <= len(venue) <= 70 and not JUNK_VENUE.search(venue) else None, city, state))
        pos = end
    return out


class TrilogyCollector(BaseCollector):
    """Trilogy Lacrosse.

    The NAV MENU carries the whole slate, pre-split by gender:
        'Midwest Momentum OCT 24 - 25, 2026 | OH'
    Parse the menu links, not the page body. Venue and town are on each
    event's page ('Location(s) Mercer County Park West Windsor, NJ ...
    Divisions'): one more fetch per event, so matching and venue mapping
    have a town, not just a state. The first location is taken.
    """
    slug = "trilogy"
    organizer = "Trilogy"
    tier = 2
    URL = "https://trilogylacrosse.com/tournaments/"
    LINK_RE = re.compile(
        r"^(?P<name>.+?)\s+(?P<date>(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
        r"[A-Za-z]*\.?\s+\d{1,2}.*?20\d{2})\s*\|\s*(?P<state>[A-Z]{2})"
        r"(?:\s*\|\s*(?P<gender>BOYS|GIRLS|COED))?\s*$", re.I)     # camps/showcases add '| BOYS' 

    def collect(self):
        soup = self.soup(self.URL)
        out, seen = [], set()
        for a in soup.select("a[href*='/events/']"):
            txt = re.sub(r"\s+", " ", self.txt(a) or "").strip()
            m = self.LINK_RE.match(txt)
            if not m:
                continue
            start, end = parse_date_range(m.group("date"))
            if not start:
                continue
            name = m.group("name").strip()
            key = (name.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            gender = normalize_gender(m.group("gender") or name)       # '| GIRLS' or 'Girls Winter Indoor League'
            if gender == "unknown":
                gender = self._gender_from_menu(a)
            sites = self.locations(a.get("href"))
            venue, city, st, address = sites[0] if sites else (None, None, None, None)
            out.append(Event(
                organizer=self.organizer, address=address,
                name=name.title() if name.isupper() else name,
                start_date=start, end_date=end, raw_date=m.group("date"),
                venue=venue, city=city if st in (None, m.group("state").upper()) else None,
                state=m.group("state").upper(), gender=gender, sites=sites if len(sites) > 1 else [],
                event_type=self._type_from_menu(a, name),
                source_url=a.get("href", self.URL),
            ))
        return out

    def locations(self, url):
        """Every (venue, city, state) in the 'Location(s) ... Divisions' block of an event page ('2nd Location TBD' is
        left out: no place yet)."""
        if not url:
            return []
        try:
            text = re.sub(r"\s+", " ", self.soup(url).get_text(" ", strip=True))
        except Exception:  # noqa: BLE001
            return []
        # a site's own division note ('Iron Peak Sports & Events 2032, 2034, 2035/36 Divisions Hillsborough Township, NJ')
        # is not the Divisions heading: drop it so the block runs on to the next site
        text = re.sub(r"\b\d{4}(?:/\d{2,4})?(?:\s*,\s*\d{4}(?:/\d{2,4})?)*\s+Divisions?\b", " ", text)
        # and its day notes: 'SAT: 2032 + 2034', 'SUN: 2027', 'SAT ONLY: 2035 & 2036 / 37 (10v10 & 7v7)'
        text = re.sub(r"\b(?:SAT|SUN|FRI|MON)[A-Z]*(?:\s+ONLY)?\s*:\s*[\d\s,+&/v]+(?:\([^)]*\))?", " ", text)
        # up to the next section heading ('Divisions', or 'Eligibility' on Spring Break Training), at most 400 characters
        m = re.search(r"\bLocations?\b\s*(.{5,400}?)\s*(?:\b(?:Divisions?|Eligibility|Cost|Dates|Register|Event Summary)\b|$)", text)
        if not m:
            return []
        sites = places_from_text(m.group(1))
        if not sites:
            # a venue with no town ('Iron Peak Sports & Events 2032, 2034, 2035/36', 'Mercer County Park & Community
            # College SAT: 2032 + 2034'): keep the venue, cut where the division notes start
            venue = re.split(r"\s+(?:\d{4}\b|(?:SAT|SUN|FRI|MON)\b\s*:)", m.group(1))[0].strip(" ,-|")
            if 3 <= len(venue) <= 70 and not JUNK_VENUE.search(venue):
                sites = [(venue, None, None)]
        # each site's street address, from further down the page ('137 Mountain View Road Hillsborough Township, NJ')
        sites = [(v, c, st, pick_address(text, c, st, v)) for v, c, st in sites]
        # 'Facility Information ... Mercer County Park: 1638 Old Trenton Rd ... Mercer County Community College 1200 ...'
        return more_sites(text, sites, sites[0][2] if sites else None)

    @staticmethod
    def _gender_from_menu(a):
        for parent in a.parents:
            t = (parent.get_text(" ", strip=True) or "")[:400].lower()
            if "girls tournaments" in t and "boys tournaments" not in t:
                return "girls"
            if "boys tournaments" in t and "girls tournaments" not in t:
                return "boys"
        return normalize_gender(a.get_text(" ", strip=True))

    @staticmethod
    def _type_from_menu(a, name):
        n = name.lower()
        if "showcase" in n:
            return "showcase"
        if "camp" in n or "training" in n:
            return "camp"
        if "league" in n:
            return "league"
        return "tournament"


class VictoryCollector(BaseCollector):
    """Victory Event Series. Event lists are clean line runs:
        'October 24 & 25, 2026' / 'VICTORY FALL CLASSIC' / 'Frederica, DE'
    The homepage shows only the next few, in a different name-then-date layout; /boys/, /girls/
    and /showcases/ list everything (showcase dates carry a weekday: 'Friday, June 18, 2027').
    An event on both the boys and girls pages is coed.
    """
    slug = "victory"
    organizer = "Victory"
    tier = 1
    URL = "https://victoryeventseries.com/"
    PAGES = [(URL + "boys/", "boys"), (URL + "girls/", "girls"), (URL + "showcases/", None)]
    DATE_LINE = re.compile(
        r"^(?:(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,?\s+)?"
        r"(January|February|March|April|May|June|July|August|September|October|"
        r"November|December)\s+\d{1,2}\s*(?:[-\u2013&]\s*(?:[A-Z][a-z]+\s+)?\d{1,2})?,?\s*20\d{2}$",
        re.I)
    CITY_LINE = re.compile(r"^[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}$")
    NOT_NAME = re.compile(r"^(learn more|view event|register|details)$", re.I)

    def collect(self):
        found, links = {}, {}
        for url, page_gender in self.PAGES:
            try:
                lines = [re.sub(r"\s+", " ", l).strip() for l in
                         self.soup(url).get_text("\n", strip=True).split("\n") if l.strip()]
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug} {url}: {e}")
                continue
            for i, line in enumerate(lines):
                if not self.DATE_LINE.match(line):
                    continue
                start, end = parse_date_range(line)
                if not start:
                    continue
                name = city = None
                for nxt in lines[i + 1: i + 6]:
                    if self.CITY_LINE.match(nxt):
                        city = nxt
                        break
                    if nxt.lower().startswith("view event") or self.DATE_LINE.match(nxt):
                        break
                    if not name and 3 < len(nxt) < 70 and not self.NOT_NAME.match(nxt):
                        name = nxt
                if not name:
                    continue
                key = (name.lower(), start)
                gender = normalize_gender(name)
                if gender in (None, "", "unknown"):
                    gender = page_gender or "unknown"
                if key in found:
                    ev = found[key]
                    if page_gender and ev.gender not in (page_gender, "unknown", "coed"):
                        ev.gender = "coed"
                    elif ev.gender == "unknown":
                        ev.gender = gender
                    continue
                c, st = split_city_state(city)
                page = best_link(name, links.setdefault(url, site_links(self, url, r"victoryeventseries\.com/event/[^/]+/?$")))
                venue = address = None
                vsites = []
                if page:
                    text = page_text(self, page)
                    venue, c, st = merge_page_place(c, st, venue_from_page(text))
                    address = pick_address(text, c, st, venue)     # 'Maps & Locations ... 4000 Bay Road Frederica, DE 19946'
                    vsites = more_sites(text, [], st)              # 'Westtown School ... West Chester University ...'
                found[key] = Event(
                    organizer=self.organizer,
                    name=name.title() if name.isupper() else name,
                    start_date=start, end_date=end, raw_date=line,
                    venue=venue, city=c, state=st, gender=gender, address=address, sites=vsites if len(vsites) > 1 else [],
                    event_type="showcase" if "showcase" in name.lower() or "showcases" in url else "tournament",
                    source_url=page or url,
                )
        return list(found.values())


class HoganCollector(BaseCollector):
    """HoganLax. The tournaments index is a nav list with NO dates - the
    dates live on each /page/<slug>. Follow the links."""
    slug = "hogans"
    organizer = "Hogan's"
    tier = 2
    INDEX = "https://www.hoganlax.com/page/tournaments"
    BASE = "https://www.hoganlax.com"

    def collect(self):
        try:
            soup = self.soup(self.INDEX)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug}: {e}")
            return []
        hrefs = set()
        for a in soup.select("a[href]"):
            h = a.get("href", "")
            if "/page/" in h and not any(
                    k in h.lower() for k in
                    ("tournament", "training", "home", "roster", "insider", "contact", "camp")):
                hrefs.add(h if h.startswith("http") else self.BASE + h)

        out, seen = [], set()
        for url in list(hrefs)[:20]:
            try:
                page = self.soup(url)
            except Exception:  # noqa: BLE001
                continue
            title = (self.txt(page.select_one("h1")) or "").strip()
            body = re.sub(r"\s+", " ", page.get_text(" ", strip=True))[:4000]
            if not title:
                continue
            m = DATE_INLINE.search(body)
            if not m:
                continue
            start, end = parse_date_range(m.group(1))
            if not start:
                continue
            # 'DETAILS Dates, Times, Location November 8, 2026 Annapolis, MD' / '... Hershey, Pennsylvania'
            li = body.find("Dates, Times, Location")
            _, city, state = place_from_text(body[li + 22: li + 200] if li >= 0 else body[m.end(): m.end() + 150])
            # 'host a spring tournament at CopperPlex with 8 Field Turf fields' (not 'Format Register DETAILS')
            vm = re.search(r"\btournament\s+at\s+((?:[A-Z][\w'&.-]*\s?){1,5}?)(?=\s+(?:with|in|on)\b|[,.])", body)
            key = (title.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            # 'LOCATIONS & DIRECTIONS South River High School 201 Central Ave, Edgewater, MD ... Annapolis High School ...'
            sites = more_sites(re.sub(r"\s+", " ", page.get_text(" ", strip=True)), [], state)
            out.append(Event(
                organizer=self.organizer, name=title,
                start_date=start, end_date=end, raw_date=m.group(1),
                venue=sites[0][0] if len(sites) == 1 else vm.group(1).strip() if vm else None,
                address=sites[0][3] if len(sites) == 1 else pick_address(body, city, state, vm.group(1).strip() if vm else None),
                city=city, state=state, gender="boys", source_url=url, sites=sites if len(sites) > 1 else [],
            ))
        return out


class ML8Collector(BaseCollector):
    """ML8 Events. Dates are FRAGMENTED across lines by their date widget:
        'THE SPOTLIGHT' / 'October' / 'October' / '17' / '&' / '18' / '2026'
    Reassemble from the token window after each event name.
    Cards carry no place; each event's page shows it under the date ('DE Turf - Frederica, DE',
    'TBD, Maryland'), so we follow the card link. Never take a city from the page footer: that is
    ML8's own office (Miami, FL) and appears on every page."""
    slug = "ml8"
    organizer = "ML8"
    tier = 2
    URL = "https://www.ml8events.com/events"
    MONTH = re.compile(r"^(January|February|March|April|May|June|July|August|"
                       r"September|October|November|December)$", re.I)

    def place(self, url):
        """(venue, city, state) from an event page's place line, the visible .event-date.is-not-capitalized with a comma."""
        try:
            soup = self.soup(url)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug} {url}: {e}")
            return None, None, None, None
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        for d in soup.select(".event-date.is-not-capitalized"):
            t = self.txt(d) or ""
            if "w-condition-invisible" in (d.get("class") or []) or "," not in t:
                continue
            venue, _, loc = t.rpartition(" - ")
            city, state = split_city_state(loc)
            if city and city.upper() == "TBD":
                city = None
            return venue.strip() or None, city, state, pick_address(text, city, state, venue.strip() or None)
        return None, None, None, None

    def collect(self):
        soup = self.soup(self.URL)
        lines = [re.sub(r"\s+", " ", l).strip() for l in
                 soup.get_text("\n", strip=True).split("\n") if l.strip()]
        # card title -> its event page ('/events/sweetlax-summer-challenge'), where the place is
        pages = {}
        for a in soup.select("a.card-container[href]"):
            h = a.find("h3")
            if h and a["href"].startswith("/events/"):
                pages.setdefault(self.txt(h).lower(), urllib.parse.urljoin(self.URL, a["href"]))
        out, seen = [], set()
        for i, line in enumerate(lines):
            # an event name is an ALLCAPS-ish line immediately followed by a month token
            if not (i + 1 < len(lines) and self.MONTH.match(lines[i + 1])):
                continue
            name = line.strip()
            if not (3 < len(name) < 70) or self.MONTH.match(name):
                continue
            win = lines[i + 1: i + 14]
            months = [w for w in win if self.MONTH.match(w)]
            days = [w for w in win if re.fullmatch(r"\d{1,2}", w)]
            years = [w for w in win if re.fullmatch(r"20\d{2}", w)]
            if not (months and days and years):
                continue
            raw = f"{months[0]} {days[0]}"
            if len(days) > 1:
                raw += f"-{days[1]}"
            raw += f", {years[0]}"
            start, end = parse_date_range(raw)
            if not start:
                continue
            etype = "showcase" if any("showcase" in w.lower() for w in win) else "tournament"
            key = (name.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            page = pages.get(name.lower())
            venue, city, state, address = self.place(page) if page else (None, None, None, None)
            out.append(Event(
                organizer=self.organizer,
                name=name.title() if name.isupper() else name,
                start_date=start, end_date=end, raw_date=raw,
                venue=venue, city=city, state=state, gender="boys", address=address,
                event_type=etype, source_url=page or self.URL,
            ))
        return out


class PrimeTimeCollector(BaseCollector):
    """PrimeTime Lacrosse (Natick MA). Event grid gives name/date/city/divisions.
    'Launch Lacrosse' is a presenting-sponsor tag on the SAME events - do not
    treat it as a second slate.
    """
    slug = "primetime"
    organizer = "PrimeTime"
    tier = 2
    URL = "https://primetimelacrosse.com/events/"
    DATE_LINE = re.compile(
        r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?"
        r"(?:\s*[/&\u2013\u2014-]\s*(?:[A-Z][a-z]+\.?\s*)?\d{1,2}(?:st|nd|rd|th)?)?,?\s*20\d{2}$", re.I)

    @staticmethod
    def page_sites(text):
        """Every site an event page names: 'Location Bryant University, Smithfield, RI (27, 28 & 29) Location NEBC,
        Northborough, MA (29, 30)', 'Location: UMass Amherst, Amherst, MA (2027-2030)', and town lists
        'Location: Northborough / Bellingham / Wellesley, MA'. A town named with a venue is not repeated without one."""
        out = []
        # 'Location & Directions <venue> <street>, <town>, ST' repeats a site with its address: not another site
        pat = r"\bLocations?(?!\s*&)\s*:?\s+(?:\d{4}\s*-\s*\d{4}\s*\|\s*)?((?:(?!Locations?\b|Divisions\b|Pricing\b)[^|()]){3,120}?),\s*([A-Z]{2})\b"
        for m in re.finditer(pat, text or ""):
            body, st = m.group(1).strip(), m.group(2)
            if "/" in body and "," not in body:
                out += [(None, t.strip(), st) for t in body.split("/") if t.strip()]
            elif "," in body:
                venue, town = (x.strip() for x in body.rsplit(",", 1))
                out.append((venue if not JUNK_VENUE.search(venue) else None, town, st))
            else:
                out.append((None, body, st))
        # 'Location & Directions Cross Point Park 34 Camp Thomas Wy., North Kingstown, RI 02852': the fullest list, one
        # section per venue with its street address. When the page has them they lead; overview lines only add towns
        # (with a venue) that the directions don't cover ('Baltimore, MD' alone is the area, Calvert Hall in Towson the site)
        directions = [(m.group(1).strip(" ,-"), m.group(2).strip(), m.group(3).upper()) for m in re.finditer(
            r"Location\s*&\s*Directions\s+([^,]{3,80}?)\s+\d{1,6}\s+[^,]{2,60},\s*([A-Z][A-Za-z .'-]+?),\s*([A-Za-z]{2})\b",
            text or "")]
        if directions:
            towns = {(c.lower(), st) for _, c, st in directions}
            out = directions + [x for x in out if x[0] and (x[1].lower(), x[2]) not in towns]
        sites, by_town = [], {}
        for v, c, st in out:                                # one site per town, the first one that names a venue
            k = (c.lower(), st)
            if k in by_town:
                if v and not sites[by_town[k]][0]:
                    sites[by_town[k]] = (v, c, st)
                continue
            by_town[k] = len(sites)
            sites.append((v, c, st))
        return sites

    @staticmethod
    def page_place(text):
        m = re.search(r"Event Overview.{0,80}?\bDATE\b\s*(.{3,160}?)\s*\bDivisions\b", text or "", re.I)
        return place_from_text(re.sub(r"\bLocation\b", " ", m.group(1))) if m else (None, None, None)

    def collect(self):
        lines = [re.sub(r"\s+", " ", l).strip() for l in
                 self.soup(self.URL).get_text("\n", strip=True).split("\n") if l.strip()]
        links = site_links(self, self.URL, r"primetimelacrosse\.com/event/[^/]+/?$")
        out, seen = [], set()
        for i, line in enumerate(lines):
            if not self.DATE_LINE.match(line):
                continue
            start, end = parse_date_range(line)
            if not start:
                continue
            name = next((l for l in reversed(lines[max(0, i - 3): i])
                         if 3 < len(l) < 70 and not self.DATE_LINE.match(l)), None)
            if not name:
                continue
            loc = divisions = None
            for nxt in lines[i + 1: i + 4]:
                if re.search(r",\s*[A-Z]{2}\b", nxt) and not loc:
                    loc = nxt
                elif re.search(r"\b(Boys|Girls)\b", nxt, re.I) and not divisions:
                    divisions = nxt
            # one event at several towns -> one site each: 'Saratoga Springs & Queensbury, NY',
            # 'Northborough / Fitchburg, MA & Smithfield, RI', 'Amherst, Northborough, & Bellingham, MA'
            city, state = split_city_state((loc or "").split("&")[-1].strip())
            sites = self.towns(loc) if loc and re.search(r"[&/]", loc) else []
            key = (name.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            # the venue is on the event's own page: 'Event Overview ... DATE Location Rogers Field, Devens, MA Divisions'
            venue, page = None, best_link(name, links)
            if page:
                text = page_text(self, page)
                psites = self.page_sites(text)
                if len(psites) > 1 and any(v for v, _, _ in psites):
                    # the page names each site's venue ('NEBC, Northborough, MA'); each gets its street address
                    sites = [(v, c, st, pick_address(text, c, st, v)) for v, c, st in psites]
                elif len(psites) == 1 and psites[0][0] and psites[0][2] == state and re.search(r"Location\s*&\s*Directions", text):
                    # one venue with its street address ('Calvert Hall 8102 Lasalle Rd, Towson, MD'): more exact than the
                    # list's area ('Baltimore, MD'), so it replaces it
                    venue, city, sites = psites[0][0], psites[0][1], []
                elif not sites:
                    venue, city, state = merge_page_place(city, state, psites[0] if psites else self.page_place(text))
            out.append(Event(
                organizer=self.organizer, name=name, venue=venue,
                address=pick_address(text, city, state, venue) if page and not sites else None,
                start_date=start, end_date=end, raw_date=line,
                city=city, state=state,
                gender=normalize_gender((divisions or "") + " " + name),
                age_range=divisions, source_url=page or self.URL, sites=sites,
            ))
        return out


    @staticmethod
    def towns(loc):
        """[(None, town, ST), ...]: each state code closes the towns listed before it."""
        out, pending = [], []
        for tok in [t.strip(" .") for t in re.split(r"\s*(?:&|/|,)\s*", loc or "") if t.strip(" .")]:
            if re.fullmatch(r"[A-Z]{2}", tok):
                out += [(None, t, tok) for t in pending]
                pending = []
            else:
                pending.append(tok)
        return out if len(out) > 1 and not pending else []


class AdrenalineCollector(BaseCollector):
    """Adrenaline.

    TRAP 1: /events/ paginates behind 'Load More' (8 cards a click); month/category URL params
            do NOT filter server-side. The button POSTs admin-ajax action=load_more_events&page=N,
            which we call ourselves (more_cards) so every page of cards is read.
    TRAP 2: their cards split the date across lines:
            'Nov' / '07' / 'Gold Cup' / '2027 - 2032' / 'Frederica, DE' / 'Boys Only'
    TRAP 3: hub pages (/western-cup/) carry qualifier calendars the index omits.
    """
    slug = "adrenaline"
    organizer = "Adrenaline"
    tier = 2
    URLS = ["https://adrln.com/events/?sort=", "https://www.adrln.com/western-cup/"]
    MON = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$", re.I)
    MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()

    def season_year(self, mon, day=1):
        """The index shows month + day only: the next occurrence from today (an event 2+ months back means next year).
        Was a fixed Aug-Dec=2026 / Jan-Jul=2027 table, which would have dated next season a year off."""
        today = datetime.date.today()
        m = self.MONTHS.index(mon.lower()[:3]) + 1
        try:
            d = datetime.date(today.year, m, int(day))
        except ValueError:
            d = datetime.date(today.year, m, 1)
        return today.year + 1 if d < today - datetime.timedelta(days=60) else today.year

    def event_dates(self, url):
        """The full range from an event's own page ('Date & Time November 7 - 8, 2026'); the index card shows day 1 only."""
        try:
            text = re.sub(r"\s+", " ", self.soup(url).get_text(" ", strip=True))
        except Exception:  # noqa: BLE001
            return None, None, None
        i = text.find("Date & Time")
        m = DATE_INLINE.search(text, i if i >= 0 else 0) or DATE_INLINE.search(text)
        if not m:
            return None, None, None
        s, e = parse_date_range(m.group(1))
        return s, e, m.group(1)

    GENERIC = set("the a of and fall spring summer winter lacrosse lax tournament showcase classic cup clinic high school boys girls".split())

    def page_by_words(self, name, pages):
        """'Final Say: Fall High School Lacrosse' -> '/events/the-final-say-fall/': two distinctive words in common."""
        words = {w for w in re.findall(r"[a-z0-9]+", name.lower()) if w not in self.GENERIC}
        best = None
        for h in set(pages.values()):
            slug = {w for w in re.split(r"[-_]", h.rstrip("/").rsplit("/", 1)[-1]) if w not in self.GENERIC}
            n = len(words & slug)
            if n >= 2 and (best is None or n > best[0]): best = (n, h)
        return best[1] if best else None

    AJAX = "https://www.adrln.com/wp-admin/admin-ajax.php"
    HOME = "https://www.adrln.com/"

    def homepage_only(self, known_pages, out):
        """Events linked from the homepage but missing from /events/ ('Texas Thrive'): read their own pages."""
        try:
            soup = self.soup(self.HOME)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug} homepage: {e}")
            return []
        found = []
        known = {u.rstrip("/").replace("://adrln", "://www.adrln") for u in known_pages}
        for a in soup.find_all("a", href=True):
            h = urllib.parse.urljoin(self.HOME, a["href"]).split("?")[0].rstrip("/")
            if not re.search(r"adrln\.com/events/[^/]+$", h) or h.replace("://adrln", "://www.adrln") in known:
                continue
            known.add(h.replace("://adrln", "://www.adrln"))
            try:
                page = self.soup(h + "/")
            except Exception:  # noqa: BLE001
                continue
            title = re.split(r"\s+[-|–]\s+", self.txt(page.find("title")) or "")[0].strip()
            text = re.sub(r"\s+", " ", page.get_text(" ", strip=True))
            s, e, raw = self.event_dates(h + "/")
            if not title or not s or any(x.name.lower() == title.lower() and x.start_date == s for x in out):
                continue
            li = text.find("Location", text.find("Date & Time"))
            venue, city, state = place_from_text(text[li + 8: li + 200]) if li >= 0 else (None, None, None)
            found.append(Event(organizer=self.organizer, name=title, start_date=s, end_date=e or s, raw_date=raw,
                               address=pick_address(text, city, state, venue),
                               venue=venue, city=city, state=state, gender=normalize_gender(text[:3000]),
                               event_type="showcase" if "showcase" in title.lower() else "tournament", source_url=h + "/"))
        return found

    def more_cards(self, html):
        """The cards hidden behind 'Load More': same markup as the index, fetched page by page until one comes back empty."""
        m = re.search(r'class="load-more-btn"[^>]*data-max-pages="(\d+)"', html)
        if not m:
            return ""
        ids, extra = re.findall(r'data-post-id="(\d+)"', html), []
        for page in range(1, min(int(m.group(1)), 20) + 1):
            try:
                r = self.session.post(self.AJAX, data={"action": "load_more_events", "page": page, "search": "",
                                                      "catSearch": "", "exclude[]": ids}, timeout=30)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug} load more page {page}: {e}")
                break
            new = re.findall(r'data-post-id="(\d+)"', r.text) if r.status_code == 200 else []
            if not new:
                break
            ids += new
            extra.append(r.text)
            time.sleep(1.0)
        return "".join(extra)

    def collect(self):
        out, seen = [], set()
        for url in self.URLS:
            try:
                html = self.get(url)
                soup = BeautifulSoup(html + self.more_cards(html), "lxml")
                lines = [re.sub(r"\s+", " ", l).strip() for l in
                         soup.get_text("\n", strip=True).split("\n") if l.strip()]
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug} {url}: {e}")
                continue
            # card name -> its event page ('/events/gold-cup/'), for the full date range
            pages = {}
            for a in soup.find_all("a", href=True):
                h = urllib.parse.urljoin(url, a["href"]).split("?")[0]      # cards often use relative links
                if re.search(r"adrln\.com/events/[^/]+/?$", h) and not h.rstrip("/").endswith("/events"):
                    pages.setdefault(re.sub(r"[^a-z0-9]+", "", a.get_text(" ", strip=True).lower()), h)
                    pages.setdefault(re.sub(r"[-_]", "", h.rstrip("/").rsplit("/", 1)[-1]), h)

            for i, line in enumerate(lines):
                if not self.MON.match(line):
                    continue
                if not (i + 2 < len(lines) and re.fullmatch(r"\d{1,2}", lines[i + 1])):
                    continue
                mon, day = line, lines[i + 1]
                name = lines[i + 2].strip()
                if not (3 < len(name) < 70) or self.MON.match(name):
                    continue
                win = lines[i + 2: i + 9]
                # NOTE: never read the year out of the card window - those
                # '2027 - 2032' strings are DIVISION grad years, not event
                # years. The index only ever shows the current season, so
                # derive the year from the month instead.
                yr = self.season_year(mon, day)
                raw = f"{mon} {day}, {yr}"
                start, end = parse_date_range(raw)
                if not start:
                    continue
                key_name = re.sub(r"[^a-z0-9]+", "", name.lower())
                page = (pages.get(key_name) or next((h for k, h in pages.items() if k and (k in key_name or key_name in k)), None)
                        or self.page_by_words(name, pages))
                src = url
                loc = next((w for w in win if re.search(r",\s*[A-Z]{2}\b", w)), None)
                city, state = split_city_state(loc)
                venue = address = None
                if page:
                    s2, e2, raw2 = self.event_dates(page)
                    if s2 and abs((s2 - start).days) <= 7:      # same event: trust its own page's full range and venue
                        start, end, raw, src = s2, e2 or s2, raw2, page
                        text = page_text(self, page)
                        venue, city, state = merge_page_place(city, state, venue_from_page(text))
                        address = pick_address(text, city, state, venue)
                gender = normalize_gender(" ".join(win))
                key = (name.lower(), start)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Event(
                    organizer=self.organizer, name=name,
                    start_date=start, end_date=end, raw_date=raw,
                    venue=venue, city=city, state=state, gender=gender, address=address,
                    age_range=next((w for w in win if re.fullmatch(r"20\d{2}\s*-\s*20\d{2}", w)), None),
                    event_type="showcase" if "showcase" in name.lower() else "tournament",
                    source_url=src,
                ))

            # hub-page qualifier tables: 'Oct 17 | Santa Barbara Showdown | SB, CA'
            joined = " \n".join(lines)
            for m in re.finditer(
                    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
                    r"(?P<d1>\d{1,2})(?:\s*[-\u2013]\s*(?P<d2>\d{1,2}))?\s*\n?"
                    r"(?P<name>[A-Z][A-Za-z0-9 .'&-]{4,50})", joined):
                mon = m.group("mon")
                yr = self.season_year(mon, m.group("d1"))
                raw = f"{mon} {m.group('d1')}" + (f"-{m.group('d2')}" if m.group("d2") else "") + f", {yr}"
                start, end = parse_date_range(raw)
                nm = m.group("name").strip()
                if not start or len(nm) < 4:
                    continue
                key = (nm.lower(), start)
                if key in seen:
                    continue
                # a schedule-table row repeating a card already found ("Final Say" vs "Final Say: Fall High School Lacrosse")
                if any(abs((e.start_date - start).days) <= 3 and (e.name.lower().startswith(nm.lower()) or nm.lower().startswith(e.name.lower()))
                       for e in out):
                    continue
                seen.add(key)
                out.append(Event(
                    organizer=self.organizer, name=nm,
                    start_date=start, end_date=end, raw_date=raw,
                    source_url=url,
                ))
        out += self.homepage_only([e.source_url for e in out], out)
        return out


class NLFCollector(BaseCollector):
    """National Lacrosse Federation.

    WARNING: /events/fall-events/ is STALE - it still shows last season's
    dates. Correct dates live only on individual event pages, so we follow
    the links rather than trusting the index.
    """
    slug = "nlf"
    organizer = "NLF"
    tier = 2
    INDEX = "https://nationallacrossefederation.com/events/fall-events/"

    def collect(self):
        try:
            soup = self.soup(self.INDEX)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug}: {e}")
            return []
        links = {a["href"] for a in soup.select("a[href]")
                 if "/event" in a.get("href", "") and a.get("href", "").startswith("http")}
        out, seen = [], set()
        for url in list(links)[:25]:
            try:
                page = self.soup(url)
            except Exception:  # noqa: BLE001
                continue
            title = self.txt(page.select_one("h1")) or ""
            body = re.sub(r"\s+", " ", page.get_text(" ", strip=True))[:3000]
            m = DATE_INLINE.search(body)
            if not title or not m:
                continue
            start, end = parse_date_range(m.group(1))
            if not start:
                continue
            venue, city, state = merge_page_place(None, None, venue_from_page(body))   # 'Where: Farmingdale State College, ...'
            if not state:
                cm = re.search(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b", body)
                city, state = split_city_state(cm.group(1) if cm else None)
            key = (title.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            out.append(Event(
                organizer=self.organizer, name=title,
                start_date=start, end_date=end, raw_date=m.group(1),
                venue=venue, city=city, state=state, gender="boys", source_url=url,
                address=pick_address(body, city, state, venue),
            ))
        return out
