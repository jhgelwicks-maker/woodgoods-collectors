"""Tier-1 collectors: static HTML, one page, no JS.

These are the cheap, high-yield sources. Verified against live markup.
"""
from __future__ import annotations
import re, datetime
from core.base import BaseCollector
from adapters import details
from core.model import Event, parse_date_range, split_city_state, normalize_gender


class MLTCollector(BaseCollector):
    """My Lacrosse Tournaments - richest single source in the set.

    Markup: div.rb-event-card
              .rb-event-badge   -> 'Tournament' / 'Showcase'
              .rb-event-title   -> name
              .rb-event-details > .rb-detail-item x4
                   [0] date   [1] 'Venue, ST'   [2] gender   [3] divisions
    These pages list upcoming events only, so a card dated in the past carries a
    mistyped year (Carolina Box Championship 'February 6th-7th, 2026' on the
    2026-27 slate): it is read as the next year and the row says so in date_note.
    """
    slug = "mlt"
    organizer = "MLT"
    tier = 1
    URL = "https://mylacrossetournaments.com/events/"
    # /events/ leaves out showcases and some regional events; each listing below has its own cards
    PAGES = [URL, URL + "showcase/", URL + "girls/", URL + "east/", URL + "south/", URL + "west/"]

    def collect(self):
        out, seen = [], set()
        for url in self.PAGES:
            try:
                soup = self.soup(url)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug} {url}: {e}")
                continue
            out += self._cards(soup, seen)
        return out

    def _cards(self, soup, seen):
        out = []
        for card in soup.select(".rb-event-card"):
            name = self.txt(card.select_one(".rb-event-title"))
            if not name:
                continue
            link = card.select_one("a[href]")
            key = link["href"] if link else name.lower()
            if key in seen:
                continue
            seen.add(key)
            items = [self.txt(d) for d in card.select(".rb-event-details .rb-detail-item")]
            items += [None] * (4 - len(items))
            raw_date, loc, gender_s, divisions = items[:4]

            start, end = parse_date_range(raw_date)
            start, end, note = self._year_typo(start, end, raw_date)
            venue, state = split_city_state(loc)
            badge = (self.txt(card.select_one(".rb-event-badge")) or "").lower()

            out.append(Event(
                organizer=self.organizer,
                name=name,
                start_date=start, end_date=end, raw_date=raw_date or "",
                venue=venue, state=state,
                gender=normalize_gender(gender_s or ""),
                age_range=divisions,
                event_type="showcase" if "showcase" in badge else "tournament",
                source_url=link["href"] if link else self.URL, date_note=note,
            ))
        return out

    @staticmethod
    def _year_typo(start, end, raw_date):
        """A past date on an upcoming-only list -> the same dates a year later, with a note; anything that would still
        be past a year on (a stale or truly old card) is left alone."""
        today = datetime.date.today()
        if not start or (end or start) >= today - datetime.timedelta(days=3):
            return start, end, None

        def plus_year(d):
            try:
                return d.replace(year=d.year + 1)
            except ValueError:                       # Feb 29
                return d + datetime.timedelta(days=365)
        s2, e2 = plus_year(start), plus_year(end or start)
        if e2 < today:
            return start, end, None
        return s2, e2, (f"site prints '{raw_date}' on its upcoming-events list; read as {s2.year} "
                        f"(year looks mistyped, confirm with the organizer)")


