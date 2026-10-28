"""OT Lacrosse, Exposure Events (lacrosse) and Legends Lacrosse."""
from __future__ import annotations
import re, json, base64, calendar, datetime
from core.base import BaseCollector
from core.model import Event, parse_date_range, split_city_state, normalize_gender, ABBR
from adapters.misc_sites import place_from_text, addresses_in
from adapters import details

SHOWCASE = re.compile(r"showcase|spotlight|prospect day|exposure", re.I)


def title_case(name):
    """'The sUMMER aUSTIN aUTHENTIC' (typed with caps lock on, shown in capitals by the site) -> 'The Summer Austin
    Authentic'; 'THE DISTRICT INDOOR' -> 'The District Indoor'."""
    return " ".join(w[:1].upper() + w[1:].lower() if w.isalpha() else w for w in name.split())


def month_span(text):
    """'MAR - MAY, 2027' (a spring league) -> (Mar 1, May 31), else None."""
    m = re.fullmatch(r"\s*([A-Za-z]{3,9})\s*[-–]\s*([A-Za-z]{3,9}),?\s*(20\d\d)\s*", text or "")
    if not m: return None
    a, b = ABBR.get(m.group(1).lower()[:4]) or ABBR.get(m.group(1).lower()[:3]), ABBR.get(m.group(2).lower()[:4]) or ABBR.get(m.group(2).lower()[:3])
    if not (a and b): return None
    y = int(m.group(3))
    return datetime.date(y, a, 1), datetime.date(y, b, calendar.monthrange(y, b)[1])


def dates_with_year(text, today=None):
    """parse_date_range, with 'DEC 12-13' (no year printed) read as the next such weekend."""
    today = today or datetime.date.today()
    s, e = parse_date_range(text)
    if s or re.search(r"20\d\d", text or ""):
        return s, e, None
    s, e = parse_date_range(text, default_year=today.year)
    if s and (e or s) < today:
        s, e = parse_date_range(text, default_year=today.year + 1)
    return s, e, ("year not printed on the site; read as the next one" if s else None)


class OTLaxCollector(BaseCollector):
    """Overtime Lacrosse: a card per event on /events/ (name, dates, town, 'View Event'); the event page has the venue,
    street address, grad years and the vendor/sponsor inbox. Spring leagues give only months ('MAR - MAY, 2027') and a
    list of venues."""
    slug = "otlax"
    organizer = "OT Lacrosse"
    tier = 1
    URL = "https://otlax.com/events/"

    def collect(self):
        try:
            soup = self.soup(self.URL)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug}: {e}")
            return []
        out = []
        for a in soup.find_all("a", href=True):
            if not re.search(r"view event", a.get_text(" ", strip=True), re.I):
                continue
            box = a
            for _ in range(8):
                box = box.parent
                parts = [p for p in box.get_text("|", strip=True).split("|") if p.strip()]
                if len(parts) >= 4: break
            parts = [p.strip() for p in parts if not re.search(r"view event", p, re.I)]
            if len(parts) < 3:
                continue
            name, when, town = parts[0], parts[1], parts[2]
            span = month_span(when)
            if span:
                (start, end), note, kind = span, f"season runs {when.strip()}; exact dates not published", "league"
            else:
                start, end, note = dates_with_year(when)
                kind = "tournament"
            if not start:
                self.errors.append(f"{self.slug}: no date in {when!r} ({name})")
                continue
            city, state = split_city_state(town)
            ev = Event(organizer=self.organizer, name=title_case(name), start_date=start, end_date=end or start,
                       raw_date=when, city=title_case(city) if city and state else None, state=state,
                       source_url=a["href"], date_note=note, event_type=kind)
            self._page(ev)
            out.append(ev)
        return out

    def _page(self, ev):
        """Venue, street address and boys/girls from the event page ('Location: Farmington Sports Arena (FSA) 11
        Executive Drive, Farmington CT, 06032'); a league's 'Venues' list becomes its sites."""
        try:
            text = details.full_text(self.get(ev.source_url))
        except Exception:  # noqa: BLE001
            return
        loc = re.search(r"Location:\s*(.+?)\s*(?:Grad Years|Registration|Game Film)", text)
        if loc:
            venue, city, state = place_from_text(loc.group(1))
            if state:
                ev.venue, ev.city, ev.state = venue or None, city, state
            elif len(loc.group(1)) <= 60 and not re.search(r"\d", loc.group(1)):
                ev.venue = loc.group(1)                # 'Location: United Sports': a venue; the card gave the town
            found = addresses_in(loc.group(1))
            ev.address = found[0][1] if found else None
        sec = re.search(r"Venues\s+(.+?)\s+Hotels\b", text)
        if sec:
            sites = []
            for block in re.split(r"\s*Get Directions\s*", sec.group(1)):
                v, c, st = place_from_text(block)
                found = addresses_in(block)
                if st:
                    sites.append((v, c, st, found[0][1] if found else None))
            if len(sites) > 1:
                ev.sites = sites
            elif sites and not ev.state:
                ev.venue, ev.city, ev.state, ev.address = sites[0]
        # OT runs boys' events; 'New in 2026, Girls Divisions!' or 'Boys and Girls Divisions' makes one co-ed
        low = text.lower()
        ev.gender = "coed" if "girls" in low else "boys" if "boys" in low else ev.gender


