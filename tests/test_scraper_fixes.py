"""Offline checks for this week's organizer-scraper fixes (adapters/). No network.

NXT / Aloha: event IDs come from the site's own /api/program list, so events added after the hand-typed IDs are not
missed; an event page that won't parse is built from the feed. Adrenaline: the year is worked out from today instead
of a fixed 2026/27 table, event pages are matched to index cards by shared words, and the cards behind 'Load More'
are fetched. APEX / MLT / Victory / Buku read every listing page (girls, showcases, regions), not just one."""
import datetime as dt

from bs4 import BeautifulSoup

from adapters.leagueapps import LeagueAppsCollector, NXTCollector
from adapters.misc_sites import AdrenalineCollector, VictoryCollector, TrilogyCollector, PrimeTimeCollector, ML8Collector, HoganCollector, place_from_text
from adapters.misc_sites import venue_from_page, merge_page_place, best_link
from adapters.static_sites import ApexCollector, MLTCollector, BukuCollector


def fake_pages(c, monkeypatch, pages):
    """Serve canned HTML by URL instead of fetching."""
    monkeypatch.setattr(c, "get", lambda url, **k: pages.get(url, "<html></html>"))
    monkeypatch.setattr(c, "soup", lambda url, **k: BeautifulSoup(pages.get(url, "<html></html>"), "lxml"))


class FakeResp:
    status_code = 200
    def __init__(self, data): self._d = data
    def json(self): return self._d


FUTURE = (dt.date.today() + dt.timedelta(days=30)).isoformat()
PAST = (dt.date.today() - dt.timedelta(days=30)).isoformat()
FEED = {"programs": [
    {"id": 1, "typeName": "Tournament", "listName": "2026 Fall Grand Prix", "start": f"{FUTURE}T04:00:00", "end": f"{FUTURE}T04:00:00",
     "city": "Westfield", "state": "Indiana", "locationName": "Droplight Grand Park Sports Campus", "genders": [True], "dateString": "x"},
    {"id": 2, "typeName": "Showcase", "listName": "Girls Winter Showcase", "start": f"{FUTURE}T05:00:00", "end": f"{FUTURE}T05:00:00",
     "locationName": "Multiple Locations", "genders": [False]},
    {"id": 3, "typeName": "Club Team", "listName": "NXT Boys Club", "start": f"{FUTURE}T05:00:00", "end": f"{FUTURE}T05:00:00"},
    {"id": 4, "typeName": "Tournament", "listName": "Old Event", "start": f"{PAST}T05:00:00", "end": f"{PAST}T05:00:00"},
]}


class FeedSite(LeagueAppsCollector):
    slug, organizer, host = "testsite", "Test", "https://register.test"
    feed_site, feed_affiliations, known_ids = "https://www.test", [7], [99]


def test_feed_ids_keep_upcoming_tournaments_and_showcases_only(monkeypatch):
    c = FeedSite(use_cache=False)
    monkeypatch.setattr(c.session, "get", lambda url, **k: FakeResp(FEED))
    assert c.feed_ids() == [1, 2]                                   # club team and past event left out


def test_unparseable_event_pages_are_built_from_the_feed(monkeypatch):
    c = FeedSite(use_cache=False)
    monkeypatch.setattr(c.session, "get", lambda url, **k: FakeResp(FEED))
    def broken(eid): raise IndexError("list index out of range")
    monkeypatch.setattr(c, "parse_event", broken)
    evs = {e.name: e for e in c.collect()}
    assert set(evs) == {"Fall Grand Prix", "Girls Winter Showcase"} and c.errors == ["testsite#99: list index out of range"]
    gp = evs["Fall Grand Prix"]
    assert (gp.city, gp.state, gp.venue, gp.gender) == ("Westfield", "IN", "Droplight Grand Park Sports Campus", "boys")
    assert evs["Girls Winter Showcase"].venue is None and evs["Girls Winter Showcase"].gender == "girls"


