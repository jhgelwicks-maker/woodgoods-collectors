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
SAT = 35 + (5 - TODAY.weekday()) % 7          # days to a Saturday 5-6 weeks out, so 'Saturday'/'Sunday' rows share a weekend


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
    assert rows["Bama Bash"]["badge"].startswith("🔴 Date corrected · app guessed ")
    assert rows["Bama Bash"]["badge"].endswith("organizer says " + L.fmt_dates(D(200), D(200)))
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
    assert rows["BlackJack Classic"]["badge"] == "🟠 Place corrected · sheet had Las Vegas, NV, organizer says Henderson, NV"
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


def test_organizer_rows_already_in_the_app_are_not_matched_against_themselves(tmp_path):
    own = {**org("Victory Tide", D(60), "Little River", "SC"), "source": "wg-victory", "status": "new", "dedupe_key": "wg-victory:victory-tide"}
    rows = _run(tmp_path, [own], [org("Victory Tide", D(60), "Little River", "SC")])
    assert rows["Victory Tide"]["status"] == "NEW"


def test_badge_records_pair_new_and_one_per_event(tmp_path):
    app = [ph("Empire of the Sun 2025", D(30), "Panama City Beach", "FL", "h2"),
           {**org("Victory Tide", D(60), "Little River", "SC"), "source": "wg-victory", "status": "new",
            "dedupe_key": "wg-victory:victory-tide"},
           {**org("Empire of The Sun", D(30), "Panama City Beach", "FL"), "source": "wg-victory", "status": "new",
            "dedupe_key": "wg-victory:empire-of-the-sun"}]
    scraped = [{**org("Empire of The Sun", D(30), "Panama City Beach", "FL"), "source": "wg-victory", "source_event_id": "empire-of-the-sun"},
               {**org("Victory Tide", D(60), "Little River", "SC"), "source": "wg-victory", "source_event_id": "victory-tide"}]
    _run(tmp_path, app, scraped)
    recs = {r["app_dedupe_key"]: r for r in json.load(open(tmp_path / "report.checks.json"))}
    assert recs["recurrence:h2"]["status"] == "confirmed" and recs["recurrence:h2"]["organizer_dedupe_key"] == "wg-victory:empire-of-the-sun"
    assert recs["wg-victory:empire-of-the-sun"]["status"] == "confirmed"                 # both copies green
    assert recs["wg-victory:empire-of-the-sun"]["matched_dedupe_key"] == "recurrence:h2"
    assert recs["wg-victory:victory-tide"]["status"] == "new" and recs["wg-victory:victory-tide"]["badge"].startswith("🔵 New")
    assert len(recs) == 3


def test_best_status_wins_and_sending_posts_batches(monkeypatch):
    recs = [{"app_dedupe_key": "x", "status": "review"}, {"app_dedupe_key": "x", "status": "confirmed"}, {"app_dedupe_key": "y", "status": "new"}]
    assert {r["app_dedupe_key"]: r["status"] for r in L.best_per_event(recs)} == {"x": "confirmed", "y": "new"}
    sent = []
    class Resp:
        def __init__(self, body): self.body = body
        def read(self): return json.dumps({"saved": len(self.body["confirmations"]), "skipped": 0, "errors": []}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=0, context=None):
        body = json.loads(req.data); sent.append((req.full_url, req.headers.get("X-ingest-key"), body["source"], len(body["confirmations"])))
        return Resp(body)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("INGEST_URL", "https://app.test/api/public/ingest"); monkeypatch.setenv("INGEST_KEY", "test-key")
    L.send_checks([{"app_dedupe_key": str(i), "status": "confirmed"} for i in range(120)], size=50)
    assert [s[3] for s in sent] == [50, 50, 20] and all(s[2] == "organizer-check" and s[1] == "test-key" for s in sent)