class ExposureCollector(BaseCollector):
    """Exposure Events' lacrosse listings: the page loads its table from a POST to the same URL (with the token the page
    prints), which returns every event as JSON with venue, street address, age grades and the director's contact.
    Events of organizers we scrape from their own site (NXT, Aloha, Hogan's, ...) are left to those scrapers."""
    slug = "exposure"
    organizer = "Exposure Events"
    tier = 1
    URL = "https://lacrosse.exposureevents.com/youth-lacrosse-events"
    OURS = re.compile(r"\b(nxt|aloha|hogan\w*|apex|mlt|my lacrosse tournaments|trilogy|victory|prime\s*time|adrenaline|"
                      r"alliance|buku|ml8|national lacrosse federation|nlf|premier lacrosse league|pll|legends|overtime|otlax)\b", re.I)
    GENDER = {1: "boys", 2: "girls"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        # Cloudflare turns away the bot user agent on this site
        self.session.headers["User-Agent"] = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

    def collect(self):
        try:
            page = self.session.get(self.URL, timeout=30).text
            token = re.search(r"tokenValue:\s*'([^']+)'", page).group(1)
            start = re.search(r'StartDateString:\s*"([^"]+)"', page).group(1)
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug}: listing page: {e}")
            return []
        results, total = [], None
        for p in range(1, 21):
            try:
                r = self.session.post(self.URL, timeout=30, data={"Page": p, "sportType": 6, "StartDateString": start,
                                                                  "CertificationType": ""},
                                      headers={"X-Exposure-Token": token, "X-Requested-With": "XMLHttpRequest", "Referer": self.URL})
                j = r.json()
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug}: page {p}: {e}")
                break
            results += j.get("Results") or []
            total = j.get("Total")
            if not j.get("Results") or len(results) >= (total or 0):
                break
        return [ev for ev in map(self._event, results) if ev]

    def _event(self, r):
        org = (r.get("OrganizationName") or "").strip()
        if self.OURS.search(org) or self.OURS.search(r.get("Name") or ""):
            return None
        s, e = (datetime.date.fromisoformat((r.get(k) or "")[:10]) if r.get(k) else None for k in ("StartDate", "EndDate"))
        if not s:
            return None
        ages = next((r.get(k) for k in ("YouthAgeGradesBoth", "YouthAgeGradesMale", "YouthAgeGradesFemale", "AdultAgeGrades",
                                         "CollegiateAgeGrades") if r.get(k)), None)
        gender = self.GENDER.get(r.get("Gender")) or (normalize_gender(ages) if ages else "unknown")
        city = (r.get("City") or "").strip()
        city = None if re.search(r"multiple", city, re.I) else city or None
        street = (r.get("StreetAddress") or "").strip()
        address = ", ".join(x for x in (street, city, " ".join(x for x in (r.get("StateRegionAbbr"), r.get("PostalCode")) if x)) if x) if street else None
        contact = {"name": (r.get("ContactName") or "").strip() or None, "email": (r.get("ContactEmail") or "").strip().lower() or None,
                   "phone": (r.get("ContactPhone") or "").strip() or None, "role": "director"}
        kind = (r.get("Type") or "").lower()
        return Event(organizer=org or self.organizer, name=re.sub(r"\s+", " ", r.get("Name") or "").strip(), start_date=s,
                     end_date=e or s, raw_date=r.get("DateFormatted") or "", venue=(r.get("Location") or "").strip() or None,
                     city=city, state=(r.get("StateRegionAbbr") or "").strip() or None, gender=gender,
                     age_range=details.grad_years(f"ages: {ages}", s) if ages else None,
                     address=address, source_url=r.get("Link"),
                     contacts=[contact] if contact["email"] or contact["name"] else [],
                     event_type="league" if "league" in kind else "showcase" if "showcase" in kind else "tournament")