class ApexCollector(BaseCollector):
    """APEX Lacrosse Events. Gender is segregated by URL path, so it's free.
    Each page lists upcoming events, then a 'Past Events' heading; stop there."""
    slug = "apex"
    organizer = "APEX"
    tier = 1
    PAGES = [
        ("https://apexlacrosseevents.com/boys-tournaments/", "boys", "tournament"),
        ("https://apexlacrosseevents.com/boys-showcases/",   "boys", "showcase"),
        ("https://apexlacrosseevents.com/girls-tournaments/", "girls", "tournament"),
        ("https://apexlacrosseevents.com/girls-showcases/",   "girls", "showcase"),
    ]
    STREET = re.compile(r"^.*\b(?:rd|road|ln|lane|st|street|ave|avenue|blvd|dr|drive|hwy|highway|pike|way|"
                        r"ct|court|pl|place|pkwy|parkway|area|grounds)\b\.?\s+", re.I)
    DATE_RE = re.compile(
        r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}"
        r"(?:\s*(?:-|–|&|/|and|to)\s*(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
        r"[a-z]*\.?\s*)?\d{1,2})?(?:,?\s*20\d{2})?)", re.I)

    def collect(self):
        out = []
        for url, gender, etype in self.PAGES:
            try:
                soup = self.soup(url)
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug} {url}: {e}")
                continue
            for h in soup.select("h1, h2, h3, h4"):
                name = self.txt(h)
                if name and name.strip().lower() == "past events":
                    break
                if not name or len(name) > 120:
                    continue
                if re.fullmatch(r"(?i)(?:boys|girls)?\s*(?:showcases?|tournaments?|events?)", name.strip()):
                    continue                            # a section heading ('Boys Showcases'), not an event
                # everything up to the next heading; class-year badges ('2030' '2029' '2028') used to push the location
                # past an 8-element window, so the events with the most age groups lost their place
                block, loc_text = [], None
                for sib in h.find_all_next(limit=40):
                    if sib.name in ("h1", "h2", "h3", "h4"):
                        break
                    if loc_text is None and "location" in (sib.get("class") or []):
                        loc_text = self.txt(sib)        # 'University of Delaware 631 S College Ave Newark, DE 19716'
                    t = self.txt(sib)
                    if t:
                        block.append(t)
                blob = " ".join(block)[:1500]
                m = self.DATE_RE.search(blob) or self.DATE_RE.search(name)
                if not m:
                    continue
                raw = m.group(1)
                start, end = parse_date_range(raw, default_year=datetime.date.today().year)
                if not start:
                    continue
                venue = address = None
                if loc_text:
                    from adapters.misc_sites import place_from_text, addresses_in
                    venue, city, state = place_from_text(loc_text)
                    found_addr = addresses_in(loc_text)         # 'University of Delaware 631 S College Ave Newark, DE 19716'
                    address = found_addr[0][1] if found_addr else None
                if not loc_text or not state:
                    venue, address, (city, state) = None, None, self._location(blob)
                out.append(Event(
                    organizer=self.organizer, name=name,
                    start_date=start, end_date=end, raw_date=raw,
                    venue=venue, city=city, state=state, gender=gender, address=address,
                    event_type=etype, source_url=url,
                    age_range=details.grad_years(blob, start),      # the card's 'Classes: 2030 2029 2028 2027' badges
                ))
        return self._dedupe(out)

    @classmethod
    def _location(cls, blob):
        m = re.search(r"([A-Z][A-Za-z .'-]+),\s*([A-Z]{2})\b", blob)
        if not m:
            return None, None
        return cls.STREET.sub("", m.group(1).strip()).strip() or None, m.group(2)

    @staticmethod
    def _dedupe(events):
        seen, keep = set(), []
        for e in events:
            if e.fingerprint in seen:
                continue
            seen.add(e.fingerprint)
            keep.append(e)
        return keep