def test_organizer_row_is_green_and_both_halves_of_an_event_get_the_badge(tmp_path):
    hist = (TODAY + dt.timedelta(days=SAT - 364)).isoformat()
    sat = ph("Veterans Day Classic: SATURDAY 2031-2035/6", D(SAT - 7), "West Windsor", "NJ", "hs")
    sun = ph("Veterans Day Classic: SUNDAY 2027-2030", D(SAT - 6), "West Windsor", "NJ", "hu")
    own = {**org("Veterans Day Classic", D(SAT), "West Windsor", "NJ", organizer="Trilogy"), "source": "wg-trilogy",
           "status": "new", "dedupe_key": "wg-trilogy:vdc"}
    scraped = [{**org("Veterans Day Classic", D(SAT), "West Windsor", "NJ", organizer="Trilogy"), "source": "wg-trilogy",
                "source_event_id": "vdc"}]
    _run(tmp_path, [sat, sun, own], scraped, history=[{"id": "hs", "start_date": hist}, {"id": "hu", "start_date": hist}])
    recs = {r["app_dedupe_key"]: r for r in json.load(open(tmp_path / "report.checks.json"))}
    assert recs["recurrence:hs"]["status"] == recs["recurrence:hu"]["status"] == "date_moved"
    assert recs["recurrence:hs"]["badge"].startswith("🔴 Date corrected · app guessed")
    assert recs["wg-trilogy:vdc"]["status"] == "confirmed" and recs["wg-trilogy:vdc"]["badge"] == "🟢 Confirmed · organizer website"
    assert "corrects the app's date" in recs["wg-trilogy:vdc"]["badge_description"]


def _hist(hid, name, days_ago, teams, venues):
    start = (TODAY - dt.timedelta(days=days_ago)).isoformat()
    return {"id": hid, "name": name, "start_date": start, "end_date": start, "team_count": teams,
            "listing_url": f"https://tm.test/{hid}", "venues": venues}


def _tmv(name, address, teams, fields, day, end="17:30"):
    return {"venue_name": name, "address": address, "teams": teams, "games": teams * 2, "fields": fields,
            "field_count": len(fields), "schedule": [{"date": day, "games": teams * 2, "hours": 9.5, "fields_active": len(fields),
                                                      "fields_active_pm": len(fields), "fields_active_late": len(fields), "est_end": end}]}


def test_organizer_sites_take_over_last_years_records_per_venue(tmp_path):
    # Veterans Day Classic 2025 was two Tourney Machine tournaments (Saturday, Sunday) sharing Mercer County Park; this year
    # the organizer lists three sites, one of them new and without Iron Peak
    sat_day, sun_day = (TODAY - dt.timedelta(days=324)).isoformat(), (TODAY - dt.timedelta(days=323)).isoformat()
    hs = _hist("hs", "Veterans Day Classic: SATURDAY 2031-2035/6", 324, 144, [
        _tmv("Mercer County Park", "334 South Post Road, West Windsor Township, NJ 08550", 64, ["MCP - Field 1", "MCP - Field 14 (Turf)"], sat_day),
        _tmv("Iron Peak Sports & Events", "137 Mountain View Rd, Hillsborough, NJ", 34, ["IP - Field 15"], sat_day),
        _tmv("Bucks Mill Park", "137 Bucks Mill Rd, Colts Neck, NJ 07722", 36, ["BM - Field 1"], sat_day)])
    hu = _hist("hu", "Veterans Day Classic: SUNDAY 2026-2030", 323, 106, [
        _tmv("Mercer County Park", "334 South Post Road, West Windsor Township, NJ 08550", 70, ["MCP - Field 1", "MCP - Field 14"], sun_day),
        _tmv("Iron Peak Sports & Events", "137 Mountain View Rd, Hillsborough, NJ", 36, ["IP - Field 15"], sun_day)])
    older = _hist("h24", "Veterans Day Classic: SUNDAY", 688, 96, [])
    sat = ph("Veterans Day Classic: SATURDAY 2031-2035/6", D(SAT), "West Windsor", "NJ", "hs")
    sun = {**ph("Veterans Day Classic: SUNDAY 2026-2030", D(SAT + 1), "West Windsor", "NJ", "hu"), "history_ids": ["hu", "h24"]}
    def site(n, venue, city, address=None):
        r = {**org(f"Veterans Day Classic – Site {n}", D(SAT), city, "NJ", organizer="Trilogy", venue=venue),
             "source": "wg-trilogy", "source_event_id": f"vdc-{n}"}
        r["raw"]["address"] = address
        return r
    scraped = [site(1, "Mercer County Park & Community College", "West Windsor", "334 South Post Road West Windsor NJ 08550"),
               site(2, "Bucks Mill Park", "Colts Neck"), site(3, "St. Joseph High School", "Metuchen")]
    own = [{**r, "status": "new", "dedupe_key": f"wg-trilogy:{r['source_event_id']}"} for r in scraped]
    rows = _run(tmp_path, [sat, sun] + own, scraped, history=[hs, hu, older])
    recs = {r["app_dedupe_key"]: r for r in json.load(open(tmp_path / "report.checks.json"))}
    s1, s2, s3 = (recs[f"wg-trilogy:vdc-{n}"] for n in (1, 2, 3))
    for s in (s1, s2, s3):
        assert sorted(s["replaces"]) == ["recurrence:hs", "recurrence:hu"]
        assert sorted(s["history_ids"]) == ["hs", "hu"] and s["earlier_history_ids"] == ["h24"]
        assert s["last_year_event"]["teams"] == 250 and s["last_year_event"]["venues_not_listed_this_year"] == ["Iron Peak Sports & Events"]
    ly = s1["last_year_venue"]                       # both days added up; 'Field 14 (Turf)' and 'Field 14' are one field
    assert (ly["teams"], ly["field_count"], [d["date"] for d in ly["schedule"]]) == (134, 2, [sat_day, sun_day])
    assert [x["teams_here"] for x in ly["from"]] == [64, 70]
    assert s2["last_year_venue"]["teams"] == 36 and len(s2["last_year_venue"]["from"]) == 1
    assert ly["schedule_quality"] == 100 and s2["last_year_venue"]["schedule_quality"] == 100     # the event formula, per venue
    assert L.venue_schedule_quality([{"date": "2025-11-08"}]) is None                            # old records without numbers
    assert s3["last_year_venue"] is None and s3["last_year_venue_status"] == "new venue this year"
    assert (s1["primary_site"], s2["primary_site"], s3["primary_site"]) == (True, False, False)
    assert recs["recurrence:hs"]["replaced_by"] == recs["recurrence:hu"]["replaced_by"] == "wg-trilogy:vdc-1"
    assert rows["Veterans Day Classic – Site 1"]["last_year_here"].startswith(f"{ly['year']}: 134 teams, 2 fields at Mercer County Park (")
    assert rows["Veterans Day Classic – Site 3"]["last_year_here"] == "new venue this year"


