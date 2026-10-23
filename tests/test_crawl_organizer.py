"""Offline checks for Crawl Organizer (collectors/organizers/): extractors, page finding, event rows, organizer record,
the vendor-contact rule and matching. A small fake organizer site with made-up .test contacts; no network."""
import datetime as dt
import json

from collectors.organizers import extract as X
from collectors.organizers import crawl as C
from collectors.organizers import registry as R
from collectors.organizers import match as M

TODAY = dt.date(2026, 10, 22)

HOME = """<html><body><nav><a href="/tournaments/">Tournaments</a> <a href="/contact-us/">Contact</a>
<a href="/become-a-vendor/">Vendors</a> <a href="https://www.facebook.com/x">fb</a>
<a href="https://www.tourneymachine.com/Public/Results/Tournament.aspx?IDTournament=h1">Brackets</a></nav>
<footer>info@laxevents.test | (410) 555-0134</footer></body></html>"""
LIST = """<html><body><h1>Boys Tournaments</h1>
<div class="card"><h3>Boys Fall Brawl</h3><p>Saturday, November 7th - 8th, 2026</p><p>Classes: 2030 2031</p>
<p>Bell Branch Park 1301 Bell Branch Way Crofton, MD 21114</p><a href="/event/fall-brawl/">View Details</a></div>
<div class="card"><h3>Girls Winter Sixes</h3><p>Dec 12, 2026</p><p>Capped at 48 teams. U12 U14</p>
<a href="/event/winter-sixes/">Register</a></div>
<div class="card"><h3>Summer Skills Camp</h3><p>July 6, 2027</p></div>
<div class="card"><h3>Spring Opener 2026</h3><p>March 2, 2026</p></div>
</body></html>"""
DETAIL = """<html><body><h1>Boys Fall Brawl</h1><p>November 7 - 8, 2026</p><p>Games 8:00 AM to 5:30 PM both days.</p>
<p>Location: Bell Branch Park</p></body></html>"""
SIXES = """<html><body><h1>Girls Winter Sixes</h1><p>December 12, 2026</p>
<p>Held at Centercourt Club, 185 Amboy Rd, Morganville, NJ 07751</p></body></html>"""
CONTACT = """<html><body><p>Questions? <a href="mailto:director@laxevents.test">Email us</a> or call 410.555.0199</p></body></html>"""
VENDOR = """<html><body><h2>Become a Vendor</h2><p>Vendor Coordinator: Jordan Smith, vendors@laxevents.test, (410) 555-0177.
Booths are 10x10.</p></body></html>"""
PAGES = {"https://laxevents.test/": HOME, "https://laxevents.test/tournaments/": LIST,
         "https://laxevents.test/event/fall-brawl/": DETAIL, "https://laxevents.test/event/winter-sixes/": SIXES,
         "https://laxevents.test/contact-us/": CONTACT, "https://laxevents.test/become-a-vendor/": VENDOR}


class FakeFetch:
    pages = 0
    def get(self, url):
        self.pages += 1
        return PAGES.get(url)


ORG = {"slug": "test-lax", "name": "Test Lacrosse Events", "canon": "test lacrosse", "website": "https://laxevents.test/",
       "scraper": None, "qualifies_vendor": True}


def test_dates_handle_weekdays_ordinals_ranges_and_october():
    assert X.find_dates("Saturday, November 7th - 8th, 2026", TODAY) == [(dt.date(2026, 11, 7), dt.date(2026, 11, 8))]
    assert X.find_dates("October 10 - 11, 2026", TODAY)[0] == (dt.date(2026, 10, 10), dt.date(2026, 10, 11))
    assert X.find_dates("Dec 30, 2026 - Jan 2, 2027", TODAY)[0] == (dt.date(2026, 12, 30), dt.date(2027, 1, 2))
    assert X.find_dates("March 14 - 15", TODAY)[0][0] == dt.date(2027, 3, 14)        # no year: next occurrence


