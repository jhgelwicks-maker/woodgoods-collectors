"""Offline tests for collectors/organizers/find.py (no network). Live accuracy is measured separately with
tools/eval_find_organizer.py against tests/fixtures/find_organizer_truth.json.
Run: python -m pytest tests/test_find_organizer.py -q"""
import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.organizers import find as F

MD_FALL_BRAWL = {"name": "Fall Brawl 2025", "start": "2025-11-02", "state": "MD", "city_states": ["Crofton, MD"],
                 "venues": [{"venue_name": "Bell Branch Park", "address": "1141 Lake Superior Dr, Crofton, MD", "lat": "39.0396", "long": "-76.6640"}]}
OCTOBERFEST = {"name": "Octoberfest 2025", "start": "2025-10-25", "state": "NY", "city_states": ["Uniondale, NY"],
               "venues": [{"venue_name": "Mitchel Field Athletic Complex", "lat": "40.7277", "long": "-73.5865"}]}


class R:  # minimal stand-in for requests.Response
    def __init__(self, url, text): self.url, self.text, self.status_code = url, text, 200


def fake_fetch(pages):
    return lambda url: R(url, pages[url]) if url in pages else None


# ---------------------------------------------------------------------------------------- verify
def test_verify_accepts_organizer_page_naming_event_and_venue():
    pages = {"https://tristarlacrosse.com/octoberfest/":
             "<h1>Octoberfest</h1><p>Saturday, October 31, 2026. Mitchel Field Athletic Complex, Uniondale, NY. Open to all boys teams.</p>"}
    v = F.verify({"website": "https://tristarlacrosse.com/", "evidence_url": "https://tristarlacrosse.com/octoberfest/"}, OCTOBERFEST, fake_fetch(pages))
    assert v["verified"] and v["on_org_site"]


def test_verify_rejects_same_name_event_in_another_state():
    pages = {"https://bukulax.com/fall-brawl": "<h1>Santa Barbara Fall Brawl</h1><p>SB Polo Club, Carpinteria, California. Nov 2026.</p>"}
    v = F.verify({"website": "https://bukulax.com/", "evidence_url": "https://bukulax.com/fall-brawl"}, MD_FALL_BRAWL, fake_fetch(pages))
    assert not v["verified"]


def test_verify_rejects_homepage_that_never_names_the_event():
    pages = {"https://somelaxclub.com/": "<h1>Some Lax Club</h1><p>Spring travel tryouts in Crofton, Maryland.</p>"}
    v = F.verify({"website": "https://somelaxclub.com/", "evidence_url": None}, MD_FALL_BRAWL, fake_fetch(pages))
    assert not v["verified"]


def test_verify_handles_unreachable_site():
    v = F.verify({"website": "https://gone.example/", "evidence_url": None}, MD_FALL_BRAWL, fake_fetch({}))
    assert v["verified"] is False and v["page"] is None


# --------------------------------------------------------------------------------------- helpers
def test_query_cleanup_drops_dates_grades_and_years():
    assert F._clean_query("LAX Mayhem - May 16th 2026 (Grade 4-6)") == "LAX Mayhem"
    assert F._clean_query("2025 Chris Barnes Seaway Classic") == "Chris Barnes Seaway Classic"
    assert "mayhem" in F._queries("LAX Mayhem - May 16th 2026 (Grade 4-6)")


def test_year_shift_allows_one_to_three_years_same_weekday():
    d = dt.date
    assert F._year_shift(d(2025, 10, 4), d(2026, 10, 3)) == 364
    assert F._year_shift(d(2024, 5, 17), d(2027, 5, 14)) == 1092
    assert F._year_shift(d(2025, 10, 4), d(2026, 3, 1)) is None


def test_platforms_are_not_organizer_sites():
    assert F.is_platform("https://www.tourneymachine.com/Public/x")
    assert F.is_platform("facebook.com")
    assert F.is_platform("https://patch.com/georgia/roswell")   # news sites can be evidence, never the organizer site
    assert not F.is_platform("https://thinklax.leagueapps.com/")      # a club's own registration subdomain is allowed
    assert not F.is_platform("https://tristarlacrosse.com/")


# ------------------------------------------------------------------------ stored-listing matching
POOL = [
    {"name": "2026 Fall Brawl", "start": "2026-10-04", "state": "NY", "organizer": "Canandaigua Lacrosse", "pts": [[42.8867, -77.2814]], "url": "u1"},
    {"name": "Santa Barbara Fall Brawl", "start": "2026-10-24", "state": "CA", "organizer": "Buku", "pts": [[34.43, -119.6]], "url": "u2"},
    {"name": "Fall Brawl – Site 2", "start": "2026-11-08", "state": "MD", "organizer": "Hogan's", "pts": [[39.0396, -76.6640]], "url": "u3"},
    {"name": "Turkey Shootout", "start": "2026-11-01", "state": "MD", "organizer": "Somebody Else", "pts": [[39.0396, -76.6640]], "url": "u4"},
]


def test_same_name_in_other_states_never_matches_and_right_one_is_found():
    got = F.listing_candidates(MD_FALL_BRAWL, POOL)
    assert [c["name"] for c in got] == ["Hogan's"]


def test_find_organizer_from_stored_listing():
    r = F.find_organizer(MD_FALL_BRAWL, stages=("stored",), pool=POOL)
    assert r["status"] == "found_verified" and r["organizer_name"] == "Hogan's" and r["method"] == "stored_listing"


def test_unrelated_event_at_same_venue_same_weekend_is_not_the_organizer():
    pool = [p for p in POOL if p["url"] == "u4"]
    r = F.find_organizer(MD_FALL_BRAWL, stages=("stored",), pool=pool)
    assert r["status"] == "not_found"
