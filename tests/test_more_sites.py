"""Offline checks for OT Lacrosse, Exposure Events, Legends Lacrosse and the age-division / contact readers
(adapters/more_sites.py, adapters/details.py). No network."""
import base64
import datetime as dt

from bs4 import BeautifulSoup

from adapters import details
from adapters.more_sites import OTLaxCollector, ExposureCollector, LegendsCollector, CarpetbaggerCollector, title_case, month_span, dates_with_year
from core.model import Event


def fake_pages(c, monkeypatch, pages):
    monkeypatch.setattr(c, "get", lambda url, **k: pages.get(url, "<html></html>"))
    monkeypatch.setattr(c, "soup", lambda url, **k: BeautifulSoup(pages.get(url, "<html></html>"), "lxml"))


def test_grad_years_from_years_grades_and_age_groups():
    nov, jan, june = dt.date(2026, 11, 7), dt.date(2027, 1, 16), dt.date(2027, 6, 12)
    assert details.grad_years("Grad Years Eligible: 2027-2035/2036 Registration Fee", nov) == "2027–2036"
    # a fall event is next spring's school year: 'November 7 & 8, 2026' is a date, and 2026 grads have left
    assert details.grad_years("Elite 8 November 7 & 8, 2026 Divisions Grad years 2030–2036 Saturday", nov) == "2030–2036"
    assert details.grad_years("Grad Years Eligible: HS Varsity, HS B, 7/8, 5/6, 3/4 Registration", jan) == "2027–2036"
    assert details.grad_years("ages: Boys & Girls: 18U-8U/12th-1st", nov) == "2027–2038"
    assert details.grad_years("Divisions span grad years 2027 through 2036 .", june) == "2027–2036"
    assert details.grad_years("Registration opens October 1, 2026. Pricing $1750", nov) is None


def test_page_contacts_roles_names_and_junk():
    text = ("Register Your Team Brian Santoleri · brian@legendslax.com Days Until ... Email: events@legendslax.com "
            "Schedule requests to name@legendslax.com. Hotels: team@athleteshospitality.com "
            "How do I become a sponsor or vendor? All sponsors and vendors can send an inquiry to us at info@otlax.com "
            "or call (610) 555-0142")
    got = details.page_contacts(text)
    assert [(c["role"], c["email"], c["name"]) for c in got] == [
        ("vendor", "info@otlax.com", None), ("director", "brian@legendslax.com", "Brian Santoleri"),
        ("organizer", "events@legendslax.com", None)]
    assert got[0]["phone"] == "610-555-0142"
    html = '<p>Contact <a href="/cdn-cgi/l/email-protection#' + "42" + "".join(f"{b ^ 0x42:02x}" for b in b"amy@club.org") + '">x</a></p>'
    assert "amy@club.org" in details.full_text(html)          # Cloudflare-protected email decoded


def test_names_dates_and_leagues():
    assert title_case("The sUMMER aUSTIN aUTHENTIC") == "The Summer Austin Authentic"
    assert month_span("MAR - MAY, 2027") == (dt.date(2027, 3, 1), dt.date(2027, 5, 31))
    s, e, note = dates_with_year("DEC 12-13", today=dt.date(2026, 10, 27))
    assert (s, e) == (dt.date(2026, 12, 12), dt.date(2026, 12, 13)) and note
    s, e, note = dates_with_year("JAN 16- 17, 2027")
    assert (s, e, note) == (dt.date(2027, 1, 16), dt.date(2027, 1, 17), None)


OT_LIST = """<div class="card"><h3>The cHICAGO sTAKES</h3><p>NOV 7-8, 2026</p><p>New Lennox, IL</p>
  <a href="https://otlax.com/events/the-chicago-stakes/">View Event</a></div>
<div class="card"><h3>The Philly Spring League</h3><p>MAR - MAY, 2027</p><p>GREATER PHILLY AREA</p>
  <a href="https://otlax.com/events/event-spring/">View Event</a></div>"""
OT_STAKES = """<p>Dates: Saturday November 7 - Sunday November 8, 2026 Location: Wintrust Crossroads Sports Complex 520 Cedar
  Crossings Drive, New Lenox IL, 60451 Grad Years Eligible: 2027-2035/2036 Registration Fee: $1750</p>
  <p>How do I become a sponsor or vendor? All sponsors and vendors can send an inquiry to us at info@otlax.com</p>"""
