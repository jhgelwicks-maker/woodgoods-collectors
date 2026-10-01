"""Tier 1-2 collectors for the remaining organizers."""
from __future__ import annotations
import re
from core.base import BaseCollector
from core.model import Event, parse_date_range, split_city_state, normalize_gender

DATE_INLINE = re.compile(
    r"((?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Za-z]*\.?\s+\d{1,2}"
    r"(?:\s*(?:[-\u2013+&/]|to|and)\s*(?:(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
    r"[A-Za-z]*\.?\s*)?\d{1,2})?,?\s*20\d{2})", re.I)


class TrilogyCollector(BaseCollector):
    """Trilogy Lacrosse.

    The NAV MENU carries the whole slate, pre-split by gender:
        'Midwest Momentum OCT 24 - 25, 2026 | OH'
    Parse the menu links, not the page body. Venue needs a second hop,
    which we skip by default - state is enough to route an operator.
    """
    slug = "trilogy"
    organizer = "Trilogy"
    tier = 2
    URL = "https://trilogylacrosse.com/tournaments/"
    LINK_RE = re.compile(
        r"^(?P<name>.+?)\s+(?P<date>(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)"
        r"[A-Za-z]*\.?\s+\d{1,2}.*?20\d{2})\s*\|\s*(?P<state>[A-Z]{2})\s*$", re.I)

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
            gender = self._gender_from_menu(a)
            out.append(Event(
                organizer=self.organizer,
                name=name.title() if name.isupper() else name,
                start_date=start, end_date=end, raw_date=m.group("date"),
                state=m.group("state").upper(), gender=gender,
                event_type=self._type_from_menu(a, name),
                source_url=a.get("href", self.URL),
            ))
        return out

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
    """Victory Event Series. Homepage 'upcoming events' block is clean:
        'October 24 & 25, 2026' / 'VICTORY FALL CLASSIC' / 'Frederica, DE'
    """
    slug = "victory"
    organizer = "Victory"
    tier = 1
    URL = "https://victoryeventseries.com/"
    DATE_LINE = re.compile(
        r"^(January|February|March|April|May|June|July|August|September|October|"
        r"November|December)\s+\d{1,2}\s*(?:[-\u2013&]\s*(?:[A-Z][a-z]+\s+)?\d{1,2})?,?\s*20\d{2}$",
        re.I)
    CITY_LINE = re.compile(r"^[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}$")

    def collect(self):
        lines = [re.sub(r"\s+", " ", l).strip() for l in
                 self.soup(self.URL).get_text("\n", strip=True).split("\n") if l.strip()]
        out, seen = [], set()
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
                if not name and 3 < len(nxt) < 70:
                    name = nxt
            if not name:
                continue
            c, st = split_city_state(city)
            key = (name.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            out.append(Event(
                organizer=self.organizer,
                name=name.title() if name.isupper() else name,
                start_date=start, end_date=end, raw_date=line,
                city=c, state=st, gender=normalize_gender(name),
                source_url=self.URL,
            ))
        return out


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
            cm = re.search(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b", body)
            city, state = split_city_state(cm.group(1) if cm else None)
            vm = re.search(r"(?:at|@)\s+([A-Z][A-Za-z0-9 .'&-]{5,45})", body)
            key = (title.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            out.append(Event(
                organizer=self.organizer, name=title,
                start_date=start, end_date=end, raw_date=m.group(1),
                venue=vm.group(1).strip() if vm else None,
                city=city, state=state, gender="boys", source_url=url,
            ))
        return out


class ML8Collector(BaseCollector):
    """ML8 Events. Dates are FRAGMENTED across lines by their date widget:
        'THE SPOTLIGHT' / 'October' / 'October' / '17' / '&' / '18' / '2026'
    Reassemble from the token window after each event name."""
    slug = "ml8"
    organizer = "ML8"
    tier = 2
    URL = "https://www.ml8events.com/events"
    MONTH = re.compile(r"^(January|February|March|April|May|June|July|August|"
                       r"September|October|November|December)$", re.I)

    def collect(self):
        lines = [re.sub(r"\s+", " ", l).strip() for l in
                 self.soup(self.URL).get_text("\n", strip=True).split("\n") if l.strip()]
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
            loc = next((w for w in win if re.search(r",\s*[A-Z]{2}\b", w)), None)
            city, state = split_city_state(loc)
            key = (name.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            out.append(Event(
                organizer=self.organizer,
                name=name.title() if name.isupper() else name,
                start_date=start, end_date=end, raw_date=raw,
                city=city, state=state, gender="boys",
                event_type=etype, source_url=self.URL,
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
        r"(?:\s*[/&-]\s*(?:[A-Z][a-z]+\.?\s*)?\d{1,2}(?:st|nd|rd|th)?)?,?\s*20\d{2}$", re.I)

    def collect(self):
        lines = [re.sub(r"\s+", " ", l).strip() for l in
                 self.soup(self.URL).get_text("\n", strip=True).split("\n") if l.strip()]
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
            city, state = split_city_state((loc or "").split("&")[-1].strip())
            key = (name.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            out.append(Event(
                organizer=self.organizer, name=name,
                start_date=start, end_date=end, raw_date=line,
                city=city, state=state,
                gender=normalize_gender((divisions or "") + " " + name),
                age_range=divisions, source_url=self.URL,
            ))
        return out


class AdrenalineCollector(BaseCollector):
    """Adrenaline.

    TRAP 1: /events/ paginates behind 'Load More'; month/category URL params
            do NOT filter server-side. '?sort=' returns a fuller roster.
    TRAP 2: their cards split the date across lines:
            'Nov' / '07' / 'Gold Cup' / '2027 - 2032' / 'Frederica, DE' / 'Boys Only'
    TRAP 3: hub pages (/western-cup/) carry qualifier calendars the index omits.
    """
    slug = "adrenaline"
    organizer = "Adrenaline"
    tier = 2
    URLS = ["https://adrln.com/events/?sort=", "https://www.adrln.com/western-cup/"]
    MON = re.compile(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)$", re.I)
    SEASON_YEAR = {"jan": 2027, "feb": 2027, "mar": 2027, "apr": 2027, "may": 2027,
                   "jun": 2027, "jul": 2027, "aug": 2026, "sep": 2026, "oct": 2026,
                   "nov": 2026, "dec": 2026}

    def collect(self):
        out, seen = [], set()
        for url in self.URLS:
            try:
                lines = [re.sub(r"\s+", " ", l).strip() for l in
                         self.soup(url).get_text("\n", strip=True).split("\n") if l.strip()]
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug} {url}: {e}")
                continue

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
                yr = self.SEASON_YEAR[mon.lower()[:3]]
                raw = f"{mon} {day}, {yr}"
                start, end = parse_date_range(raw)
                if not start:
                    continue
                loc = next((w for w in win if re.search(r",\s*[A-Z]{2}\b", w)), None)
                city, state = split_city_state(loc)
                gender = normalize_gender(" ".join(win))
                key = (name.lower(), start)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Event(
                    organizer=self.organizer, name=name,
                    start_date=start, end_date=end, raw_date=raw,
                    city=city, state=state, gender=gender,
                    age_range=next((w for w in win if re.fullmatch(r"20\d{2}\s*-\s*20\d{2}", w)), None),
                    event_type="showcase" if "showcase" in name.lower() else "tournament",
                    source_url=url,
                ))

            # hub-page qualifier tables: 'Oct 17 | Santa Barbara Showdown | SB, CA'
            joined = " \n".join(lines)
            for m in re.finditer(
                    r"(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
                    r"(?P<d1>\d{1,2})(?:\s*[-\u2013]\s*(?P<d2>\d{1,2}))?\s*\n?"
                    r"(?P<name>[A-Z][A-Za-z0-9 .'&-]{4,50})", joined):
                mon = m.group("mon")
                yr = self.SEASON_YEAR[mon.lower()[:3]]
                raw = f"{mon} {m.group('d1')}" + (f"-{m.group('d2')}" if m.group("d2") else "") + f", {yr}"
                start, end = parse_date_range(raw)
                nm = m.group("name").strip()
                if not start or len(nm) < 4:
                    continue
                key = (nm.lower(), start)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Event(
                    organizer=self.organizer, name=nm,
                    start_date=start, end_date=end, raw_date=raw,
                    source_url=url,
                ))
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
            cm = re.search(r"([A-Z][A-Za-z .'-]+,\s*[A-Z]{2})\b", body)
            city, state = split_city_state(cm.group(1) if cm else None)
            key = (title.lower(), start)
            if key in seen:
                continue
            seen.add(key)
            out.append(Event(
                organizer=self.organizer, name=title,
                start_date=start, end_date=end, raw_date=m.group(1),
                city=city, state=state, gender="boys", source_url=url,
            ))
        return out