def test_feed_failure_falls_back_to_known_ids(monkeypatch):
    c = FeedSite(use_cache=False)
    def down(url, **k): raise ConnectionError("feed down")
    monkeypatch.setattr(c.session, "get", down)
    assert c.feed_ids() == [] and "feed down" in c.errors[0]


def test_nxt_reads_gender_per_event():
    assert NXTCollector.default_gender == "unknown" and NXTCollector.feed_affiliations == [127, 284]


def test_adrenaline_year_follows_today_not_a_fixed_season():
    c = AdrenalineCollector(use_cache=False)
    today = dt.date.today()
    nxt = today + dt.timedelta(days=20)
    assert c.season_year(nxt.strftime("%b"), nxt.day) == nxt.year
    old = today - dt.timedelta(days=90)
    assert c.season_year(old.strftime("%b"), old.day) == old.year + 1   # months long past -> next season


def test_adrenaline_matches_event_pages_by_shared_words():
    c = AdrenalineCollector(use_cache=False)
    pages = {"thefinalsayfall": "https://www.adrln.com/events/the-final-say-fall/",
             "goldcup": "https://www.adrln.com/events/gold-cup/"}
    assert c.page_by_words("Final Say: Fall High School Lacrosse", pages) == "https://www.adrln.com/events/the-final-say-fall/"
    assert c.page_by_words("Gold Rush Invitational", pages) is None              # one word in common is not enough


def test_adrenaline_reads_cards_behind_load_more(monkeypatch):
    c = AdrenalineCollector(use_cache=False)
    first = '<div class="event-item" data-post-id="1"></div><button class="load-more-btn" data-page="1" data-max-pages="5">'
    pages = {1: '<div class="event-item" data-post-id="2">Nov</div>', 2: ""}
    sent = []
    class R:
        status_code = 200
        def __init__(self, t): self.text = t
    def post(url, data, **k):
        sent.append(data["page"]); return R(pages[data["page"]])
    monkeypatch.setattr(c.session, "post", post)
    monkeypatch.setattr("adapters.misc_sites.time.sleep", lambda s: None)
    assert 'data-post-id="2"' in c.more_cards(first) and sent == [1, 2]   # stops at the first empty page
    assert c.more_cards("<div>no button</div>") == ""


def test_apex_reads_girls_pages_stops_at_past_events_and_drops_streets(monkeypatch):
    c = ApexCollector(use_cache=False)
    html = ("<h3>Girls Fall Prelim</h3><p>October 24 & 25, 2026</p><p>Conshohocken Rd Conshohocken, PA</p>"
            "<h2>Past Events</h2><h3>Girls Old Event</h3><p>June 1, 2026</p>")
    fake_pages(c, monkeypatch, {"https://apexlacrosseevents.com/girls-tournaments/": html})
    evs = c.collect()
    assert [(e.name, e.gender, e.city, e.state) for e in evs] == [("Girls Fall Prelim", "girls", "Conshohocken", "PA")]


def test_mlt_reads_every_listing_once(monkeypatch):
    c = MLTCollector(use_cache=False)
    card = lambda n, slug: (f'<div class="rb-event-card"><a href="https://mlt.test/event/{slug}/"></a><div class="rb-event-title">{n}</div>'
                            '<div class="rb-event-details"><div class="rb-detail-item">Nov 13, 2026</div>'
                            '<div class="rb-detail-item">Aurora Sports Park, CO</div><div class="rb-detail-item">Boys</div></div></div>')
    fake_pages(c, monkeypatch, {c.URL: card("Fall Brawl", "fall-brawl"),
                                c.URL + "showcase/": card("Rocky Mountain Showcase", "rms") + card("Fall Brawl", "fall-brawl")})
    assert [e.name for e in c.collect()] == ["Fall Brawl", "Rocky Mountain Showcase"]