class LegendsCollector(BaseCollector):
    """Legends Lacrosse: /events/ builds its grid from a list kept in the page, base64-encoded ({ n:"Elite 8", g:"Boys",
    dt:"Nov 7–8, 2026", v:"Saint Joseph High School · Metuchen, NJ", w:"<event page>" }); Lax Wars entries point at
    window.LGD_LAXWARS. Event pages give grad years and the event's named contact."""
    slug = "legends"
    organizer = "Legends"
    tier = 1
    URL = "https://legendslax.com/events/"

    @staticmethod
    def decoded(html):
        """The page with its base64 blocks appended as text."""
        extra = []
        for b in re.findall(r'atob\("([A-Za-z0-9+/=]+)"\)', html):
            try: extra.append(base64.b64decode(b).decode("utf-8", "replace"))
            except ValueError: pass
        return html + "\n".join(extra)

    @staticmethod
    def entries(html):
        """[{n, g, dt, v, w, c}] from the EVENTS list; LW.x values from window.LGD_LAXWARS."""
        lw = dict(re.findall(r'(\w+):\s*"([^"]*)"', (re.search(r"window\.LGD_LAXWARS\s*=\s*\{(.*?)\};", html, re.S) or [None, ""])[1]))
        block = (re.search(r"var EVENTS\s*=\s*\[(.*?)\];", html, re.S) or [None, ""])[1]
        out = []
        # one entry per line ('{ n:"Fall Exposure Showcase", ..., l:(window.LGD_LAXWARS || {}).fallExposureLogo || "" },')
        for line in (ln for ln in block.splitlines() if re.search(r"\{\s*n\s*:", ln)):
            item = {}
            for k, quoted, ref in re.findall(r'\b(n|g|dt|v|w|c)\s*:\s*(?:"([^"]*)"|LW\.(\w+)|\[[^\]]*\])', line):
                item[k] = quoted if quoted else lw.get(ref, "") if ref else ""
            item["c"] = re.search(r"\bc\s*:\s*(\[[^\]]*\]|\"[^\"]*\")", line).group(1) if re.search(r"\bc\s*:", line) else ""
            if item.get("n"):
                out.append(item)
        return out

    def collect(self):
        try:
            html = self.decoded(self.get(self.URL))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug}: {e}")
            return []
        out = []
        for it in self.entries(html):
            s, e, note = dates_with_year(it.get("dt", ""))
            if not s:
                self.errors.append(f"{self.slug}: no date in {it.get('dt')!r} ({it['n']})")
                continue
            venue, _, town = (it.get("v") or "").partition("·")
            city, state = split_city_state(town.strip() or venue.strip())
            venue = venue.strip() if town.strip() else None
            ev = Event(organizer=self.organizer, name=it["n"], start_date=s, end_date=e or s, raw_date=it.get("dt", ""),
                       venue=venue, city=city, state=state, gender=normalize_gender(it.get("g")), source_url=it.get("w"),
                       date_note=note, event_type="showcase" if SHOWCASE.search(it["n"]) else "tournament",
                       college_coaches=None)
            try:                                       # the page keeps some sections base64-encoded too
                text = details.full_text(self.decoded(self.get(ev.source_url)))
                ev.age_range = details.grad_years(text, s)
                ev.contacts = details.page_contacts(text, {"legendslax.com"})
            except Exception:  # noqa: BLE001
                pass
            out.append(ev)
        return out


