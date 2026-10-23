"""Offline checks for tools/lacrosse_match_report.py: a tiny fake export (placeholders, sheet, history, venues) and a few
organizer events, run end to end. No network."""
import csv
import datetime as dt
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import lacrosse_match_report as L  # noqa: E402

TODAY = dt.date.today()
D = lambda days: (TODAY + dt.timedelta(days=days)).isoformat()


def _write(path, rows):
    with open(path, "w") as f:
        for r in rows: f.write(json.dumps(r) + "\n")


def _run(tmp_path, app, scraped, history=(), venues=()):
    exp = tmp_path / "export"; exp.mkdir()
    _write(exp / "event_candidates.jsonl", app)
    _write(exp / "event_history.jsonl", history)
    _write(exp / "venues.jsonl", venues)
    _write(tmp_path / "scraped.jsonl", scraped)
    out = tmp_path / "report.csv"
    L.main(["--export-dir", str(exp), "--scraped", str(tmp_path / "scraped.jsonl"), "--out", str(out)])
    return {(r["event"] or r["app_event"]): r for r in csv.DictReader(open(out))}


def ph(name, start, city, state, hid, venue=None):
    return {"source": "recurrence", "sport": "lacrosse", "status": "expected", "name": name, "start_date": start, "end_date": start,
            "city_states": [f"{city}, {state}"], "state": state, "venue_name": venue, "raw": {"history_id": hid},
            "dedupe_key": f"recurrence:{hid}"}


def org(name, start, city, state, organizer="Victory", venue=None):
    return {"source": f"wg-{organizer.lower()}", "source_event_id": name.lower().replace(" ", "-"), "name": name, "sport": "lacrosse",
            "organizer_name": organizer, "start_date": start, "end_date": start, "city": city, "state": state, "venue_name": venue,
            "city_states": [f"{city}, {state}"], "raw": {"gender": "boys"}, "listing_url": "https://org.test/e"}


def test_week_late_placeholder_is_date_moved_with_the_reason(tmp_path):
    last_year = TODAY + dt.timedelta(days=200 - 364)
    app = [ph("Bama Bash 2026", D(207), "Hoover", "AL", "h1")]          # the app rolled last year's date + 371 days
    rows = _run(tmp_path, app, [org("Bama Bash", D(200), "Hoover", "AL")],
                history=[{"id": "h1", "start_date": last_year.isoformat()}])
    r = rows["Bama Bash"]
    assert r["status"] == "DATE MOVED" and "APP DATE A WEEK LATE" in r["tags"] and r["days_off"] == "-7"


def test_same_weekend_placeholder_is_confirmed_and_new_events_are_new(tmp_path):
    app = [ph("Empire of the Sun 2025", D(30), "Panama City Beach", "FL", "h2")]
    rows = _run(tmp_path, app, [org("Empire of The Sun", D(30), "Panama City Beach", "FL"),
                                org("Victory Tide", D(60), "Little River", "SC")])
    assert rows["Empire of The Sun"]["status"] == "CONFIRMED"
    assert rows["Victory Tide"]["status"] == "NEW"


def test_sheet_sites_are_one_event_and_unlisted_sheet_events_are_reported(tmp_path):
    sheet = lambda n, c, d: {"source": "manual-lacrosse-sheet", "sport": "lacrosse", "status": "new", "name": n, "start_date": d,
                             "end_date": d, "organizer_name": "Hogan's", "city": c, "state": "MD", "city_states": [f"{c}, MD"],
                             "dedupe_key": f"sheet:{n}"}
    app = [sheet("Fall Brawl – Site 1", "Gambrills", D(20)), sheet("Fall Brawl – Site 2", "Annapolis", D(20)),
           sheet("Queen of Club", "Annapolis", D(40))]
    rows = _run(tmp_path, app, [org("Fall Brawl", D(20), "Annapolis", "MD", organizer="Hogan's")])
    fb = rows["Fall Brawl"]
    assert fb["status"] == "CONFIRMED" and fb["app_event"] == "Fall Brawl – Site 2"          # the site in the organizer's town
    assert "Site 1" in fb["also_in_app"]
    assert rows["Queen of Club"]["status"] == "NOT ON ORGANIZER SITE"


def test_level_and_names_and_towns():
    assert L.match_name("Boys Connecticut Box Championships (BOX)") == "Boys CT Box Championships"
    assert L.match_name("Great Lax Box Classic (BOX) – site lists yr as 2026, presumed 2027") == "Great Lax Box Classic"
    assert L.match_name("Albany Invitational (HS)") == "Albany Invitational (HS)"
    assert L._town_key("URI, Kingston") == L._town_key("Kingston")
    # a venue written into city_states is not a town; 'DC AREA' is not a state
    assert L._city_state({"venue_name": "River City Sports Complex", "city_states": ["River City Sports Complex, VA"]}) == (None, "VA")
    assert L._city_state({"city": "Washington", "state": "DC AREA"}) == ("Washington", None)


def test_badges_and_descriptions(tmp_path):
    last_year = TODAY + dt.timedelta(days=200 - 364)
    app = [ph("Bama Bash 2026", D(207), "Hoover", "AL", "h1"), ph("Empire of the Sun 2025", D(30), "Panama City Beach", "FL", "h2")]
    rows = _run(tmp_path, app, [org("Bama Bash", D(200), "Hoover", "AL"), org("Empire of The Sun", D(30), "Panama City Beach", "FL"),
                                org("Victory Tide", D(60), "Little River", "SC")],
                history=[{"id": "h1", "start_date": last_year.isoformat()}])
    assert rows["Empire of The Sun"]["badge"] == "🟢 Confirmed · organizer website"
    assert rows["Bama Bash"]["badge"] == "🔴 Date wrong · organizer says " + L.fmt_dates(D(200), D(200))
    assert rows["Bama Bash"]["badge_description"] == "Organizer might have changed the date"
    assert rows["Victory Tide"]["badge"].startswith("🔵 New")
    assert L.fmt_dates("2027-06-19", "2027-06-20") == "Jun 19–20, 2027"
    assert L.fmt_dates("2027-06-29", "2027-07-01") == "Jun 29 – Jul 1, 2027"