def test_victory_reads_boys_girls_showcases_and_marks_shared_events_coed(monkeypatch):
    c = VictoryCollector(use_cache=False)
    row = lambda d, n, city: f"<p>{d}</p><p>{n}</p><p>{city}</p><p>View Event</p>"
    fake_pages(c, monkeypatch, {
        c.URL + "boys/": row("October 31 & November 1, 2026", "Cowboy Cup", "Plano, TX") + row("June 5-6, 2027", "Texas Draw", "Melissa, TX"),
        c.URL + "girls/": row("October 31 & November 1, 2026", "Cowboy Cup", "Plano, TX"),
        c.URL + "showcases/": row("Friday, June 18, 2027", "Beast of The East Showcase", "West Chester, PA")})
    evs = {e.name: e for e in c.collect()}
    assert evs["Cowboy Cup"].gender == "coed" and evs["Texas Draw"].gender == "boys"
    assert evs["Beast of The East Showcase"].start_date == dt.date(2027, 6, 18) and evs["Beast of The East Showcase"].event_type == "showcase"


def test_trilogy_reads_menu_links_ending_in_gender(monkeypatch):
    c = TrilogyCollector(use_cache=False)
    fake_pages(c, monkeypatch, {c.URL: '<a href="https://trilogylacrosse.com/events/future-aces-philly/">Future Aces Philly JUL 26 - 29, 2027 | PA | BOYS</a>'
                                       '<a href="https://trilogylacrosse.com/events/gwil/">Girls Winter Indoor League JAN 4 - FEB 22, 2027 | NJ</a>'})
    evs = {e.name: e for e in c.collect()}
    assert (evs["Future Aces Philly"].end_date, evs["Future Aces Philly"].gender) == (dt.date(2027, 7, 29), "boys")
    assert evs["Girls Winter Indoor League"].gender == "girls"


def test_buku_reads_the_showcases_page_too(monkeypatch):
    c = BukuCollector(use_cache=False)
    fake_pages(c, monkeypatch, {c.URL: "<p>June 12, 2027</p><p>Battle of the Bay</p><p>San Francisco, CA</p>",
                                "https://www.buku.events/showcases": "<p>November 6, 2026</p><p>Boys Vegas</p><p>Showcase</p><p>Las Vegas, NV</p>"})
    evs = {e.name: e for e in c.collect()}
    assert set(evs) == {"Battle of the Bay", "Boys Vegas Showcase"} and evs["Boys Vegas Showcase"].source_url.endswith("/showcases")


def test_primetime_reads_en_dash_ranges():
    assert PrimeTimeCollector.DATE_LINE.match("July 22nd \u2013 25th, 2027")


def test_ml8_reads_each_events_place_from_its_page_not_the_footer(monkeypatch):
    c = ML8Collector(use_cache=False)
    card = lambda slug, n, d: (f'<a class="card-container" href="/events/{slug}"><h3>{n}</h3><div>{d}</div></a>')
    fake_pages(c, monkeypatch, {
        c.URL: card("sweetlax", "SWEETLAX SUMMER CHALLENGE", "<div>June</div><div>19</div><div>&amp;</div><div>20</div><div>2027</div>")
               + card("backstage-pass", "Backstage Pass", "<div>June</div><div>21</div><div>2027</div>"),
        "https://www.ml8events.com/events/sweetlax": '<div class="event-date is-not-capitalized w-condition-invisible">Old Field - Miami, FL</div>'
            '<div class="event-date is-not-capitalized">DE Turf - Frederica, DE</div><footer>Miami, FL 33101</footer>',
        "https://www.ml8events.com/events/backstage-pass": '<div class="event-date is-not-capitalized">TBD, Maryland</div>'})
    evs = {e.name: e for e in c.collect()}
    sw = evs["Sweetlax Summer Challenge"]
    assert (sw.venue, sw.city, sw.state, sw.end_date) == ("DE Turf", "Frederica", "DE", dt.date(2027, 6, 20))
    assert sw.source_url == "https://www.ml8events.com/events/sweetlax"
    assert (evs["Backstage Pass"].city, evs["Backstage Pass"].state) == (None, "MD")