class BukuCollector(BaseCollector):
    """Buku Events. Layout is a flat text run:
        'October 24-25, 2026' / name (may wrap over 1-3 lines) /
        'Santa Barbara, CA' / 'View Event'
    Walk forward from each date line to the city line; everything between
    is the name. City only - Buku never publishes a venue.
    """
    slug = "buku"
    organizer = "Buku"
    tier = 1
    URL = "https://www.buku.events/tournaments"
    PAGES = [URL, "https://www.buku.events/showcases"]      # showcases are only on their own page
    DATE_RE = re.compile(
        r"^(January|February|March|April|May|June|July|August|September|October|"
        r"November|December)\s+\d{1,2}\s*(?:[-\u2013]\s*\d{1,2})?\s*,\s*20\d{2}$", re.I)
    CITY_RE = re.compile(r"^[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}$")

    def collect(self):
        out, seen, self._links = [], set(), {}
        for url in self.PAGES:
            try:
                lines = [l.strip() for l in
                         self.soup(url).get_text("\n", strip=True).split("\n") if l.strip()]
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug} {url}: {e}")
                continue
            out += self._events(lines, url, seen)
        return out

    def _events(self, lines, url, seen):
        out = []
        for i, line in enumerate(lines):
            if not self.DATE_RE.match(line):
                continue
            start, end = parse_date_range(line)
            if not start:
                continue
            name_parts, city = [], None
            for nxt in lines[i + 1: i + 7]:
                if self.CITY_RE.match(nxt):
                    city = nxt
                    break
                if nxt.lower().startswith("view event"):
                    break
                name_parts.append(nxt)
            name = " ".join(name_parts).strip()
            if not name:
                continue
            c, st = split_city_state(city)
            key = (name.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            # the venue is on the event's page: 'Venue & Location Santa Barbara Polo Club 3300 Via Real, Carpinteria, CA'
            from adapters.misc_sites import best_link, page_text, venue_from_page, site_links, merge_page_place, pick_address
            page = best_link(name, self._links.setdefault(url, site_links(self, url, r"buku\.events/[a-z0-9-]+/?(\?|$)")))
            venue = address = None
            if page:
                text = page_text(self, page)
                pv, pc, pst = venue_from_page(text)
                paddr = pick_address(text, pc, pst, pv) if pv else None
                if paddr and pst == st:
                    # 'Santa Barbara Polo Club 3300 Via Real, Carpinteria, CA 93013' on the Santa Barbara Fall Brawl page:
                    # a street address is exact, so its town replaces the list's nearby one
                    venue, c, address = pv, pc, paddr
                else:
                    venue, c, st = merge_page_place(c, st, (pv, pc, pst))
                    address = pick_address(text, c, st, venue)
            out.append(Event(
                organizer=self.organizer, name=name, venue=venue, address=address,
                start_date=start, end_date=end, raw_date=line,
                city=c, state=st,
                gender=normalize_gender(name),
                event_type="showcase" if "showcase" in name.lower() else "tournament",
                source_url=page or url,
            ))
        return out


class PLLPlayCollector(BaseCollector):
    """PLL Play. Repeating 4-line block:
        'High Peaks Sixes'
        'OCTOBER 10-11, 2026   |   LAKE PLACID, NY'
        'BOYS 2035-2028'
        'Find Out More'
    The name is the line BEFORE the date; divisions/gender the line after.
    """
    slug = "pll-play"
    organizer = "PLL"
    tier = 1
    URL = "https://premierlacrosseleague.com/play/tournaments"
    ROW_RE = re.compile(
        r"^(?P<date>(?:JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|"
        r"OCTOBER|NOVEMBER|DECEMBER)\s+\d{1,2}\s*(?:[-\u2013]\s*(?:[A-Z]+\s+)?\d{1,2})?"
        r",?\s*20\d{2})\s*\|\s*(?P<loc>.+)$", re.I)

    @staticmethod
    def page_sites(text):
        """[(venue, city, state), ...] from a pllplay.com page: 'Location Riverview Sports Park Address 12200 Joerger Drive,
        Truckee, CA 96145 ... Location Truckee High School Address ...', else 'Arvada, CO: Stenger Soccer Complex'."""
        from collectors.organizers import extract as X
        out = []
        for m in re.finditer(r"\bLocation\s+(.{3,80}?)\s+Address\s+(.{5,140}?\b[A-Z]{2}\s+\d{5})", text or ""):
            towns = X.find_city_states(m.group(2))
            if towns:
                c, st = towns[-1].rsplit(", ", 1)
                out.append((m.group(1).strip(), c, st, re.sub(r"\s+", " ", m.group(2)).strip()))
        if not out:
            m = re.search(r"\b([A-Z][A-Za-z .'-]+),\s*([A-Z]{2})\s*:\s*([A-Z][\w '&.-]{3,60}?)(?=\s+[A-Z][a-z]+\s+[a-z]|\s{2}|$)", text or "")
            if m:
                out.append((m.group(3).strip(), m.group(1).strip(), m.group(2), None))
        return out

    def collect(self):
        from adapters.misc_sites import best_link, page_text, merge_page_place, more_sites
        soup = self.soup(self.URL)
        import urllib.parse
        links = [urllib.parse.urljoin(self.URL, a["href"]) for a in soup.find_all("a", href=True)
                 if "find out" in a.get_text(" ", strip=True).lower()]
        raw = soup.get_text("\n", strip=True).replace("\xa0", " ")
        lines = [re.sub(r"\s+", " ", l).strip() for l in raw.split("\n") if l.strip()]
        out, seen = [], set()
        for i, line in enumerate(lines):
            m = self.ROW_RE.match(line)
            if not m:
                continue
            start, end = parse_date_range(m.group("date"))
            if not start:
                continue
            name = lines[i - 1].strip() if i else ""
            if not name or len(name) > 80:
                continue
            divisions = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if divisions.lower().startswith("find out"):
                divisions = ""
            city, state = split_city_state(m.group("loc").title())
            key = (name.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            page = best_link(name, links)
            ptext = page_text(self, page) if page else ""
            sites = self.page_sites(ptext) if page else []
            sites = more_sites(ptext, sites, state)          # 'Field Location(s) NORTH ELBA ATHLETIC FIELDS ... SARANAC LAKE HS ...'
            sites = [x for x in sites if x[2] == (state or x[2])]       # a page about somewhere else is ignored
            venue = address = None
            if len(sites) == 1:
                venue, city, state = merge_page_place(city, state, sites[0][:3])
                address = sites[0][3] if venue else None
            elif sites:
                venue, address = sites[0][0], sites[0][3]              # several sites: each keeps its own town and address
            out.append(Event(
                organizer=self.organizer, name=name.title() if name.isupper() else name,
                start_date=start, end_date=end, raw_date=m.group("date"), address=address,
                venue=venue, city=city, state=state, sites=sites if len(sites) > 1 else [],
                gender=normalize_gender(divisions),
                age_range=divisions or None,
                source_url=page or self.URL,
            ))
        return out