def test_short_tourney_machine_venue_names_and_honest_venue_status():
    assert L.same_venue_loose("HBSC", "Huntingdon Beach Sports Complex") and L.same_venue_loose("OCGP", "OC Great Park")
    assert L.same_venue_loose("URI", "University of Rhode Island") and L.same_venue_loose("Hoover Met Complex", "Hoover Metropolitan Complex")
    assert not L.same_venue_loose("NJ", "New Jersey") and not L.same_venue_loose("Bucks Mill Park", "Blue Sky Park")
    recs = [{"start_date": "2025-11-08", "venues": [{"venue_name": "UMass Amherst", "address": "131 Commonwealth Ave, Amherst, MA 01003"}]}]
    assert L.venue_status({"city": "Amherst", "state": "MA", "raw": {}}, None, recs) == "organizer gives no venue"
    assert L.venue_status({"city": "Amherst", "state": "MA", "venue_name": "Amherst Regional HS", "raw": {}}, None, recs) == \
        "same town as last year, venue name differs"
    assert L.venue_status({"city": "Amherst", "state": "MA", "venue_name": "X Park", "raw": {}}, None,
                          [{"start_date": "2025-11-08", "venues": []}]) == "last year's venues unknown"


def test_a_possible_match_never_replaces_anything():
    scr = [{"source": "wg-x", "source_event_id": "a", "venue_name": "P", "raw": {}}]
    app = [{"_kind": "placeholder", "dedupe_key": "recurrence:h", "raw": {"history_id": "h"}}]
    assert L.event_links(scr, app, {"g": [0]}, {0: [(0, "REVIEW")]}, {"h": {"id": "h", "start_date": "2025-01-01"}}) == ({}, {}, {})