OT_LEAGUE = """<p>Location: Greater Philly Area Grad Years Eligible: 2031-2036 Registration Fee: $3500</p>
  <p>Spring League Venues Germantown Academy 340 Morris Road, Fort Washington PA 19034 Get Directions WSFS Bank Sportsplex
  2485 Seaport Drive, Chester, PA, 19013 Get Directions Hotels Click here</p>"""


def test_otlax_cards_event_pages_and_leagues(monkeypatch):
    c = OTLaxCollector(use_cache=False)
    fake_pages(c, monkeypatch, {c.URL: OT_LIST, "https://otlax.com/events/the-chicago-stakes/": OT_STAKES,
                                "https://otlax.com/events/event-spring/": OT_LEAGUE})
    evs = details.enrich(c, c.collect())
    stakes, league = evs
    assert (stakes.name, stakes.start_date, stakes.end_date) == ("The Chicago Stakes", dt.date(2026, 11, 7), dt.date(2026, 11, 8))
    assert (stakes.venue, stakes.city, stakes.state) == ("Wintrust Crossroads Sports Complex", "New Lenox", "IL")
    assert stakes.address.startswith("520 Cedar") and stakes.age_range == "2027–2036"
    assert stakes.contacts[0] == {"name": None, "email": "info@otlax.com", "phone": None, "role": "vendor"}
    assert league.event_type == "league" and league.date_note and league.start_date == dt.date(2027, 3, 1)
    assert [s[:3] for s in league.sites] == [("Germantown Academy", "Fort Washington", "PA"), ("WSFS Bank Sportsplex", "Chester", "PA")]


EXPOSURE_PAGE = """<script>app.viewModel.events.init({ criteria: { StartDateString: "10/27/2026", },
  tokenName: 'X-Exposure-Token', tokenValue: 'test-token' });</script>"""


def _exp(name, org, gender, **kw):
    return {"Name": name, "OrganizationName": org, "Gender": gender, "StartDate": "2026-11-07T00:00:00.000",
            "EndDate": "2026-11-08T00:00:00.000", "Location": "Clarkston High School", "StreetAddress": "6093 Fleming Lake Road",
            "City": "Clarkston", "StateRegionAbbr": "MI", "PostalCode": "48346", "ContactName": "Jimmy James",
            "ContactEmail": "LaxBash@gmail.com", "ContactPhone": None, "Type": "Tournament", "Link": "https://x.test/1", **kw}


def test_exposure_reads_the_json_table_and_skips_organizers_we_scrape(monkeypatch):
    c = ExposureCollector(use_cache=False)
    posts = []
    class Resp:
        def __init__(self, d): self.d, self.text = d, EXPOSURE_PAGE
        def json(self): return self.d
    monkeypatch.setattr(c.session, "get", lambda url, **k: Resp(None))
    def post(url, data=None, headers=None, **k):
        posts.append((data["Page"], headers["X-Exposure-Token"]))
        return Resp({"Total": 3, "Results": [_exp("2006 The Turkey Shoot", "Lax Bash Tournaments", 0, YouthAgeGradesBoth="Boys & Girls: 18U-8U/12th-1st"),
                                             _exp("NXT | Fall Bulldog Bash 2026", "NXT", 1),
                                             _exp("BWI League", "Robinson Sports", 2, Type="League", YouthAgeGradesFemale="12th-8th")]})
    monkeypatch.setattr(c.session, "post", post)
    evs = c.collect()
    assert posts == [(1, "test-token")]
    assert [e.name for e in evs] == ["2006 The Turkey Shoot", "BWI League"]                 # NXT left to its own scraper
    t, bwi = evs
    assert (t.organizer, t.gender, t.venue, t.city, t.state) == ("Lax Bash Tournaments", "coed", "Clarkston High School", "Clarkston", "MI")
    assert t.address == "6093 Fleming Lake Road, Clarkston, MI 48346" and t.age_range == "2027–2038"
    assert t.contacts == [{"name": "Jimmy James", "email": "laxbash@gmail.com", "phone": None, "role": "director"}]
    assert (bwi.gender, bwi.event_type, bwi.age_range) == ("girls", "league", "2027–2031")