def test_place_from_text_reads_venue_and_town():
    assert place_from_text("Iron Peak Sports & Events Hillsborough, NJ NOTRE DAME High School Lawrenceville, NJ") == \
        ("Iron Peak Sports & Events", "Hillsborough", "NJ")
    assert place_from_text("Round Rock Multipurpose Complex 2001 N. Kenney Fort Blvd Round Rock, TX 78665") == \
        ("Round Rock Multipurpose Complex", "Round Rock", "TX")
    assert place_from_text("JUNE 12-13, 2027 Hershey, Pennsylvania All Teams")[1:] == ("Hershey", "PA")


def test_trilogy_reads_venue_and_town_from_the_event_page(monkeypatch):
    c = TrilogyCollector(use_cache=False)
    fake_pages(c, monkeypatch, {
        c.URL: '<a href="https://trilogylacrosse.com/events/east-coast-cup/">EAST COAST CUP JUN 19 - 20, 2027 | NJ</a>',
        "https://trilogylacrosse.com/events/east-coast-cup/": "<h2>Locations</h2><p>Mercer County Park/College West Windsor, NJ</p>"
                                                               "<p>2nd Location TBD</p><h2>Divisions</h2>"})
    e = c.collect()[0]
    assert (e.venue, e.city, e.state) == ("Mercer County Park/College", "West Windsor", "NJ")


def test_hogan_place_comes_from_the_location_block_not_page_words(monkeypatch):
    c = HoganCollector(use_cache=False)
    fake_pages(c, monkeypatch, {
        c.INDEX: '<a href="/page/fallbrawl">Fall Brawl</a>',
        "https://www.hoganlax.com/page/fallbrawl": "<h1>Fall Brawl</h1><p>Round Robin Format Register DETAILS Dates, Times, Location "
                                                   "November 8, 2026 Annapolis, MD First game 8:00</p>"})
    e = c.collect()[0]
    assert (e.venue, e.city, e.state) == (None, "Annapolis", "MD")


def test_mlt_reads_a_past_date_on_its_upcoming_list_as_next_year_with_a_note(monkeypatch):
    c = MLTCollector(use_cache=False)
    past = dt.date.today() - dt.timedelta(days=200)
    raw = f"{past:%B} {past.day}, {past.year}"
    card = (f'<div class="rb-event-card"><a href="https://mlt.test/event/box/"></a><div class="rb-event-title">Carolina Box Championship</div>'
            f'<div class="rb-event-details"><div class="rb-detail-item">{raw}</div><div class="rb-detail-item">NetSports Complex, Cary NC</div>'
            '<div class="rb-detail-item">Boys</div></div></div>')
    fake_pages(c, monkeypatch, {c.URL: card})
    e = c.collect()[0]
    assert e.start_date.year == past.year + 1 and (e.start_date.month, e.start_date.day) == (past.month, past.day)
    assert "mistyped" in e.date_note and raw in e.date_note
    assert MLTCollector._year_typo(dt.date.today(), None, "x")[2] is None                 # upcoming dates untouched
    old = dt.date.today() - dt.timedelta(days=500)
    assert MLTCollector._year_typo(old, old, "x") == (old, old, None)                      # still past a year on: left alone


def test_multi_venue_events_become_one_row_per_site(monkeypatch):
    from collectors.woodgoods_lacrosse import expand_sites, to_common, split_venues
    from core.model import Event
    ev = Event(organizer="MLT", name="Lax Fest", start_date=dt.date(2027, 6, 19), end_date=dt.date(2027, 6, 20),
               venue="Farmingdale State College & Destination KP", state="NY")
    rows = [to_common(x, "mlt") for x in expand_sites(ev)]
    assert [(r["name"], r["venue_name"]) for r in rows] == [("Lax Fest – Site 1", "Farmingdale State College"),
                                                            ("Lax Fest – Site 2", "Destination KP")]
    assert len({r["source_event_id"] for r in rows}) == 2 and "site" not in rows[0]["source_event_id"]   # stable ids
    assert split_venues("Iron Peak Sports & Events") == ["Iron Peak Sports & Events"]
    assert split_venues("SUSA Orlin & Cohen Sports Complex & Destination KP") == ["SUSA Orlin & Cohen Sports Complex", "Destination KP"]
    assert split_venues("DE and PA") == ["DE and PA"]
    assert split_venues("North Myrtle Beach Park and Sports Complex") == ["North Myrtle Beach Park and Sports Complex"]
    one = Event(organizer="MLT", name="Spring Jam", start_date=dt.date(2027, 5, 8), venue="Stony Brook South P Lot", state="NY")
    assert expand_sites(one) == [one]