def test_address_city_venue_ages_gender_teams_times():
    text = "Classes: 2028 2029 2030 2031 Capelli Sport Complex 1659 Wayside Rd Tinton Falls, NJ 07712 View Details"
    addr = X.find_addresses(text)[0]
    assert addr == "1659 Wayside Rd Tinton Falls, NJ 07712"                           # not "2031 Capelli ..."
    assert X.venue_before(text, addr) == "Capelli Sport Complex"
    assert X.find_city_states("Bryant University Providence, RI; College Park, MD") == ["Providence, RI", "College Park, MD"]
    assert X.ages_gender("Girls Prelim October 24, 2026 Classes: 2030 2031", 2026) == ("2030–2031", 2030, 2031, "girls")
    assert X.find_team_count("Capped at 48 teams") == 48 and X.find_team_count("Team cap: 64") == 64
    assert X.find_times("Games 8:00 AM to 5:30 PM") == ("8:00 AM", "5:30 PM")


def test_contacts_and_vendor_contact():
    assert X.find_emails(CONTACT) == ["director@laxevents.test"]
    assert X.find_phones("call 410.555.0199") == ["(410) 555-0199"]
    vc = X.vendor_contact("Vendor Coordinator: Jordan Smith, vendors@laxevents.test, (410) 555-0177. Booths are 10x10.")
    assert vc == {"name": "Jordan Smith", "email": "vendors@laxevents.test", "phone": "(410) 555-0177"}


def test_discover_classifies_links_and_records_platforms():
    found = C.discover(HOME, "https://laxevents.test/")
    assert found["events"] == ["https://laxevents.test/tournaments/"]
    assert found["contact"] == ["https://laxevents.test/contact-us/"]
    assert found["vendor"] == ["https://laxevents.test/become-a-vendor/"]
    assert len(found["platforms"]) == 1 and "tourneymachine" in found["platforms"][0]


def test_crawl_organizer_end_to_end():
    res = C.crawl_organizer(ORG, fetch=FakeFetch(), today=TODAY)
    evs = {e["name"]: e for e in res["events"]}
    assert set(evs) == {"Boys Fall Brawl", "Girls Winter Sixes"}                       # camp and past event dropped
    brawl, sixes = evs["Boys Fall Brawl"], evs["Girls Winter Sixes"]
    assert (brawl["start"], brawl["end"]) == (dt.date(2026, 11, 7), dt.date(2026, 11, 8))
    assert brawl["address"] == "1301 Bell Branch Way Crofton, MD 21114" and brawl["venue_name"] == "Bell Branch Park"
    assert brawl["city_states"][0] == "Crofton, MD" and brawl["gender"] == "boys" and brawl["grad_year_min"] == 2030
    assert (brawl["start_time"], brawl["end_time"]) == ("8:00 AM", "5:30 PM")
    assert sixes["team_count"] == 48 and sixes["gender"] == "girls" and sixes["city_states"][0] == "Morganville, NJ"
    rec = res["organizer"]
    assert rec["main_email"] == "director@laxevents.test" or rec["main_email"] == "info@laxevents.test"
    assert rec["main_phone"] == "(410) 555-0134" and rec["vendor_page"].endswith("/become-a-vendor/")
    assert rec["vendor_contact"]["name"] == "Jordan Smith"


def test_vendor_contact_only_for_qualifying_organizers():
    res = C.crawl_organizer({**ORG, "qualifies_vendor": False}, fetch=FakeFetch(), today=TODAY)
    assert res["organizer"]["vendor_page"] and not any(res["organizer"]["vendor_contact"].values())


def test_event_rows_have_venues_and_no_contacts():
    res = C.crawl_organizer(ORG, fetch=FakeFetch(), today=TODAY)
    rows = [C.to_row(ORG, e) for e in res["events"]]
    brawl = next(r for r in rows if r["name"] == "Boys Fall Brawl")
    assert brawl["source"] == "org-test-lax" and brawl["venues"][0]["address"].startswith("1301 Bell Branch Way")
    assert "@laxevents.test" not in json.dumps(rows, default=str)                          # no emails in event rows