LEGENDS_GRID = """<div>var EVENTS = [
    { n:"Elite 8", c:["Fall/Winter","Recruiting Events"], t:"Fall", g:"Boys", dt:"Nov 7–8, 2026", v:"Saint Joseph High School · Metuchen, NJ", w:"https://legendslax.com/legends-elite-8/", l:"Logos5.png" },
    { n:"Fall Exposure Showcase", c:["Fall/Winter"], g:"Boys", dt:"Nov 13, 2026", v:"Warhill Sports Complex · Williamsburg, VA", w:"https://legendslax.com/legends-chrome-classic/#exposure", l:(window.LGD_LAXWARS || {}).fallExposureLogo || "" },
    { n:"Lax Wars", c:"Summer", g:LW.division, dt:LW.date, v:LW.venue, w:LW.url, l:LW.logo }
  ];</div>"""
LEGENDS_PAGE = ('<script>window.LGD_LAXWARS = { url: "https://legendslax.com/lax-wars/", date: "June 26-27, 2027", '
                'venue: "Newington High School · Newington, CT", division: "Boys" };</script>'
                '<script>var h=decodeURIComponent(escape(atob("' + base64.b64encode(LEGENDS_GRID.encode()).decode() + '")));</script>')


def test_legends_reads_the_encoded_grid(monkeypatch):
    c = LegendsCollector(use_cache=False)
    fake_pages(c, monkeypatch, {c.URL: LEGENDS_PAGE, "https://legendslax.com/legends-elite-8/":
                                "<p>Grad Years 2030-2036</p><p>Register Your Team Brian Santoleri · brian@legendslax.com</p>"})
    evs = c.collect()
    assert [e.name for e in evs] == ["Elite 8", "Fall Exposure Showcase", "Lax Wars"]
    elite, showcase, wars = evs
    assert (elite.venue, elite.city, elite.state, elite.gender) == ("Saint Joseph High School", "Metuchen", "NJ", "boys")
    assert elite.age_range == "2030–2036" and elite.contacts[0]["name"] == "Brian Santoleri"
    assert showcase.event_type == "showcase"
    assert (wars.start_date, wars.venue, wars.city) == (dt.date(2027, 6, 26), "Newington High School", "Newington")


def test_contacts_reach_the_row_and_the_contacts_payload():
    from collectors.woodgoods_lacrosse import to_common, contact_updates
    ev = Event(organizer="OT Lacrosse", name="The Chicago Stakes", start_date=dt.date(2026, 11, 7), venue="Wintrust", city="New Lenox",
               state="IL", contacts=[{"name": None, "email": "info@otlax.com", "phone": None, "role": "vendor"},
                                     {"name": "Brett Manney", "email": "brett@otlax.com", "phone": "610-555-0142", "role": "director"}])
    row = to_common(ev, "otlax")
    assert row["vendor_contact_email"] == "info@otlax.com" and len(row["raw"]["contacts"]) == 2
    cs = contact_updates([row])
    assert [(c["role"], c["email"], c["enriched_via"]) for c in cs] == [("vendor_coordinator", "info@otlax.com", "site"),
                                                                        ("director", "brett@otlax.com", "site")]
    assert cs[0]["dedupe_key"] == f"wg-otlax:{row['source_event_id']}"


def test_contacts_keep_the_organizers_own_addresses_with_their_own_roles():
    text = ("Questions? info@victoryeventseries.com Staffing: staffing@victoryeventseries.com Vendors and sponsors: "
            "vendors@victoryeventseries.com Schedule: cal@victoryeventseries.com Travel: sports@teamtravelsource.com "
            "Software: help@eventbeacon.com contact@mysite.com Coach Bob bob.smith@gmail.com View Recap info@apex.test")
    got = details.page_contacts(text, {"victoryeventseries.com"})
    assert {c["email"]: c["role"] for c in got} == {"vendors@victoryeventseries.com": "vendor", "info@victoryeventseries.com": "organizer",
                                                    "staffing@victoryeventseries.com": "organizer", "cal@victoryeventseries.com": "organizer",
                                                    "bob.smith@gmail.com": "director"}
    assert details.site_domain("https://events.nxtsports.com/site/x") == "nxtsports.com"
    assert details.page_contacts("View Recap info@apex.test")[0]["name"] is None