def test_send_event_sends_every_record_of_one_event(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(L, "send_checks", lambda batch: sent.extend(batch))
    sat = ph("Veterans Day Classic: SATURDAY", D(40), "West Windsor", "NJ", "hs")
    other = ph("Empire of the Sun", D(30), "Panama City Beach", "FL", "h2")
    scraped = [{**org(f"Veterans Day Classic – Site {n}", D(40), "West Windsor", "NJ", organizer="Trilogy"),
                "source": "wg-trilogy", "source_event_id": f"vdc-{n}"} for n in (1, 2)] + \
              [{**org("Empire of The Sun", D(30), "Panama City Beach", "FL"), "source": "wg-victory", "source_event_id": "eots"}]
    own = [{**r, "status": "new", "dedupe_key": f"{r['source']}:{r['source_event_id']}"} for r in scraped]
    exp = tmp_path / "export"; exp.mkdir()
    _write(exp / "event_candidates.jsonl", [sat, other] + own); _write(exp / "event_history.jsonl", []); _write(exp / "venues.jsonl", [])
    _write(tmp_path / "s.jsonl", scraped)
    L.main(["--export-dir", str(exp), "--scraped", str(tmp_path / "s.jsonl"), "--out", str(tmp_path / "r.csv"),
            "--send-event", "veterans day classic"])
    assert sorted(c["app_dedupe_key"] for c in sent) == ["recurrence:hs", "wg-trilogy:vdc-1", "wg-trilogy:vdc-2"]


def test_old_copies_of_organizer_rows_point_at_their_current_row(tmp_path):
    def pt(name, start, city, venue, sid, src="wg-primetime"):
        return {**org(name, start, city, "MA", organizer="PrimeTime", venue=venue), "source": src, "source_event_id": sid}
    scraped = [pt("Fall Classic (Boys) – Site 1", D(5), "Smithfield", "Bryant University", "fc-bryant"),
               pt("Fall Classic (Boys) – Site 2", D(5), "Northborough", "NEBC", "fc-nebc"),
               pt("Fall Classic (Boys) – Site 3", D(5), "Fitchburg", "Game On", "fc-gameon"),
               pt("Summer Kickoff – Site 1", D(60), "Baltimore", "Calvert Hall", "sk-calvert")]
    def in_app(r): return {**r, "status": "new", "dedupe_key": f"{r['source']}:{r['source_event_id']}"}
    app = [in_app(r) for r in scraped[:3]] + [                       # Summer Kickoff's current row isn't in the app yet
        in_app(pt("Fall Classic (Boys) – Site 1", D(5), "Northborough", None, "fc-northborough")),
        in_app(pt("Fall Classic (Boys) – Site 3", D(5), "Smithfield", None, "fc-smithfield")),
        in_app(pt("Fall Classic (Boys)", D(5), "Worcester", None, "fc-worcester")),             # town of no site: first site
        in_app(pt("Summer Kickoff", D(60), "Baltimore", None, "sk-baltimore")),                # wait for its current row
        in_app(pt("Road to the Challenge", D(30), "Dover", None, "road")),                      # gone from the site
        in_app(pt("Spring Thaw", D(-30), "Dover", None, "thaw")),                               # past: left alone
        in_app(pt("Other Org Event", D(30), "Dover", None, "x", src="wg-hogans"))]              # organizer not scraped
    rows = _run(tmp_path, app, scraped)
    recs = {r["app_dedupe_key"]: r for r in json.load(open(tmp_path / "report.checks.json"))}
    assert recs["wg-primetime:fc-northborough"]["replaced_by"] == "wg-primetime:fc-nebc"
    assert recs["wg-primetime:fc-smithfield"]["replaced_by"] == "wg-primetime:fc-bryant"
    assert recs["wg-primetime:fc-worcester"]["replaced_by"] == "wg-primetime:fc-bryant"
    assert recs["wg-primetime:fc-northborough"]["status"] == "superseded"
    assert recs["wg-primetime:road"]["retire"] is True and recs["wg-primetime:road"]["status"] == "not_on_organizer_site"
    for k in ("wg-primetime:sk-baltimore", "wg-primetime:thaw", "wg-hogans:x"):
        assert k not in recs
    assert rows["Road to the Challenge"]["status"] == "GONE FROM ORGANIZER SITE"


def test_placeholders_we_hid_still_match_on_the_next_run(tmp_path):
    own = {**org("Empire of The Sun", D(30), "Panama City Beach", "FL"), "source": "wg-victory", "source_event_id": "eots",
           "status": "new", "dedupe_key": "wg-victory:eots", "id": "org-row"}
    hidden = {**ph("Empire of the Sun 2025", D(30), "Panama City Beach", "FL", "h2"), "status": "duplicate", "duplicate_of": "org-row"}
    other = {**ph("Victory Tide", D(60), "Little River", "SC", "h3"), "status": "duplicate", "duplicate_of": "some-tm-row"}
    rows = _run(tmp_path, [own, hidden, other, {"id": "some-tm-row", "source": "tourneymachine", "sport": "lacrosse"}],
                [{k: v for k, v in own.items() if k not in ("status", "dedupe_key", "id")},
                 {**org("Victory Tide", D(60), "Little River", "SC"), "source": "wg-victory", "source_event_id": "tide"}])
    assert rows["Empire of The Sun"]["status"] == "CONFIRMED"           # hidden by our record: still the match
    assert rows["Victory Tide"]["status"] == "NEW"                      # hidden by the app for another reason: not used


def test_sheet_rows_are_replaced_by_the_organizer_site_in_their_town(tmp_path):
    def pt(n, city, venue, sid):
        return {**org(f"Fall Classic (Boys) – Site {n}", D(SAT), city, "MA", organizer="PrimeTime", venue=venue),
                "source": "wg-primetime", "source_event_id": sid}
    scraped = [pt(1, "Smithfield", "Bryant University", "bryant"), pt(2, "Northborough", "NEBC", "nebc"), pt(3, "Fitchburg", "Game On", "gameon")]
    for r in scraped: r["state"] = "RI" if r["city"] == "Smithfield" else "MA"; r["city_states"] = [f"{r['city']}, {r['state']}"]
    def sheet(n, city):
        return {"source": "manual-lacrosse-sheet", "sport": "lacrosse", "status": "new", "name": f"Fall Classic (Boys) – Site {n}",
                "start_date": D(SAT), "end_date": D(SAT + 1), "organizer_name": "PrimeTime", "city": city, "state": "MA",
                "city_states": [f"{city}, MA"], "dedupe_key": f"manual-lacrosse-sheet:fc-{n}"}
    own = [{**r, "status": "new", "dedupe_key": f"wg-primetime:{r['source_event_id']}"} for r in scraped]
    _run(tmp_path, [sheet(1, "Northborough"), sheet(2, "Fitchburg")] + own, scraped)
    recs = {r["app_dedupe_key"]: r for r in json.load(open(tmp_path / "report.checks.json"))}
    assert recs["manual-lacrosse-sheet:fc-1"]["replaced_by"] == "wg-primetime:nebc"
    assert recs["manual-lacrosse-sheet:fc-2"]["replaced_by"] == "wg-primetime:gameon"
    for k in ("bryant", "nebc", "gameon"):
        assert sorted(recs[f"wg-primetime:{k}"]["replaces"]) == ["manual-lacrosse-sheet:fc-1", "manual-lacrosse-sheet:fc-2"]
        assert recs[f"wg-primetime:{k}"]["same_event_keys"] == []


def test_a_sheet_row_stays_when_the_organizer_says_less_about_the_place():
    assert L.placed({"venue_name": "NEBC", "city": "Northborough", "state": "MA"})
    assert L.placed({"venue_name": None, "city": "Kingston", "state": "RI"})
    assert not L.placed({"venue_name": "DE and PA", "city": None, "state": "DE"})
    assert not L.placed({"venue_name": "Multiple Locations", "city": None, "state": "CT"})


def test_a_partial_scrape_retires_nothing_as_gone(tmp_path):
    def pt(name, sid):
        return {**org(name, D(30), "Dover", "DE", organizer="ML8"), "source": "wg-ml8", "source_event_id": sid}
    scraped = [pt("Summer Challenge", "a")]                                  # 1 of the 4 events the app shows: a failed page
    app = [{**r, "status": "new", "dedupe_key": f"wg-ml8:{r['source_event_id']}"} for r in
           [pt("Summer Challenge", "a"), pt("Spotlight", "b"), pt("D3 Showcase", "c"), pt("Summer Nationals", "d")]]
    _run(tmp_path, app, scraped)
    recs = {r["app_dedupe_key"] for r in json.load(open(tmp_path / "report.checks.json"))}
    assert not recs & {"wg-ml8:b", "wg-ml8:c", "wg-ml8:d"}


def test_records_not_applied_get_one_more_try_alone(monkeypatch):
    calls = []
    class Resp:
        def __init__(self, body): self.body = body
        def read(self):
            keys = [c["app_dedupe_key"] for c in self.body["confirmations"]]
            bad = [k for k in keys if k == "slow"] if len(keys) > 1 else []
            return json.dumps({"saved": len(keys), "applied": len(keys) - len(bad), "errors": [f"{k}: apply failed: timeout" for k in bad]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=0, context=None):
        body = json.loads(req.data); calls.append(len(body["confirmations"])); return Resp(body)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("INGEST_URL", "https://app.test/api/public/ingest"); monkeypatch.setenv("INGEST_KEY", "test-key")
    res = L.send_checks([{"app_dedupe_key": k, "status": "confirmed"} for k in ("a", "slow", "b")], size=10)
    assert calls == [3, 1] and res["applied"] == 3 and res["failed"] == []


def test_a_live_tourney_machine_listing_stays_and_takes_the_organizer_details(tmp_path):
    tm = {"source": "tourneymachine", "sport": "lacrosse", "status": "new", "name": "2026 Liberty National Elite Fall Classic",
          "start_date": D(SAT), "end_date": D(SAT + 1), "city": "Aston", "state": "PA", "city_states": ["Aston, PA"],
          "venue_name": "MSI Sports Complex", "venues": [{"venue_name": "MSI Sports Complex", "teams": 28}],
          "dedupe_key": "tourneymachine:h1", "organizer_name": "Victory Event Series"}
    sheet = {"source": "manual-lacrosse-sheet", "sport": "lacrosse", "status": "new", "name": "Liberty National Elite Fall Classic",
             "start_date": D(SAT), "end_date": D(SAT + 1), "organizer_name": "Victory", "city": "Aston", "state": "PA",
             "city_states": ["Aston, PA"], "dedupe_key": "manual-lacrosse-sheet:lnefc"}
    o = {**org("Liberty National Elite Fall Classic", D(SAT), "Aston", "PA", venue="MSI Sports Complex"),
         "source": "wg-victory", "source_event_id": "lnefc", "vendor_contact_email": "vendors@victoryeventseries.com",
         "age_groups": "2027–2030"}
    o["raw"].update(address="4881 Chichester Avenue, Aston, PA 19014",
                    contacts=[{"name": None, "email": "vendors@victoryeventseries.com", "phone": None, "role": "vendor"}])
    own = {**o, "status": "new", "dedupe_key": "wg-victory:lnefc"}
    _run(tmp_path, [tm, sheet, own], [o])
    recs = {r["app_dedupe_key"]: r for r in json.load(open(tmp_path / "report.checks.json"))}
    assert recs["wg-victory:lnefc"]["replaced_by"] == "tourneymachine:h1"             # the organizer row gives way
    assert recs["manual-lacrosse-sheet:lnefc"]["replaced_by"] == "tourneymachine:h1"  # and the sheet row points at TM too
    t = recs["tourneymachine:h1"]
    assert t["status"] == "confirmed" and t["takes_over"] == "wg-victory:lnefc"
    assert (t["organizer_url"], t["organizer_address"], t["organizer_age_divisions"]) == (
        "https://org.test/e", "4881 Chichester Avenue, Aston, PA 19014", "2027–2030")
    assert t["organizer_vendor_contact"]["email"] == "vendors@victoryeventseries.com" and len(t["organizer_contacts"]) == 1
    assert "manual-lacrosse-sheet:lnefc" in t["replaces"]


def test_rows_no_organizer_site_confirms_get_a_grey_badge(tmp_path):
    unseen = ph("Tidewater Shootout", D(50), "Norfolk", "VA", "h9")                       # an organizer we don't scrape
    ours = {**ph("Victory Spring Splash", D(80), "Ocean City", "MD", "h8"), "organizer_name": "Victory"}   # scraped, not listed yet
    tm = {"source": "tourneymachine", "sport": "lacrosse", "status": "new", "name": "Lehigh Valley Lax Fest", "start_date": D(60),
          "end_date": D(60), "city": "Allentown", "state": "PA", "dedupe_key": "tourneymachine:h7", "organizer_name": "LV Lax"}
    scraped = [{**org("Empire of The Sun", D(30), "Panama City Beach", "FL"), "source": "wg-victory", "source_event_id": "eots"}]
    _run(tmp_path, [unseen, ours, tm], scraped, history=[{"id": "h9", "start_date": (TODAY - dt.timedelta(days=314)).isoformat()}])
    recs = {r["app_dedupe_key"]: r for r in json.load(open(tmp_path / "report.checks.json"))}
    assert recs["recurrence:h9"]["badge"] == "⚪ Predicted from last year · not checked" and recs["recurrence:h9"]["status"] == "unchecked"
    assert "last year's Tourney Machine event" in recs["recurrence:h9"]["badge_description"]
    assert recs["recurrence:h8"]["badge"] == "⚪ Predicted from last year · not on organizer site yet"
    assert recs["tourneymachine:h7"]["badge"] == "⚪ Tourney Machine listing · not checked"
