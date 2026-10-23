"""LeagueApps adapter.

Several organizers in this set run on LeagueApps. The signature is a
registration host that serves per-event detail at:

    {host}/site/register/register.aspx?EventID={id}

The *marketing* site is often JS-gated and useless to a fetcher
(www.nxtsports.com returns nav chrome only), while the registration host
serves full server-rendered detail: name, dates, venue, street address,
divisions and team pricing.

Three ways to get event IDs (combined, feed first):
  feed       - the marketing site's own event list, /api/program (what its JS
               events page loads). Finds every listed event, including ones
               added since the IDs below were typed in. Needs the site's
               affiliation id(s), read once from a browser network capture.
  known_ids  - fast, exact. Fallback if the feed is down.
  scan       - probe an ID range to discover new events. Slower and
               noisier; run it monthly, not weekly.
"""
from __future__ import annotations
import re, json, datetime
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
US_STATE = {n.lower(): a for a, n in {
    "AL": "Alabama", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
    "WY": "Wyoming"}.items()}


class LeagueAppsCollector(BaseCollector):
    """Subclass and set `host`, plus `known_ids` and/or `scan_range`."""

    tier = 1
    host: str = ""
    known_ids: list[int] = []
    scan_range: tuple[int, int] | None = None
    default_gender: str = "unknown"
    feed_site: str = ""                 # e.g. https://www.nxtsports.com (serves /api/program)
    feed_affiliations: list[int] = []   # affiliationIds the site's events page sends
    feed_current: int | None = None     # currentAffiliationId
    feed_types = {"Tournament", "Showcase"}   # skip clubs, clinics, leagues, training

    def event_url(self, eid):
        return f"{self.host}/site/register/register.aspx?EventID={eid}"

    def feed_ids(self):
        """Event IDs of upcoming tournaments/showcases from the site's /api/program list; [] if no feed or it fails."""
        if not (self.feed_site and self.feed_affiliations):
            return []
        params = {"affiliationIds": json.dumps(self.feed_affiliations, separators=(",", ":")),
                  "currentAffiliationId": self.feed_current or self.feed_affiliations[0]}
        try:
            r = self.session.get(f"{self.feed_site}/api/program", params=params, timeout=30,
                                 headers={"Accept": "application/json, text/plain, */*"})
            programs = r.json().get("programs", []) if r.status_code == 200 else []
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug} feed: {e}")
            return []
        today = datetime.date.today().isoformat()
        keep = [p for p in programs
                if p.get("typeName") in self.feed_types and (p.get("end") or p.get("start") or "")[:10] >= today and p.get("id")]
        self.feed_programs = {p["id"]: p for p in keep}
        ids = list(self.feed_programs)
        if not ids:
            self.errors.append(f"{self.slug} feed returned no upcoming events (status {r.status_code})")
        return ids

    def collect(self):
        ids = list(dict.fromkeys(self.feed_ids() + list(self.known_ids)))
        if self.scan_range:
            seen = set(ids)
            ids += [i for i in range(*self.scan_range) if i not in seen]
        out = []
        for eid in ids:
            try:
                ev = self.parse_event(eid)
            except FetchError:
                ev = self._event_from_feed(eid)
            except Exception as e:  # noqa: BLE001
                ev = self._event_from_feed(eid)          # the page didn't parse, but the feed still knows the event
                if not ev:
                    self.errors.append(f"{self.slug}#{eid}: {e}")
                    continue
            if ev is None:
                ev = self._event_from_feed(eid)
            if ev:
                self._fill_from_feed(ev, eid)
                out.append(ev)
        return out

    def _event_from_feed(self, eid):
        """An Event from the feed alone (name, dates, venue, city/state) when the event page can't be read."""
        p = getattr(self, "feed_programs", {}).get(eid)
        if not p:
            return None
        def d(x):
            try: return datetime.date.fromisoformat((x or "")[:10])
            except ValueError: return None
        name = re.sub(r"^20\d{2}\s+", "", p.get("listName") or p.get("name") or "").strip()
        if not name or not d(p.get("start")):
            return None
        ev = Event(organizer=self.organizer, name=name, start_date=d(p.get("start")), end_date=d(p.get("end")) or d(p.get("start")),
                   raw_date=p.get("dateString") or "", source_url=self.event_url(eid),
                   event_type="showcase" if p.get("typeName") == "Showcase" else "tournament")
        self._fill_from_feed(ev, eid)
        return ev

    def _fill_from_feed(self, ev, eid):
        """The feed knows city, state, venue and boys/girls even when the event page's header doesn't say."""
        p = getattr(self, "feed_programs", {}).get(eid)
        if not p:
            return
        if not ev.venue and p.get("locationName") and p["locationName"].lower() != "multiple locations":
            ev.venue = p["locationName"]
        if not ev.city and p.get("city"):
            ev.city = p["city"]
        if not ev.state and p.get("state"):
            ev.state = US_STATE.get(p["state"].lower(), p["state"] if len(p["state"]) == 2 else None)
            if ev.state:
                ev.__post_init__()                 # recompute region/fingerprint with the state
        if ev.gender in (None, "", "unknown") and p.get("genders"):
            g = set(p["genders"])
            ev.gender = "coed" if g == {True, False} else "boys" if g == {True} else "girls" if g == {False} else ev.gender

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
            if re.search(r"\bregistration\b", l, re.I):       # '2026 Fall Ball Registration' heading (Aloha): no location line
                break
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
        # "97 college coaches attended" is how many recruiters came, not how many teams played
        cc = re.search(r"(\d{2,3})\s+college coaches", body, re.I)
        coaches = int(cc.group(1)) if cc else None

        return Event(
            organizer=self.organizer, name=name,
            start_date=start, end_date=end, raw_date=raw_date,
            venue=venue, city=city, state=state,
            gender=(self.default_gender if self.default_gender != "unknown"
                    else normalize_gender(name + " " + body[:800])),
            age_range=divisions, college_coaches=coaches,
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
    default_gender = "unknown"          # the feed brings girls events too; read gender from each event
    feed_site = "https://www.nxtsports.com"
    feed_affiliations = [127, 284]
    feed_current = 284
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
    feed_site = "https://www.alohatournaments.com"
    feed_affiliations = [121]
    known_ids = [18264, 18287, 18285, 20759]


class AllianceCollector(LeagueAppsCollector):
    """Alliance Lacrosse League ("The League"), on the 3Step Sports platform like NXT.

    The register index and /page/events now load programs with JavaScript (and /page/events
    was down on 2026-10-08), so neither lists events to a fetcher. Two sources that do:
      - EventID links on the home page -> parsed with the normal LeagueApps event parser
      - the league's page on its video partner, nlvproductions.com, which lists every event
        (split into Saturday/Sunday/Both Days packages; merged back into one event here)."""
    slug = "alliance"
    organizer = "Alliance"
    host = "https://www.thealliancelacrosseleague.com"
    nlv_url = "https://nlvproductions.com/events/host/alliance-lacrosse-league"
    tier = 2
    default_gender = "boys"
    known_ids: list[int] = []

    def collect(self):
        out = []
        try:
            home = self.soup(f"{self.host}/page/home")
            ids = sorted({int(x) for x in re.findall(r"EventID=(\d+)", str(home))} | set(self.known_ids))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug} home: {e}"); ids = list(self.known_ids)
        for eid in ids:
            try:
                ev = self.parse_event(eid)
                if ev: out.append(ev)
            except FetchError:
                continue
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug}#{eid}: {e}")
        try:
            out += self.collect_nlv(out)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug} nlv: {e}")
        return out

    def collect_nlv(self, have):
        """One table row per package: 'b' = name, then date and location cells. Upcoming events only."""
        import datetime
        merged = {}
        for tr in self.soup(self.nlv_url).select("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4 or not tds[1].find("b"): continue
            title = self.txt(tds[1].find("b")).strip()
            start, end = parse_date_range(self.txt(tds[2]).strip())
            if not start: continue
            end = end or start
            if end < datetime.date.today(): continue
            name = re.sub(r"\s*-?\s*\b(Saturday Only|Sunday Only|Both Days)$", "", title, flags=re.I)
            name = re.sub(r"^20\d{2}\s+", "", name).strip(" -")
            d = merged.setdefault((start.year, name.lower()), {"name": name, "start": start, "end": end,
                                                              "raw": self.txt(tds[2]).strip(), "where": self.txt(tds[3]).strip()})
            d["start"] = min(d["start"], start); d["end"] = max(d["end"], end)
        seen = {(e.name.lower(), e.start_date) for e in have}
        out = []
        for d in merged.values():
            if (d["name"].lower(), d["start"]) in seen: continue
            states = re.findall(r"\b([A-Z]{2})\b", d["where"])
            out.append(Event(
                organizer=self.organizer, name=d["name"], start_date=d["start"], end_date=d["end"], raw_date=d["raw"],
                venue=d["where"] if len(states) != 1 else None, city=None, state=states[0] if len(states) == 1 else None,
                gender="boys", event_type="tournament", source_url=self.nlv_url,
            ))
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
