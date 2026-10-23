"""Offline checks for this week's organizer-scraper fixes (adapters/). No network.

NXT / Aloha: event IDs come from the site's own /api/program list, so events added after the hand-typed IDs are not
missed; an event page that won't parse is built from the feed. Adrenaline: the year is worked out from today instead
of a fixed 2026/27 table, event pages are matched to index cards by shared words, and the cards behind 'Load More'
are fetched. APEX / MLT / Victory / Buku read every listing page (girls, showcases, regions), not just one."""
import datetime as dt

from bs4 import BeautifulSoup

from adapters.leagueapps import LeagueAppsCollector, NXTCollector
from adapters.misc_sites import AdrenalineCollector, VictoryCollector, TrilogyCollector, PrimeTimeCollector
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