class CarpetbaggerCollector(BaseCollector):
    """Carpetbagger Lacrosse (cblax.org). Each card on /events/ pairs the event's own page ('Details') with its LeagueApps
    tournament ('Register' -> /app/tournaments/<id>). LeagueApps' member-portal JSON gives the dates and every division
    ('2027 Girls', 'Varsity Boys 2027/2028'; 83 across the six events); the cblax.org page gives the venues with street
    addresses, split by boys and girls when they play apart ('BOYS DIVISIONS ... Mazeppa Park, Cornelius Road Park /
    GIRLS DIVISIONS ... Richard Barry Memorial Park'), and the organizers' contacts. LeagueApps lists one venue for all
    divisions, so the page's venues win."""
    slug = "cblax"
    organizer = "Carpetbagger"
    tier = 1
    URL = "https://cblax.org/events/"
    API = "https://api.leagueapps.io/api/member-portal/cblacrosse/tournaments/{}"
    FACILITY = r"(?:Park|Complex|School|Center|Centre|Fields|Sportsplex|Stadium)"

    def cards(self):
        """[(event page, LeagueApps tournament id)] in the order the site lists them."""
        soup = self.soup(self.URL)
        out, page = [], None
        for a in soup.find_all("a", href=True):
            if re.search(r"cblax\.org/events/[\w-]+/?$", a["href"]):
                page = a["href"]
            m = re.search(r"/app/tournaments/(\d+)", a["href"])
            if m and page:
                out.append((page, m.group(1)))
                page = None
        return out

    @classmethod
    def venues(cls, text):
        """{'boys'|'girls'|None: [(venue, city, state, address)]} from the page's 'Tournament Locations' section."""
        sec = re.search(r"Tournament Locations\s+(.+?)(?:Join Us|Escape the Summer Heat|Make It a|Finish the Year|Divisions\b)", text)
        # one-venue pages ('All games will be played at Mazeppa Park ...'): the longest 'Details ... Divisions' stretch,
        # not the page's tab menu ('Details Hotels Divisions')
        sec = sec.group(1) if sec else max(re.findall(r"\bDetails\s+(.*?)\bDivisions\b", text), key=len, default="")
        parts = re.split(r"\b(BOYS|GIRLS) DIVISIONS\b", sec)
        groups = [(None, parts[0])] + [(parts[i].lower(), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
        out = {}
        for gender, chunk in groups:
            found = []
            addrs = addresses_in(chunk)
            for pos, addr in addrs:                 # 'Mazeppa Park 645 Mazeppa Rd, Mooresville, NC 28115'
                lead = re.split(r"[.:!]\s+|\b(?:Bathrooms|bathrooms|restrooms|spectators|together)\b", chunk[:pos].strip())[-1]
                name = re.search(rf"((?:[A-Z][\w.&'-]*\s+){{0,4}}{cls.FACILITY})\s*$", lead.strip())
                city, state = split_city_state(re.sub(r"\s+\d{5}(?:-\d{4})?$", "", addr.split(",", 1)[1].strip()) if "," in addr else "")
                if name:
                    found.append((name.group(1).strip(), city, state, addr))
            if not addrs:                           # names only: 'Buncombe County Sports Park Premier tournament facility ...'
                for m in re.finditer(rf"((?:[A-Z][\w.&'-]*\s+){{0,4}}{cls.FACILITY})\s*,?\s+(?:Premier|Additional|features|featuring)", chunk):
                    found.append((m.group(1).strip(), None, None, None))
            names = set()
            uniq = [v for v in found if not (v[0] in names or names.add(v[0]))]
            if uniq:
                out[gender] = uniq
        return out

    def collect(self):
        try:
            cards = self.cards()
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.slug}: {e}")
            return []
        out = []
        for page, tid in cards:
            try:
                t = self.session.get(self.API.format(tid), timeout=30, headers={"Accept": "application/json"}).json()
            except Exception as e:  # noqa: BLE001
                self.errors.append(f"{self.slug}: tournament {tid}: {e}")
                continue
            start, end = (datetime.date.fromisoformat(t[k]) if t.get(k) else None for k in ("startDate", "endDate"))
            if not start:
                continue
            divisions = t.get("divisions") or []
            try:
                text = details.full_text(self.get(page))
            except Exception:  # noqa: BLE001
                text = ""
            contacts = details.page_contacts(text, {"cblax.org"})
            town = re.search(r"\b20\d\d\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?),\s*(North Carolina|South Carolina|Virginia|Georgia|Tennessee)\b", text)
            town = (town.group(1), {"North Carolina": "NC", "South Carolina": "SC", "Virginia": "VA", "Georgia": "GA", "Tennessee": "TN"}[town.group(2)]) if town else (None, None)
            by_gender = self.venues(text)
            base = re.sub(r"^20\d\d\s+", "", t.get("name") or "").strip()
            # boys and girls at different venues are two events; otherwise one event for all its divisions
            plan = ([(g, by_gender[g]) for g in ("boys", "girls")] if set(by_gender) >= {"boys", "girls"}
                    else [(None, by_gender.get(None) or next(iter(by_gender.values()), []))])
            for gender, sites in plan:
                divs = [d for d in divisions if not gender or d.get("gender") == {"boys": "male", "girls": "female"}[gender]]
                genders = {d.get("gender") for d in divs}
                g = gender or ("coed" if genders >= {"male", "female"} else "boys" if genders == {"male"} else "girls" if genders == {"female"} else "unknown")
                grads = sorted(int(y) for d in divs for y in re.findall(r"\b(20[2-4]\d)\b", d.get("name") or ""))
                sites = [(v, c or town[0], s or town[1], a) for v, c, s, a in sites] or [((t.get("location") or {}).get("name"), town[0], town[1], None)]
                ev = Event(organizer=self.organizer, name=f"{base} ({gender.title()})" if gender else base,
                           start_date=start, end_date=end or start, raw_date=f"{t.get('startDate')} to {t.get('endDate')}",
                           venue=sites[0][0], city=sites[0][1], state=sites[0][2], address=sites[0][3],
                           sites=sites if len(sites) > 1 else [], gender=g, source_url=page,
                           age_range=(f"{grads[0]}–{grads[-1]}" if grads[0] != grads[-1] else str(grads[0])) if grads else None,
                           divisions=[d.get("name") for d in divs], contacts=contacts,
                           event_type="tournament")
                out.append(ev)
        # a venue one page gives with its town or street address fills the same venue on pages that don't
        # ('Buncombe County Sports Park' on the Asheville Fall Classic page has no town; the summer pages place it)
        known = {}
        for ev in out:
            for v, c, st, a in ev.sites or [(ev.venue, ev.city, ev.state, ev.address)]:
                if v and c and (v not in known or (a and not known[v][2])):
                    known[v] = (c, st, a)
        for ev in out:
            if ev.venue in known and not ev.city:
                ev.city, ev.state = known[ev.venue][:2]
                ev.address = ev.address or known[ev.venue][2]
            ev.sites = [(v, c or known.get(v, (None,))[0], st or known.get(v, (None, None))[1], a or known.get(v, (None, None, None))[2])
                        for v, c, st, a in ev.sites]
        return out
