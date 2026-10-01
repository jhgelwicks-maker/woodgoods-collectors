"""Venue gazetteer.

Many organizers publish a venue name with no city/state ('United Sports
Training Center'). Region is the single most important field for routing
operators, and region is derived from state - so a missing state means the
row can't be routed.

This table backfills city/state from venue names already seen in the data.
It grows: every time a new venue appears with a state attached, add it here
and every future row naming that venue gets routed correctly.
"""
from __future__ import annotations
import re

VENUES = {
    # Pennsylvania
    "united sports training center":   ("Downingtown", "PA"),
    "united sports":                   ("Downingtown", "PA"),
    "proving grounds":                 ("Conshohocken", "PA"),
    "the proving grounds":             ("Conshohocken", "PA"),
    "maplezone sports institute":      ("Aston", "PA"),
    "west chester east hs":            ("West Chester", "PA"),
    "xl sports world hatfield":        ("Hatfield", "PA"),
    "in the net":                      ("Hershey", "PA"),
    # New Jersey
    "capelli sport complex":           ("Tinton Falls", "NJ"),
    "capelli sports complex":          ("Tinton Falls", "NJ"),
    "iron peak sports & events":       ("Hillsborough", "NJ"),
    "drum point sports complex":       ("Brick", "NJ"),
    "mercer county park":              ("West Windsor", "NJ"),
    "rutgers university":              ("Piscataway", "NJ"),
    # New York
    "farmingdale state college":       ("Farmingdale", "NY"),
    "purchase college":                ("Purchase", "NY"),
    "stony brook university":          ("Stony Brook", "NY"),
    "blue sky sports complex":         ("Middletown", "NY"),
    "onatru farm park":                ("South Salem", "NY"),
    "liu post fields":                 ("Brookville", "NY"),
    "heckscher state park":            ("East Islip", "NY"),
    "mount sinai high school":         ("Mount Sinai", "NY"),
    # New England
    "oakwood sports center":           ("Glastonbury", "CT"),
    "farmington sports arena":         ("Farmington", "CT"),
    "yale university":                 ("New Haven", "CT"),
    "quinnipiac university":           ("Hamden", "CT"),
    "bryant university":               ("Smithfield", "RI"),
    "brown university":                ("Providence", "RI"),
    "university of rhode island":      ("Kingston", "RI"),
    "umass amherst":                   ("Amherst", "MA"),
    "forekicks":                       ("Taunton", "MA"),
    "stony brook south p lot":         ("Stony Brook", "NY"),
    "susa orlin & cohen sports complex": ("Bethpage", "NY"),
    "destination kp":                  ("Kings Park", "NY"),
    "laurel park":                     ("Laurel", "NY"),
    "long beach middle school":        ("Long Beach", "NY"),
    "mecklenberg county sportsplex":   ("Charlotte", "NC"),
    # Mid-Atlantic
    "de turf sports complex":          ("Frederica", "DE"),
    "de turf":                         ("Frederica", "DE"),
    "kirkwood soccer complex":         ("New Castle", "DE"),
    "kirkwood sports complex":         ("New Castle", "DE"),
    "chase fieldhouse":                ("Wilmington", "DE"),
    "sandhill fields":                 ("Georgetown", "DE"),
    "calvert regional park":           ("North East", "MD"),
    "cedar lane regional park":        ("Bel Air", "MD"),
    "cedar lane park":                 ("Bel Air", "MD"),
    "copperplex":                      ("Edgewood", "MD"),
    "coppermine copperplex":           ("Edgewood", "MD"),
    "blandair regional park":          ("Columbia", "MD"),
    "blandair park":                   ("Columbia", "MD"),
    "troy park":                       ("Elkridge", "MD"),
    "maryland soccerplex":             ("Boyds", "MD"),
    "maryland state fairgrounds":      ("Timonium", "MD"),
    "south river high school":         ("Edgewater", "MD"),
    "annapolis high school":           ("Annapolis", "MD"),
    "bell branch park":                ("Gambrills", "MD"),
    "dulles sportsplex":               ("Sterling", "VA"),
    "virginia beach fieldhouse":       ("Virginia Beach", "VA"),
    "rivercity sports complex":        ("Midlothian", "VA"),
    "river city sports complex":       ("Midlothian", "VA"),
    "sports center of richmond":       ("Richmond", "VA"),
    # Midwest / South / West
    "grand park sports campus":        ("Westfield", "IN"),
    "grand park":                      ("Westfield", "IN"),
    "warren county sports park":       ("Lebanon", "OH"),
    "stsa athletic complex":           ("Saginaw", "MI"),
    "wintrust crossroads sports complex": ("New Lenox", "IL"),
    "richard barry park":              ("Huntersville", "NC"),
    "mazeppa park":                    ("Mooresville", "NC"),
    "matthews sportsplex":             ("Matthews", "NC"),
    "zoo city sportsplex":             ("Asheboro", "NC"),
    "netsports complex":               ("Cary", "NC"),
    "truist sports park":              ("Charlotte", "NC"),
    "bryan park":                      ("Greensboro", "NC"),
    "championsgate sports complex":    ("Davenport", "FL"),
    "paradise coast sports complex":   ("Naples", "FL"),
    "rocky top sports world":          ("Gatlinburg", "TN"),
    "hoover metropolitan complex":     ("Hoover", "AL"),
    "riverside park":                  ("Woodstock", "GA"),
    "hubert park":                     ("Acworth", "GA"),
    "mudd creek park":                 ("Marietta", "GA"),
    "socal sports complex":            ("Oceanside", "CA"),
    "silverlakes equestrian & sports park": ("Norco", "CA"),
    "galway downs":                    ("Temecula", "CA"),
    "regional athletic complex":       ("Salt Lake City", "UT"),
    "western sports park":             ("Salt Lake City", "UT"),
    "farmington regional park":        ("Farmington", "UT"),
    "long lake regional park":         ("Denver", "CO"),
    "aurora sports park":              ("Aurora", "CO"),
    "university of denver":            ("Denver", "CO"),
    "copper sky regional park":        ("Maricopa", "AZ"),
    "beacon park":                     ("Melissa", "TX"),
    "carpenter park":                  ("Plano", "TX"),
    "railroad park":                   ("Lewisville", "TX"),
    "harold patterson sports center":  ("Arlington", "TX"),
}

_NOISE = re.compile(r"^(droplight|title|display|image)\s+", re.I)


def clean_venue(name):
    """Strip CMS artifacts that leak into scraped venue strings."""
    if not name:
        return None
    v = _NOISE.sub("", str(name)).strip(" ,-\u2013")
    return v or None


def lookup(venue):
    """venue name -> (city, state) or (None, None)."""
    if not venue:
        return None, None
    v = clean_venue(venue).lower()
    v = re.sub(r"\s+", " ", v).strip(" .,-")
    if v in VENUES:
        return VENUES[v]
    for key, val in VENUES.items():          # substring fallback
        if key in v or v in key:
            return val
    return None, None


def enrich(event):
    """Fill city/state/region on an Event from its venue name, in place."""
    event.venue = clean_venue(event.venue)
    if event.state:
        return event
    city, state = lookup(event.venue)
    if state:
        event.city = event.city or city
        event.state = state
        from core.model import region_for
        event.region = region_for(state)
    return event
