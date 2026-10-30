"""Offline checks for tools/web_confirm.py: reading an event's dates off its page, and refusing last season's page."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import web_confirm as W  # noqa: E402


def test_dates_nearest_the_event_not_deadlines_or_other_events():
    page = ("EVENTS RUN BY Inside Lacrosse November 2026 Event Date Location 2026 IL Fall Derby Inside Lacrosse Nov 7 - 8, 2026 Elkridge "
            "2026 IL Fall Cup - Saturday Only Inside Lacrosse Nov 14 - 14, 2026 Marlboro, Maryland")
    assert W.event_dates(page, "2026 IL Fall Cup", dt.date(2026, 11, 14)) == (dt.date(2026, 11, 14), dt.date(2026, 11, 14))
    assert W.event_dates("A $10 late fee applies to all registrations after November 1.", "NOVA Fall Brawl", dt.date(2026, 11, 21)) is None
    assert W.event_dates("Orlando Open December 5 and 6, 2026 - 2026 Family Planner - updated October 28, 2026", "2025 Orlando Open",
                         dt.date(2026, 12, 5)) == (dt.date(2026, 12, 5), dt.date(2026, 12, 6))
    assert W.event_dates("FALL HIGH SCHOOL INVITATIONAL Event Date: November 14th + 15th, 2026", "Fall High School Invitational",
                         dt.date(2026, 11, 7)) == (dt.date(2026, 11, 14), dt.date(2026, 11, 15))


def test_a_date_written_before_its_name_in_a_season_list():
    page = ("December 19-20: Jingle Brawl , Tamarac, FL December 29-31: The Holiday Bowl, Fort Lauderdale, FL 2027 Tournaments "
            "January 15-17: Florida Lacrosse Classic , Weston, FL")
    assert W.event_dates(page, "The 2025 Holiday Bowl", dt.date(2026, 12, 28)) == (dt.date(2026, 12, 29), dt.date(2026, 12, 31))


def test_last_seasons_page_confirms_nothing():
    feb27 = dt.date(2027, 2, 6)
    old = "Boston Strong Tournament @Taunton Forekicks Saturday, February 7th 2026 Divisions (2031/2032) Sunday, February 8th"
    assert not W.current_page(old, feb27, "Boston Strong 2026")
    assert not W.current_page("Longhorn Shootout Dec. 3rd.4th Posted on September 06, 2022", dt.date(2026, 12, 5), "Longhorn Shootout")
    assert W.current_page("TURKEY SHOOTOUT 2026 Saturday November 21st & Sunday November 22nd", dt.date(2026, 11, 21), "Turkey Shootout")
    assert W.current_page("2026 Tournament Information Two-Day Event: November 7-8.", dt.date(2026, 11, 7), "Colonial Challenge")
    assert W.current_page("Best in Box Feb 14, 2027, 9:00 AM", dt.date(2027, 1, 2), "Best in Box")


def test_badges_for_found_events():
    row = {"dedupe_key": "recurrence:x", "name": "Airstation Shootout", "start_date": "2027-01-23", "end_date": "2027-01-24"}
    moved = W.badge_record(row, {"url": "https://www.laxocevents.com/airstation-shootout", "status": "date_moved",
                                 "found_start": "2027-01-30", "found_end": "2027-01-31", "query": "q"}, "now")
    assert moved["badge"] == "🔴 Date corrected · app guessed Jan 23–Jan 24, event website says Jan 30–Jan 31"
    assert moved["status"] == "date_moved" and moved["organizer_url"].endswith("airstation-shootout")
    assert W.badge_record(row, {"url": "https://x.test/", "status": "site_old", "query": "q"}, "now") is None   # stays grey
    assert W.badge_record(row, {"url": None, "status": "not_found", "query": "q"}, "now") is None


def test_only_the_events_own_page_may_correct_a_date():
    assert W.own_page("https://www.laxocevents.com/airstation-shootout", "AirStation Shootout | Lax OC Events", "26 Airstation Shootout")
    assert not W.own_page("https://www.buku.events/", "Buku Events", "NW Lax Events San Diego")
    assert not W.own_page("https://www.masselite.com/team/schedules/tournament-schedule", "Schedule", "Northeast Fall Championships")