def test_trilogy_keeps_every_location(monkeypatch):
    c = TrilogyCollector(use_cache=False)
    fake_pages(c, monkeypatch, {
        c.URL: '<a href="https://trilogylacrosse.com/events/radar/">RADAR INVITATIONAL JUN 19 - 20, 2027 | NJ</a>',
        "https://trilogylacrosse.com/events/radar/": "<h2>Location</h2><p>Iron Peak Sports & Events Hillsborough, NJ</p>"
                                                     "<p>NOTRE DAME High School Lawrenceville, NJ</p><h2>Divisions</h2>"})
    e = c.collect()[0]
    assert e.sites == [("Iron Peak Sports & Events", "Hillsborough", "NJ"), ("NOTRE DAME High School", "Lawrenceville", "NJ")]


def test_primetime_town_lists_become_sites():
    assert PrimeTimeCollector.towns("Northborough / Fitchburg, MA & Smithfield, RI") == \
        [(None, "Northborough", "MA"), (None, "Fitchburg", "MA"), (None, "Smithfield", "RI")]
    assert PrimeTimeCollector.towns("Amherst, Northborough, & Bellingham, MA") == \
        [(None, "Amherst", "MA"), (None, "Northborough", "MA"), (None, "Bellingham", "MA")]
    assert PrimeTimeCollector.towns("Smithfield, RI") == []


def test_apex_reads_the_location_box_even_after_many_class_badges(monkeypatch):
    c = ApexCollector(use_cache=False)
    badges = "".join(f'<span class="badge">{y}</span>' for y in (2030, 2029, 2028, 2027))
    html = ('<h3>Boys Apex Summer Invitational High School</h3><div class="dates"><img/><div><span class="event-date">July 11 & 12, 2027'
            f'</span><div class="event-classes"><span>Classes: {badges}</span></div></div></div>'
            '<div class="location"><img/><ul class="location-info"><li>University of Delaware</li><li>631 S College Ave</li>'
            '<li>Newark, DE 19716</li></ul></div>')
    fake_pages(c, monkeypatch, {"https://apexlacrosseevents.com/boys-tournaments/": html})
    e = c.collect()[0]
    assert (e.venue, e.city, e.state) == ("University of Delaware", "Newark", "DE")


def test_place_from_text_keeps_lowercase_venues_and_drops_greater_area():
    assert place_from_text("nCino Sports Parks 205 Sutton Steam Plant Rd Wilmington, NC  28401") == \
        ("nCino Sports Parks", "Wilmington", "NC")
    assert place_from_text("Greater Tinton Falls Area Tinton Falls, NJ") == (None, "Tinton Falls", "NJ")