CB_LIST = """<a href="https://cblax.org/events/lkn-fall-classic/">Details</a><a href="http://cblacrosse.leagueapps.com/app/tournaments/11">Register</a>
<a href="https://cblax.org/events/christmas-jam/">Details</a><a href="http://cblacrosse.leagueapps.com/app/tournaments/22">Register</a>"""
CB_LKN = """<p>Details Hotels Divisions Local Info Details Boys and Girls November 7-8, 2026 Mooresville and Huntersville, NC
Tournament Locations Games will be played across three excellent facilities. BOYS DIVISIONS All boys' games will be played at Mazeppa
Park and Cornelius Road Park in Mooresville. Mazeppa Park 645 Mazeppa Rd, Mooresville, NC 28115 Mazeppa Park features six lighted
fields Permanent Bathrooms Cornelius Road Park 247 Cornelius Rd, Mooresville, NC 28115 Cornelius Road Park features: 3 turf fields
GIRLS DIVISIONS All girls' games will be played at Richard Barry Memorial Park in Huntersville. Richard Barry Memorial Park 13707
Beatties Ford Rd, Huntersville, NC 28078 Join Us at Lake Norman! Divisions ... contact us trent@cblax.org</p>
<footer>Trent Brown: trent@cblax.org Steve Schattner: steve@cblax.org</footer>"""
CB_JAM = """<p>Details Hotels Divisions Local Info Details Boys and Girls Sunday December 6, 2026 Mazeppa Rd Park Mooresville, NC
Fast-Paced Lacrosse. Mazeppa Park 645 Mazeppa Rd, Mooresville, NC 28115 All games will be played at Mazeppa Park. Divisions Varsity</p>"""


def test_carpetbagger_pairs_pages_with_leagueapps_and_splits_boys_and_girls_venues(monkeypatch):
    c = CarpetbaggerCollector(use_cache=False)
    fake_pages(c, monkeypatch, {c.URL: CB_LIST, "https://cblax.org/events/lkn-fall-classic/": CB_LKN,
                                "https://cblax.org/events/christmas-jam/": CB_JAM})
    tours = {"11": {"name": "2026 Lake Norman Fall Classic", "startDate": "2026-11-07", "endDate": "2026-11-08",
                    "location": {"name": "Mazeppa Road Park"},
                    "divisions": [{"name": "2027 Girls", "gender": "female"}, {"name": "2035 Girls", "gender": "female"},
                                  {"name": "2028 Boys", "gender": "male"}, {"name": "2036 Boys", "gender": "male"}]},
             "22": {"name": "2026 Christmas Jam", "startDate": "2026-12-06", "endDate": "2026-12-06", "location": {"name": "Mazeppa Road Park"},
                    "divisions": [{"name": "Varsity Boys 2027/2028", "gender": "male"}, {"name": "Elementary Girls 2033/2034", "gender": "female"}]}}
    class Resp:
        def __init__(self, d): self.d = d
        def json(self): return self.d
    monkeypatch.setattr(c.session, "get", lambda url, **k: Resp(tours[url.rsplit("/", 1)[1]]))
    boys, girls, jam = c.collect()
    assert (boys.name, boys.gender, boys.age_range, boys.divisions) == ("Lake Norman Fall Classic (Boys)", "boys", "2028–2036", ["2028 Boys", "2036 Boys"])
    assert [s[:3] for s in boys.sites] == [("Mazeppa Park", "Mooresville", "NC"), ("Cornelius Road Park", "Mooresville", "NC")]
    assert (girls.venue, girls.city, girls.age_range) == ("Richard Barry Memorial Park", "Huntersville", "2027–2035")
    assert (jam.name, jam.gender, jam.venue, jam.city, jam.age_range) == ("Christmas Jam", "coed", "Mazeppa Park", "Mooresville", "2027–2034")
    assert [(x["name"], x["email"]) for x in boys.contacts] == [("Trent Brown", "trent@cblax.org"), ("Steve Schattner", "steve@cblax.org")]