def test_registry_vendor_rule_uses_precedent_not_registrations(tmp_path):
    lines = [{"sport": "lacrosse", "source": "tourneymachine", "organizer_name": "Late Fill Lax", "team_count": 5,
              "expected_teams": 180, "state": "MD", "start_date": "2026-11-07"},
             {"sport": "lacrosse", "source": "tourneymachine", "organizer_name": "Tiny Lax", "team_count": 6, "state": "MD", "start_date": "2026-11-07"},
             {"sport": "lacrosse", "source": "tourneymachine", "organizer_name": "Hogan's", "team_count": 30, "state": "MD", "start_date": "2026-11-07"}]
    (tmp_path / "event_candidates.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    orgs = {o["name"]: o for o in R.build(str(tmp_path), seed={})}
    assert orgs["Late Fill Lax"]["qualifies_vendor"] is True          # 5 registered today, 180 expected
    assert orgs["Tiny Lax"]["qualifies_vendor"] is False
    assert "Hogan's" not in orgs and orgs["Hogan's Lacrosse"]["events"] == 1   # merged into the known organizer


def test_match_marks_duplicates_and_confirms_placeholders(tmp_path):
    app = [{"source": "tourneymachine", "dedupe_key": "tourneymachine:h9", "sport": "lacrosse", "name": "2026 Fall Brawl",
            "organizer_name": "Test Lacrosse Events", "start_date": "2026-11-07", "state": "MD", "status": "open"},
           {"source": "recurrence", "dedupe_key": "recurrence:x:2026", "sport": "lacrosse", "name": "2025 Winter Sixes",
            "start_date": "2026-12-12", "state": "NJ", "status": "expected", "raw": {"gender": "girls"}}]
    (tmp_path / "event_candidates.jsonl").write_text("".join(json.dumps(l) + "\n" for l in app))
    res = C.crawl_organizer(ORG, fetch=FakeFetch(), today=TODAY)
    rows = [C.to_row(ORG, e) for e in res["events"]]
    results, summary = M.match_events(rows, str(tmp_path), today=TODAY)
    by = {r["name"]: r for r in results}
    assert by["Boys Fall Brawl"]["duplicate_of"] == "tourneymachine:h9"                 # already in the app
    assert by["Girls Winter Sixes"]["placeholder"]["dedupe_key"] == "recurrence:x:2026"
    assert summary["already_in_app"] == 1 and summary["new"] == 1


# ---- PrimeTime-style pages: same event on the homepage and a listing page, "&" and "/" dates, label rows ----
PT_HOME = """<html><body><a href="/events/">Events</a>
<div class="ev"><h3>Colonial Clash Nov. 14th / 15th, 2026</h3><p>Amherst, MA</p></div>
<div class="ev"><h3>Lone Star Cup Dec. 5th &amp; 6th, 2026</h3><p>Dates Arlington, TX</p></div>
<div class="ev"><h4>Attending College Coaches</h4><p>July 3, 2027</p></div>
<div class="ev"><h4>Location:</h4><p>July 7, 2027 Bryant University, Smithfield, RI</p></div>
</body></html>"""
PT_EVENTS = """<html><body>
<div class="ev"><h3>Colonial Clash</h3><p>November 14, 2026</p><p>UMass Amherst, 300 Massachusetts Ave, Amherst, MA 01003</p>
<a href="/colonial-clash/">Details</a></div></body></html>"""
PT_PAGES = {"https://pt.test/": PT_HOME, "https://pt.test/events/": PT_EVENTS, "https://pt.test/colonial-clash/": "<h1>Colonial Clash</h1>"}


class PTFetch:
    pages = 0
    def get(self, url):
        self.pages += 1
        return PT_PAGES.get(url)


def test_multi_day_dates_with_ampersand_slash_and_and():
    assert X.find_dates("Nov. 14th / 15th, 2026", TODAY)[0] == (dt.date(2026, 11, 14), dt.date(2026, 11, 15))
    assert X.find_dates("Dec. 5th & 6th, 2026", TODAY)[0] == (dt.date(2026, 12, 5), dt.date(2026, 12, 6))
    assert X.find_dates("June 26th and 27th, 2027", TODAY)[0] == (dt.date(2027, 6, 26), dt.date(2027, 6, 27))


def test_city_drops_label_words():
    assert X.find_city_states("Dates Arlington, TX; Where: Naples, FL") == ["Arlington, TX", "Naples, FL"]


def test_names_lose_their_dates_and_label_rows_are_dropped():
    assert C.clean_name("Laxtoberfest Oct. 25th, 2026") == "Laxtoberfest"
    assert C.JUNK_NAME.search(C.clean_name("Location:")) and C.JUNK_NAME.search("Attending College Coaches")


def test_same_event_on_two_pages_merges_into_one_with_the_full_dates():
    res = C.crawl_organizer({**ORG, "website": "https://pt.test/", "qualifies_vendor": False}, fetch=PTFetch(), today=TODAY)
    names = [e["name"] for e in res["events"]]
    assert sorted(names) == ["Colonial Clash", "Lone Star Cup"]                        # no duplicate, no label rows
    clash = next(e for e in res["events"] if e["name"] == "Colonial Clash")
    assert (clash["start"], clash["end"]) == (dt.date(2026, 11, 14), dt.date(2026, 11, 15))   # homepage "14th / 15th" kept
    assert clash["address"] == "300 Massachusetts Ave, Amherst, MA 01003"                     # listing page's address merged in
    star = next(e for e in res["events"] if e["name"] == "Lone Star Cup")
    assert star["city_states"][0] == "Arlington, TX" and star["end"] == dt.date(2026, 12, 6)


def test_two_same_day_events_side_by_side_stay_two_events():
    html = """<html><body><div class="grid">
    <div><div><h3><a href="/lax/">Laxtoberfest</a></h3></div><div><span>Oct. 25th, 2026</span></div></div>
    <div><div><h3><a href="/fc/">Fall Classic (Girls)</a></h3></div><div><span>Oct. 25th, 2026</span></div></div>
    </div></body></html>"""
    assert sorted(b["name"] for b in C.event_blocks(html, "https://pt.test/", TODAY)) == ["Fall Classic (Girls)", "Laxtoberfest"]


# ---- JavaScript-built lists: empty shell over plain HTTP, events only after rendering; start-here links ----
SHELL = "<html><body><nav><a href='/page/about'>About</a></nav><div id='programs'></div></body></html>"
JS_LIST = """<html><body><div id='programs'>
<div class="card"><h3><a href="https://register.jsorg.test/register.aspx?EventID=1">2026 Boys Fall Grand Prix</a></h3>
<p>Oct 24 - 25, 2026</p><p>Droplight Grand Park Sports Campus, Westfield, IN</p></div>
<div class="card"><h3><a href="https://register.jsorg.test/register.aspx?EventID=2">Constitution Box Cup</a></h3>
<p>Dec 19 - 20, 2026</p><p>Oakwood Sports Center, Glastonbury, CT</p></div></div></body></html>"""


class JSFetch:
    render = True
    def __init__(self): self.pages, self.rendered_urls = 0, []
    def get(self, url):
        self.pages += 1
        return SHELL if url.startswith("https://www.jsorg.test/") else None
    def rendered(self, url):
        self.rendered_urls.append(url)
        return JS_LIST if url == "https://www.jsorg.test/page/all-events" else None


def test_javascript_list_is_rendered_from_the_start_page():
    org = {**ORG, "slug": "jsorg", "website": "https://www.jsorg.test/", "qualifies_vendor": False,
           "start_pages": ["https://www.jsorg.test/page/all-events"]}
    f = JSFetch()
    res = C.crawl_organizer(org, fetch=f, today=TODAY)
    names = sorted(e["name"] for e in res["events"])
    assert names == ["Boys Fall Grand Prix", "Constitution Box Cup"]
    assert "https://www.jsorg.test/page/all-events" in f.rendered_urls
    cup = next(e for e in res["events"] if e["name"] == "Constitution Box Cup")
    assert (cup["start"], cup["end"]) == (dt.date(2026, 12, 19), dt.date(2026, 12, 20)) and cup["city_states"][0] == "Glastonbury, CT"


def test_no_rendering_when_the_fetcher_cannot_render():
    org = {**ORG, "slug": "jsorg", "website": "https://www.jsorg.test/", "qualifies_vendor": False,
           "start_pages": ["https://www.jsorg.test/page/all-events"]}
    f = JSFetch(); f.render = False
    assert C.crawl_organizer(org, fetch=f, today=TODAY)["events"] == [] and f.rendered_urls == []


def test_uppercase_month_names_parse():
    assert X.find_dates("MAY 9 - 10, 2027", TODAY)[0] == (dt.date(2027, 5, 9), dt.date(2027, 5, 10))


def test_button_text_is_never_the_name_and_caps_are_tidied():
    html = """<html><body><div class="card"><div><span>NJ FALL CLASSIC</span>
    <p>OCTOBER 31 - NOVEMBER 1, 2026 | METUCHEN, NJ</p><p>BOYS 2036-2028</p>
    <a href="/play/nj-fall-classic"><span class="fl-button-text">Find Out More</span></a></div></div>
    <div class="card"><div><span>ZION SUMMIT CLASSIC</span><p>NOVEMBER 6 - 7, 2026 | ST. GEORGE, UT</p>
    <a href="/play/zion"><span>Find Our More</span></a></div></div></body></html>"""
    bl = C.event_blocks(html, "https://pll.test/play/tournaments", TODAY)
    assert sorted(b["name"] for b in bl) == ["NJ Fall Classic", "Zion Summit Classic"]
    assert X.find_city_states("ST. GEORGE, UT | METUCHEN, NJ") == ["St. George, UT", "Metuchen, NJ"]


def test_list_pages_rank_before_single_event_pages():
    home = """<a href="/events/autumn-drop/">Autumn Drop</a> <a href="/events/norcal-ascent/">NorCal Ascent</a>
    <a href="/trilogy-events-for-teams/?_season=Fall">Fall</a> <a href="/trilogy-events-for-teams/">Team Events</a>"""
    assert C.discover(home, "https://tri.test/")["events"][0] == "https://tri.test/trilogy-events-for-teams/"


# ---- Hogan-style cards: the name is only in the logo; "Learn More" leads to the event page that names it ----
HOGAN_HOME = """<html><body><h2>Upcoming Events</h2><div class="row">
<div class="col"><img src="https://cdn.test/uploads/116/Event%20Logos/Fall%20Brawl/FB_Primary_FC.png">
<h4>NOVEMBER 8, 2026</h4><p>Annapolis, MD</p><a href="https://register.hogan.test/page/fallbrawl">Learn More</a></div>
<div class="col"><img src="https://cdn.test/uploads/116/Event%20Logos/Spring%20Thaw/ST_Primary_FC.png">
<h4>March 7, 2027</h4><p>Edgewood, MD</p><a href="https://register.hogan.test/page/spring-thaw">LEARN MORE</a></div>
</div><p>Melt the ice and prep your squad for the upcoming season with Spring Thaw on March 20, 2027 at the dome.</p></body></html>"""
HOGAN_PAGES = {"https://www.hogan.test/": HOGAN_HOME,
               "https://register.hogan.test/page/fallbrawl": "<html><head><title>Fall Brawl | Hoganlax</title></head><body><h1>Fall Brawl 2026</h1></body></html>",
               "https://register.hogan.test/page/spring-thaw": "<html><body><h1>Spring Thaw Lacrosse Tournament</h1></body></html>"}


class HoganFetch:
    pages = 0
    def get(self, url):
        self.pages += 1
        return HOGAN_PAGES.get(url)


def test_logo_only_cards_get_names_from_their_event_pages():
    org = {**ORG, "slug": "hogan", "website": "https://www.hogan.test/", "qualifies_vendor": False}
    res = C.crawl_organizer(org, fetch=HoganFetch(), today=TODAY)
    got = sorted((e["name"], str(e["start"]), e["city_states"][0]) for e in res["events"])
    assert got == [("Fall Brawl", "2026-11-08", "Annapolis, MD"), ("Spring Thaw Lacrosse Tournament", "2027-03-07", "Edgewood, MD")]


def test_name_from_card_uses_the_logo_folder():
    from bs4 import BeautifulSoup
    el = BeautifulSoup(HOGAN_HOME, "lxml").find("div", class_="col")
    assert C.name_from_card(el) == "Fall Brawl"


def test_ml8_double_dash_dates_city_tags_and_heading_rows():
    assert X.find_dates("FUTURE PHENOMS FLORIDA January 9 - - January 10 2027", TODAY)[0] == (dt.date(2027, 1, 9), dt.date(2027, 1, 10))
    assert X.find_city_states("Bel Air, MD, Lacrosse Tournament Edgewood, MD") == ["Bel Air, MD", "Edgewood, MD"]
    assert C.HEADING_NAME.match("Events") and C.HEADING_NAME.match("Upcoming Events")
    assert not C.HEADING_NAME.match("Tournament of Champions")
