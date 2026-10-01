"""LeagueApps adapter.

Several organizers in this set run on LeagueApps. The signature is a
registration host that serves per-event detail at:

    {host}/site/register/register.aspx?EventID={id}

The *marketing* site is often JS-gated and useless to a fetcher
(www.nxtsports.com returns nav chrome only), while the registration host
serves full server-rendered detail: name, dates, venue, street address,
divisions and team pricing.

Two modes:
  known_ids  - fast, exact. Use the IDs you already have.
  scan       - probe an ID range to discover new events. Slower and
               noisier; run it monthly, not weekly.
"""
from __future__ import annotations
import re
from core.base import BaseCollector, FetchError
from core.model import Event, parse_date_range, split_city_state, normalize_gender

DATE_RE = re.compile(
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}"
    r"(?:\s*[-\u2013]\s*(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*)?"
    r"\d{1,2})?,?\s*20\d{2})", re.I)

ADDR_RE = re.compile(
    r"(\d{2,6}\s+[A-Z][A-Za-z0-9 .'-]{3,40}"
    r"(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Pike|Highway|Hwy|Court|Ct)\.?"
    r"[, ]+[A-Z][A-Za-z .'-]+,?\s*[A-Z]{2}\s*\d{5}?)")

CITYST_RE = re.compile(r"([A-Z][A-Za-z .'-]+),\s*([A-Z]{2})\b")


class LeagueAppsCollector(BaseCollector):
    """Subclass and set `host`, plus `known_ids` and/or `scan_range`."""

    tier = 1
    host: str = ""
    known_ids: list[int] = []
    scan_range: tuple[int, int] | None = None
    default_gender: str = "unknown"

    def event_url(self, eid):
        return f"{self.host}/site/register/register.aspx?EventID={eid}"

    def collect(self):
        ids = list(self.known_ids)
        if self.scan_range:
            ids += [i for i in range(*self.scan_range) if i not in set(ids)]
        out = []
        for eid in ids:
            try:
                ev = self.parse_event(eid)
            except FetchError:
                continue
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug}#{eid}: {e}")
                continue
            if ev:
                out.append(ev)
        return out

    def parse_event(self, eid):
        """LeagueApps renders a tight header block:

            2026 Boys Jersey Fall Invitational
            Nov 8, 2026
            Capelli Sport Complex - Monmouth County, NJ
            Register
            Divisions: 2027-2030

        Anchor on the <h1> title, then read the next few lines. Far more
        reliable than regexing the whole page, which picks up marketing copy.
        """
        url = self.event_url(eid)
        soup = self.soup(url)

        title = (self.txt(soup.select_one("h1")) or "").strip()
        if not title:
            title = re.sub(r"\s*\|.*$", "", self.txt(soup.select_one("title")) or "").strip()
        if not title or "not found" in title.lower():
            return None

        lines = [l.strip() for l in soup.get_text("\n", strip=True).split("\n") if l.strip()]
        try:
            i = next(i for i, l in enumerate(lines) if l == title)
        except StopIteration:
            i = next((i for i, l in enumerate(lines) if title[:28] in l), None)
            if i is None:
                return None

        block = lines[i + 1: i + 8]

        raw_date = next((l for l in block if DATE_RE.fullmatch(l) or DATE_RE.match(l)), None)
        if not raw_date:
            return None
        start, end = parse_date_range(raw_date)
        if not start:
            return None

        # The line after the date is the location. It may be
        #   'Capelli Sport Complex - Monmouth County, NJ'  (venue + city/state)
        #   'United Sports Training Center'                (venue only)
        #   'Multiple Locations'                           (neither)
        venue = city = state = None
        di = block.index(raw_date) if raw_date in block else -1
        loc_line = None
        for l in block[di + 1:] if di >= 0 else block:
            if l.lower() in ("register", "details", "hotels") or l.lower().startswith("divisions"):
                continue
            loc_line = l
            break

        if loc_line and loc_line.lower() != "multiple locations":
            cm = CITYST_RE.search(loc_line)
            if cm:
                state = cm.group(2)
                head = loc_line[:cm.start()].strip(" ,-\u2013")
                tail = cm.group(1).strip()
                if " - " in loc_line:
                    v, _, c = loc_line.rpartition(" - ")
                    venue = v.strip() or None
                    city = c.split(",")[0].strip() or None
                else:
                    venue = head or None
                    city = tail or None
            else:
                venue = loc_line
        elif loc_line:
            venue = loc_line  # 'Multiple Locations' is still worth recording

        divisions = next((l.split(":", 1)[1].strip() for l in block
                          if l.lower().startswith("divisions")), None)

        name = re.sub(r"^20\d{2}\s+", "", title).strip()
        body = re.sub(r"\s+", " ", " ".join(lines[i: i + 60]))[:2500]
        teams = None
        tm = re.search(r"(\d{2,3})\s+college coaches", body, re.I)
        if tm:
            teams = f"{tm.group(1)} college coaches"

        return Event(
            organizer=self.organizer, name=name,
            start_date=start, end_date=end, raw_date=raw_date,
            venue=venue, city=city, state=state,
            gender=(self.default_gender if self.default_gender != "unknown"
                    else normalize_gender(name + " " + body[:800])),
            age_range=divisions, team_count=teams,
            event_type="showcase" if "showcase" in name.lower() else "tournament",
            source_url=url,
        )


