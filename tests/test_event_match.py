"""Real cases from event_history / event_candidates (Oct 2026). Each one is a mistake the matcher must not make.
Run: python -m pytest tests/test_event_match.py -q   (or: python tests/test_event_match.py)"""
import datetime as dt, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.event_match import Event, score

D = dt.date.fromisoformat
OAKTON = [(38.8788, -77.3070)]; BELL_BRANCH = [(39.0396, -76.6640)]; CANANDAIGUA = [(42.8867, -77.2814)]
MITCHEL = [(40.7277, -73.5865)]; OC_GREAT_PARK = [(33.6705, -117.7450)]; SB_POLO = [(34.4300, -119.5990)]
MASON = [(39.3600, -84.3100)]

def ev(name, date, state, pts=None, org=None, sites=(), teams=None, gender=None, ns=1):
    return Event(name=name, start=D(date), state=state, organizer=org, points=pts or [], sites=set(sites),
                 teams=teams, gender=gender, name_series=ns)

# (past edition, current listing, expected decision, why)
CASES = [
    # --- must MATCH -------------------------------------------------------------------------
    (ev("Fall Brawl 2025", "2026-11-01", "MD", BELL_BRANCH, sites={"bb"}, teams=87, ns=3),
     ev("Fall Brawl – Site 2", "2026-11-08", "MD", BELL_BRANCH, org="Hogan's", sites={"bb"}),
     {"confirmed", "review"}, "generic name, but same facility a week later"),
    (ev("PT Fall Challenge - 2025", "2026-10-17", "CT", [(41.24, -73.52)], sites={"pt"}),
     ev("Prime Time Fall Challenge", "2026-10-17", "CT", [(41.24, -73.52)], org="PTlacrosse", sites={"pt"}),
     {"confirmed"}, "abbreviation PT = Prime Time, same place and day"),
    (ev("2025 IL Fall Cup", "2026-11-07", "NY", [(40.80, -73.20)], sites={"il"}),
     ev("IL Youth Fall Cup", "2026-11-14", "NY", [(40.80, -73.20)], org="MLT", sites={"il"}),
     {"confirmed"}, "same distinctive words, same facility, one week later"),
    (ev("Capital Box Lacrosse Classic 2026", "2027-02-13", "MD", [(39.0, -76.9)], sites={"cap"}),
     ev("Capital Box Lacrosse Classic (BOX lacrosse)", "2027-02-06", "MD", [(39.0, -76.9)], org="MADLAX", sites={"cap"}),
     {"confirmed"}, "same name, same facility"),
    (ev("2024 Canandaigua Fall Brawl", "2025-10-05", "NY", CANANDAIGUA, teams=51, ns=1),
     ev("2025 Fall Brawl", "2025-10-05", "NY", [(42.8860, -77.2800)], teams=39, ns=3),
     {"confirmed", "review"}, "renamed (town dropped), same fields, same weekend"),
    (ev("Mason Lacrosse Club Fall Brawl 2024", "2025-11-02", "OH", MASON, teams=34),
     ev("Mason Fall Brawl", "2025-11-09", "OH", [(39.3550, -84.3150)], teams=33),
     {"confirmed", "review"}, "shortened name, nearby venue"),
    (ev("Octoberfest 2025", "2026-10-24", "NY", MITCHEL, sites={"mf"}, teams=93, gender="boys"),
     ev("Octoberfest", "2026-10-31", "NY", MITCHEL, org="Tristar Lacrosse", sites={"mf"}, gender="boys"),
     {"confirmed"}, "moved one week (Halloween Saturday)"),
    # --- must NOT match ---------------------------------------------------------------------
    (ev("APEX GIRLS SOPHOMORE SELECT 2025", "2026-11-01", "NJ", [(40.5, -74.6)], sites={"ip"}),
     ev("APEX TRI-STATE INVITE 2026", "2026-10-31", "NJ", [(40.5, -74.6)], org="APEX Lacrosse Events", sites={"ip"}),
     {"no_match"}, "same organizer word, same facility, different event"),
    (ev("APEX GIRLS SOPHOMORE SELECT 2025", "2026-11-01", "NJ", [(40.5, -74.6)], sites={"ip"}),
     ev("Boys Sophomore Select", "2026-10-17", "NJ", [(40.5, -74.6)], org="APEX", sites={"ip"}),
     {"no_match"}, "girls event vs boys event"),
    (ev("NORTH AMERICAN LACROSSE - 2025 FALL YOUTH INVITATIONAL", "2026-11-07", "PA", [(40.0, -75.6)], sites={"x"}),
     ev("Apex Fall Invitational", "2026-11-07", "PA", [(40.0, -75.6)], org="APEX", sites={"x"}),
     {"no_match"}, "same facility and day, different organizers' events"),
    (ev("Town Turkey Shootout", "2026-11-21", "VA", [(37.4, -77.6)], sites={"rc"}),
     ev("Fall Classic – Site 1", "2026-11-21", "VA", [(37.4, -77.6)], org="MLT", sites={"rc"}),
     {"no_match"}, "busy complex, unrelated event"),
    (ev("Polar Bear League Charlotte - Boys", "2026-12-06", "NC", [(35.1, -80.7)], sites={"m"}),
     ev("2026 Charlotte Fall Classic", "2026-10-24", "NC", [(35.1, -80.7)], org="Carolina Lacrosse Tournaments", sites={"m"}),
     {"no_match"}, "shared city word only, six weeks apart"),
    (ev("OC Fall Brawl 2025", "2026-11-14", "CA", OC_GREAT_PARK, teams=91, ns=3),
     ev("Santa Barbara Fall Brawl", "2026-10-24", "CA", SB_POLO, org="Buku", teams=83, ns=3),
     {"no_match"}, "same name ending, different events 150 km apart"),
    (ev("Fall Brawl 2025", "2026-11-01", "MD", BELL_BRANCH, ns=3),
     ev("2025 Fall Brawl", "2026-10-04", "NY", CANANDAIGUA, ns=3),
     {"no_match"}, "same name, different state"),
    (ev("2025 NOVA Fall Brawl - Girls High School - Nov. 29", "2026-11-28", "VA", OAKTON, gender="girls"),
     ev("2026 NOVA Fall Brawl - Boys High School", "2026-11-29", "VA", OAKTON, gender="boys"),
     {"no_match"}, "same series family but the girls edition is not the boys edition"),
]

def test_cases():
    bad = []
    for past, cur, want, why in CASES:
        r = score(past, cur)
        if r["decision"] not in want:
            bad.append(f"{past.name!r} vs {cur.name!r}: got {r['decision']} ({r['score']}) want {want} — {why}\n   {r['reasons']}")
    assert not bad, "\n".join(bad)

if __name__ == "__main__":
    ok = 0
    for past, cur, want, why in CASES:
        r = score(past, cur); good = r["decision"] in want; ok += good
        print(("PASS" if good else "FAIL"), f"{r['decision']:9} {r['score']:>6}  {past.name[:38]:38} | {cur.name[:34]:34} | {why}")
        if not good: print("       ", r["reasons"])
    print(f"\n{ok}/{len(CASES)} passed")