def test_event_page_venues_and_the_safety_rule():
    assert venue_from_page("Texas Draw June 5-6, 2027 | Beacon Park, Melissa, TX| Boys Register") == ("Beacon Park", "Melissa", "TX")
    assert venue_from_page("Venue & Location Santa Barbara Polo Club 3300 Via Real, Carpinteria, CA 93013 Map linked here") == \
        ("Santa Barbara Polo Club", "Carpinteria", "CA")
    assert venue_from_page("Where: Farmingdale State College, 2350 NY-110, Farmingdale, N.Y. 11735, Golf Fields") == \
        ("Farmingdale State College", "Farmingdale", "NY")
    # the listing's town wins; a venue from another town (a party on the event page) is dropped; page text is not a venue
    assert merge_page_place("Frederica", "DE", ("The Treehouse Lounge", "Rehoboth Beach", "DE")) == (None, "Frederica", "DE")
    assert merge_page_place("Huntington Station", "NY", ("Breezy Park", "Rogues Path Huntington Station", "NY")) == \
        ("Breezy Park", "Huntington Station", "NY")
    assert merge_page_place("Melissa", "TX", ("Event Links Hotels Sponsors", "Melissa", "TX")) == (None, "Melissa", "TX")
    assert merge_page_place(None, None, ("IMG Academy", "Bradenton", "FL")) == ("IMG Academy", "Bradenton", "FL")
    assert best_link("Lake Tahoe Summit", ["https://pllplay.com/tournaments/lake-tahoe-summit-classic/",
                                           "https://pllplay.com/tournaments/jamtown/"]).endswith("lake-tahoe-summit-classic/")


def test_town_without_a_comma_and_venue_without_a_town(monkeypatch):
    assert place_from_text("Towson University Towson MD") == ("Towson University", "Towson", "MD")
    c = TrilogyCollector(use_cache=False)
    fake_pages(c, monkeypatch, {
        c.URL: '<a href="https://trilogylacrosse.com/events/fae/">Future Aces East JUL 12 - 15, 2027 | MD | BOYS</a>'
               '<a href="https://trilogylacrosse.com/events/ad/">Autumn Drop OCT 25, 2026 | NJ</a>',
        "https://trilogylacrosse.com/events/fae/": "<h3>Locations</h3><p>Towson University</p><p>TOWSON MD</p><h3>Divisions</h3>",
        "https://trilogylacrosse.com/events/ad/": "<h3>Location</h3><p>Iron Peak Sports & Events</p><p>2032, 2034, 2035/36</p><h3>Divisions</h3>"})
    evs = {e.name: e for e in c.collect()}
    assert (evs["Future Aces East"].venue, evs["Future Aces East"].city, evs["Future Aces East"].state) == ("Towson University", "Towson", "MD")
    assert (evs["Autumn Drop"].venue, evs["Autumn Drop"].city, evs["Autumn Drop"].state) == ("Iron Peak Sports & Events", None, "NJ")


def test_every_site_keeps_its_venue_even_in_the_same_town(monkeypatch):
    from collectors.woodgoods_lacrosse import expand_sites
    c = TrilogyCollector(use_cache=False)
    fake_pages(c, monkeypatch, {
        c.URL: '<a href="https://trilogylacrosse.com/events/oss/">Ocean State Showdown JUL 10 - 11, 2027 | RI</a>'
               '<a href="https://trilogylacrosse.com/events/sbt/">Spring Break Tournament MAR 14 - 15, 2027 | FL</a>',
        "https://trilogylacrosse.com/events/oss/": "<h3>Locations</h3><p>Brown University / Moses Brown School</p><p>Providence, RI</p>"
            "<p>East Providence HS / PCD Complex</p><p>Providence, RI</p><p>Bishop Hendricken High School</p><p>Warwick, RI</p>"
            "<p>JWU Harborside Campus</p><p>Cranston, RI</p><h3>Divisions</h3>",
        "https://trilogylacrosse.com/events/sbt/": "<h3>Locations</h3><p>Paradise Coast Sports Complex</p><p>Naples, FL</p><h3>Eligibility</h3>"})
    evs = {e.name: e for e in c.collect()}
    sites = [(s.venue, s.city) for s in expand_sites(evs["Ocean State Showdown"])]
    assert sites == [("Brown University", "Providence"), ("Moses Brown School", "Providence"), ("East Providence HS", "Providence"),
                     ("PCD Complex", "Providence"), ("Bishop Hendricken High School", "Warwick"), ("JWU Harborside Campus", "Cranston")]
    sb = evs["Spring Break Tournament"]
    assert (sb.venue, sb.city, sb.state) == ("Paradise Coast Sports Complex", "Naples", "FL")