class NXTCollector(LeagueAppsCollector):
    """NXT Sports - biggest organizer in the set.

    www.nxtsports.com is JS-gated; events.nxtsports.com is not.
    IDs below are the confirmed 2026-27 boys slate. Add new ones as they
    appear, or enable scan_range once a year to discover them.
    """
    slug = "nxt"
    organizer = "NXT"
    host = "https://events.nxtsports.com"
    default_gender = "boys"
    known_ids = [
        19031,  # The Circuit Session I
        19660,  # Harvest Classic
        19032,  # Boys Fall Grand Prix
        19068,  # Fall Bulldog Bash Youth
        19067,  # Fall Bulldog Bash HS
        19072,  # Boys Fall Middle School Invitational
        19069,  # Can-Am Youth Invitational
        19099,  # Can-Am Showcase
        19070,  # Boys Fall Can-Am Invitational
        19071,  # Boys Jersey Fall Invitational
        19073,  # Philly Boys Fall Invitational
        19078,  # High School Challenge
        21286,  # Constitution Box Cup
        20418,  # Boys Connecticut Box Championships
        20449,  # East Coast Box Championships
        21089,  # Top 12 Invitational
        19127,  # Boys Philadelphia Indoor Championships
    ]


class AlohaCollector(LeagueAppsCollector):
    """Aloha Tournaments. Registration portal is JS-driven; per-event IDs work."""
    slug = "aloha"
    organizer = "Aloha"
    host = "https://www.alohatournaments.com"
    tier = 2
    known_ids = [18264, 18287, 18285, 20759]


class AllianceCollector(LeagueAppsCollector):
    """Alliance Lacrosse League. Low yield - one fall event."""
    slug = "alliance"
    organizer = "Alliance"
    host = "https://register.thealliancelacrosseleague.com"
    tier = 2
    default_gender = "boys"
    known_ids: list[int] = []

    def collect(self):
        """No stable IDs captured yet; fall back to the register index."""
        out = []
        try:
            soup = self.soup(f"{self.host}/site/register/")
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug}: {e}")
            return out
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        for m in DATE_RE.finditer(text):
            start, end = parse_date_range(m.group(1))
            if not start:
                continue
            window = text[max(0, m.start() - 120): m.start()]
            nm = re.findall(r"([A-Z][A-Za-z0-9 .'&-]{6,50})\s*$", window)
            if not nm:
                continue
            out.append(Event(
                organizer=self.organizer, name=nm[-1].strip(" ,-"),
                start_date=start, end_date=end, raw_date=m.group(1),
                gender="boys", source_url=f"{self.host}/site/register/",
            ))
        return out
