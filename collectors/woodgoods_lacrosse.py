"""Lacrosse organizer-site collectors, emitting the shared common.py schema.

Wraps the 14 per-organizer scrapers (NXT, MLT, APEX, Trilogy, Victory,
PrimeTime, Adrenaline, Aloha, Alliance, Buku, Hogan's, ML8, NLF, PLL) and
maps their output onto the same row shape every other collector uses.

These sources complement Tourney Machine rather than duplicate it:
  - they publish VENUE names, which Tourney Machine does not
  - they publish PER-SITE detail for multi-site events
  - they cover organizers that never list on Tourney Machine at all

Both land on the same `event_site_key`, so the ingest layer reconciles them
into one row per booking instead of two rows side by side.
"""
import re, datetime, logging, dataclasses
from . import common
from .common import log

FACILITY = re.compile(r"\b(park|parks|complex|fields?|center|centre|\w*plex|stadium|university|college|academy|school|hs|club|"
                      r"grounds|arena|farm|sports|athletic|athletics|recreation|turf|dome|campus|commons|kp|fieldhouse)\b", re.I)
SITE_SUFFIX = re.compile(r"\s+–\s+Site \d+$")


def split_venues(venue):
    """'Farmingdale State College & Destination KP' -> two venues. Pieces that are not a venue on their own are joined
    back: 'SUSA Orlin & Cohen Sports Complex', 'Iron Peak Sports & Events' and 'North Myrtle Beach Park and Sports
    Complex' are one venue each, 'DE and PA' is not a venue list. 'Multiple Locations' / TBD cannot be split (the organizer doesn't say where)."""
    if not venue or re.search(r"multiple locations|\btbd\b|\btbc\b", venue, re.I):
        return [venue] if venue else []
    pieces = [p.strip(" ,") for p in re.split(r"\s+(?:&|and|\+)\s+|\s*/\s*", venue) if p.strip(" ,")]
    out = []
    for p in pieces:
        generic = all(FACILITY.fullmatch(w) or w.lower() in ("and", "of", "the", "community", "county", "regional")
                      for w in p.split())                          # 'Sports Complex', 'Community College'
        if out and (not FACILITY.search(out[-1]) or len(p.split()) == 1 or generic):
            out[-1] = f"{out[-1]} & {p}"
        else:
            out.append(p)
    return out if len(out) > 1 else [venue]


def expand_sites(ev):
    """One Event per venue: 'Lax Fest – Site 1' at Farmingdale State College, 'Lax Fest – Site 2' at Destination KP,
    each with its own venue and town (the same '– Site N' naming as the app's manual sheet)."""
    if len(ev.sites) > 1:
        # a listed site can itself name two venues in one town ('Brown University / Moses Brown School Providence, RI',
        # 'Blandair Park/Oakland Mills High School 5750 Oakland Mills Road'): each piece keeps the address given for both
        sites = []
        for site in ev.sites:
            v, c, st = site[:3]
            addr = site[3] if len(site) > 3 else None
            names = split_venues(v) or [v]
            sites += [(vv, c, st or ev.state, addr) for vv in names]
    else:
        names = split_venues(ev.venue)
        if len(names) < 2:
            return [ev]
        sites = []
        for n in names:
            v, st = common_split(n)
            sites.append((v, ev.city, st or ev.state, None))
    return [dataclasses.replace(ev, name=f"{ev.name} – Site {k}", venue=v, city=c, state=st, address=a, sites=[])
            for k, (v, c, st, a) in enumerate(sites, 1)]


def common_split(text):
    from core.model import split_city_state
    return split_city_state(text) if "," in (text or "") else (text, None)

# The organizer scrapers live in the `collectors` package shipped alongside
# this one. Import lazily so this module can be inspected without them.
def _load():
    from adapters.static_sites import (
        MLTCollector, ApexCollector, BukuCollector, PLLPlayCollector)
    from adapters.leagueapps import (
        NXTCollector, AlohaCollector, AllianceCollector)
    from adapters.misc_sites import (
        TrilogyCollector, VictoryCollector, HoganCollector, ML8Collector,
        PrimeTimeCollector, AdrenalineCollector, NLFCollector)
    return [MLTCollector, ApexCollector, BukuCollector, PLLPlayCollector,
            NXTCollector, VictoryCollector, HoganCollector, ML8Collector,
            TrilogyCollector, PrimeTimeCollector, AdrenalineCollector,
            NLFCollector, AlohaCollector, AllianceCollector]


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def to_common(ev, collector_slug):
    """Map one scraped Event onto the shared row schema."""
    city_state = None
    if ev.city and ev.state:
        city_state = f"{ev.city}, {ev.state}"
    elif ev.state:
        city_state = f"{ev.venue or ''}, {ev.state}".strip(", ")

    # name without '– Site N' + venue: a site keeps its id when the organizer reorders its locations
    ident = _slug(f"{SITE_SUFFIX.sub('', ev.name)}-{ev.start_date}-{ev.venue or ev.city or ''}")

    return {
        "source": f"wg-{collector_slug}",
        "source_event_id": ident,
        "name": ev.name,
        "sport": ev.sport or "lacrosse",
        "organizer_name": ev.organizer,
        "start_date": ev.start_date,
        "end_date": ev.end_date,
        "venue_name": ev.venue,
        "city": ev.city,
        "state": ev.state,
        "city_states": [city_state] if city_state else [],
        # organizer sites publish counts for the SITE, not the whole event
        "team_count": ev.team_count,
        "team_count_scope": "site",
        "age_groups": ev.age_range,
        "listing_url": ev.source_url,
        "official_url": ev.source_url,
        "raw": {"gender": ev.gender, "event_type": ev.event_type,
                "raw_date": ev.raw_date, "collector": collector_slug,
                "college_coaches": ev.college_coaches, "date_note": ev.date_note,
                # street address: the app's venue geocoder reads raw.address (Census) before falling back to the name
                "address": ev.address},
    }


def collect(only=None):
    rows, errors = [], 0
    for cls in _load():
        if only and cls.slug not in only:
            continue
        c = cls()
        evs = c.run()
        errors += len(c.errors)
        n = len(rows)
        for ev in evs:
            for site in expand_sites(ev):        # a two-venue event is two rows, one per site
                rows.append(to_common(site, c.slug))
        log.info("wg-%s: %d events, %d rows", c.slug, len(evs), len(rows) - n)
    return rows, errors
