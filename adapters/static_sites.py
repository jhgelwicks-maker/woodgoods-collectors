"""Tier-1 collectors: static HTML, one page, no JS.

These are the cheap, high-yield sources. Verified against live markup.
"""
from __future__ import annotations
import re, datetime
from core.base import BaseCollector
from core.model import Event, parse_date_range, split_city_state, normalize_gender


class MLTCollector(BaseCollector):
    """My Lacrosse Tournaments - richest single source in the set.

    Markup: div.rb-event-card
              .rb-event-badge   -> 'Tournament' / 'Showcase'
              .rb-event-title   -> name
              .rb-event-details > .rb-detail-item x4
                   [0] date   [1] 'Venue, ST'   [2] gender   [3] divisions
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
                source_url=link["href"] if link else self.URL,
            ))
        return out


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
                block = []
                for sib in h.find_all_next(limit=8):
                    if sib.name in ("h1", "h2", "h3", "h4"):
                        break
                    t = self.txt(sib)
                    if t:
                        block.append(t)
                blob = " ".join(block)[:600]
                m = self.DATE_RE.search(blob) or self.DATE_RE.search(name)
                if not m:
                    continue
                raw = m.group(1)
                start, end = parse_date_range(raw, default_year=datetime.date.today().year)
                if not start:
                    continue
                city, state = self._location(blob)
                out.append(Event(
                    organizer=self.organizer, name=name,
                    start_date=start, end_date=end, raw_date=raw,
                    city=city, state=state, gender=gender,
                    event_type=etype, source_url=url,
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
        out, seen = [], set()
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
            out.append(Event(
                organizer=self.organizer, name=name,
                start_date=start, end_date=end, raw_date=line,
                city=c, state=st,
                gender=normalize_gender(name),
                event_type="showcase" if "showcase" in name.lower() else "tournament",
                source_url=url,
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

    def collect(self):
        raw = self.soup(self.URL).get_text("\n", strip=True).replace("\xa0", " ")
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
            out.append(Event(
                organizer=self.organizer, name=name.title() if name.isupper() else name,
                start_date=start, end_date=end, raw_date=m.group("date"),
                city=city, state=state,
                gender=normalize_gender(divisions),
                age_range=divisions or None,
                source_url=self.URL,
            ))
        return out