def test_confirmed_needs_the_place_too(tmp_path):
    sheet = lambda n, c, st: {"source": "manual-lacrosse-sheet", "sport": "lacrosse", "status": "new", "name": n, "start_date": D(40),
                              "end_date": D(40), "organizer_name": "Adrenaline", "city": c, "state": st,
                              "city_states": [f"{c}, {st}"] if c else [st], "dedupe_key": f"sheet:{n}"}
    app = [sheet("BlackJack Classic", "Las Vegas", "NV"), sheet("Gold Cup", "Frederica", "DE"), sheet("Winter Classic", None, "NV")]
    rows = _run(tmp_path, app, [org("BlackJack Classic", D(40), "Henderson", "NV", organizer="Adrenaline"),
                                org("Gold Cup", D(40), "Frederica", "DE", organizer="Adrenaline"),
                                org("Winter Classic", D(40), "Henderson", "NV", organizer="Adrenaline")])
    assert rows["Gold Cup"]["status"] == "CONFIRMED"
    assert rows["BlackJack Classic"]["status"] == "PLACE DIFFERS"
    assert rows["BlackJack Classic"]["badge"] == "🟠 Place differs · organizer says Henderson, NV"
    assert rows["Winter Classic"]["status"] == "DATE CONFIRMED"


def test_venue_comparison_only_for_real_venues():
    assert L.same_venue("Iron Peak Sports & Events", "Iron Peak Sports") is True      # '&' inside one venue's name
    assert L.same_venue("Aurora Sports Park", "Long Lake Regional Park") is False
    assert L.same_venue("Sandhill Fields", "DE and PA") is None                        # a list of states is not a venue
    assert L.same_venue(None, "Iron Peak Sports & Events") is None
    assert L.same_venue("Multiple Locations", "Farmington Sports Arena") is None
    assert L._city_state({"city": "NJ (venue TBC)", "city_states": ["NJ (venue TBC)"]}) == (None, "NJ")


def test_check_data_only_for_misleading_flags():
    assert not L.needs_check(["gender unknown"], "CONFIRMED")
    assert not L.needs_check(["no state"], "CONFIRMED")                    # place matched the app's row
    assert L.needs_check(["no state", "no town or venue"], "NEW")           # nothing to place it with
    assert L.needs_check(["date corrected by the scraper: site prints ..."], "CONFIRMED")
    assert L.needs_check(["13 days long (league or camp?)"], "DATE CONFLICT")


def test_organizer_sites_meet_the_apps_sites(tmp_path):
    sheet = lambda n, v, c: {"source": "manual-lacrosse-sheet", "sport": "lacrosse", "status": "new", "name": n, "start_date": D(50),
                             "end_date": D(50), "organizer_name": "MLT", "venue_name": v, "city": c, "state": "NY",
                             "city_states": [f"{c}, NY"], "dedupe_key": f"sheet:{n}"}
    app = [sheet("Lax Fest – Site 1", "Farmingdale State College", "Farmingdale"), sheet("Lax Fest – Site 2", "Destination KP", "Kings Park")]
    site = lambda k, v, c: {**org(f"Lax Fest – Site {k}", D(50), c, "NY", organizer="MLT", venue=v), "source_event_id": f"lf-{k}"}
    rows = _run(tmp_path, app, [site(1, "Farmingdale State College", "Farmingdale"), site(2, "Destination KP", "Kings Park")])
    assert rows["Lax Fest – Site 1"]["app_event"] == "Lax Fest – Site 1" and rows["Lax Fest – Site 1"]["status"] == "CONFIRMED"
    assert rows["Lax Fest – Site 2"]["app_event"] == "Lax Fest – Site 2" and rows["Lax Fest – Site 2"]["status"] == "CONFIRMED"


def test_an_extra_organizer_site_is_a_new_site(tmp_path):
    app = [ph("The Ivy 2026", D(70), "New Haven", "CT", "h9", venue="Yale University")]
    site = lambda k, v, c: {**org(f"The Ivy – Site {k}", D(70), c, "CT", organizer="Trilogy", venue=v), "source_event_id": f"ivy-{k}"}
    rows = _run(tmp_path, app, [site(1, "Yale University", "New Haven"), site(2, "Quinnipiac University", "Hamden")])
    assert rows["The Ivy – Site 1"]["status"] == "CONFIRMED"
    assert rows["The Ivy – Site 2"]["status"] == "NEW SITE" and rows["The Ivy – Site 2"]["badge"].startswith("🔵 New site")


def test_same_organizer_alone_is_not_a_match_but_same_words_are(tmp_path):
    app = [ph("APEX GRIND BLUEPRINT", D(95), "Conshohocken", "PA", "g1"), ph("APEX FALL PRELIM 2026", D(20), "Conshohocken", "PA", "g2")]
    rows = _run(tmp_path, app, [org("Boys Apex Summer Invitational High School", D(90), "Newark", "DE", organizer="APEX"),
                                org("Girls Fall Prelim", D(20), "Conshohocken", "PA", organizer="APEX")])
    assert rows["Boys Apex Summer Invitational High School"]["status"] == "NEW"
    assert rows["Girls Fall Prelim"]["status"] == "CONFIRMED"
