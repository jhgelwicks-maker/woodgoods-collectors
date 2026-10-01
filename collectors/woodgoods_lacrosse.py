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
import re, datetime, logging
from . import common
from .common import log

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

    ident = _slug(f"{ev.name}-{ev.start_date}-{ev.venue or ev.city or ''}")

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
                "raw_date": ev.raw_date, "collector": collector_slug},
    }


def collect(only=None):
    rows, errors = [], 0
    for cls in _load():
        if only and cls.slug not in only:
            continue
        c = cls()
        evs = c.run()
        errors += len(c.errors)
        for ev in evs:
            rows.append(to_common(ev, c.slug))
        log.info("wg-%s: %d rows", c.slug, len(evs))
    return rows, errors
