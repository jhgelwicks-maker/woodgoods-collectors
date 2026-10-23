"""Stage D of Crawl Organizer: what does each crawled event mean for the app?

  duplicate_of   the same event is already in the app from another source (Tourney Machine, an organizer scraper, ...):
                 don't add it again.
  placeholder    the app's "expected" recurrence placeholder this event confirms (same date and place) or moves
                 (new date or venue), or a possible match for a human to review.

Uses collectors.event_match (evidence scoring, two anchors, one-to-one) against a local export of the app's data.
Never decides "not running": an organizer site that lists nothing for a series is reported, not acted on."""
from __future__ import annotations
import json, pathlib, datetime as dt

from ..event_match import Event, score, assign


def _date(v):
    if isinstance(v, dt.date): return v
    try: return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError): return None


def _points(venues):
    out = []
    for v in venues or []:
        try:
            if isinstance(v, dict) and v.get("lat") not in (None, "") and v.get("long") not in (None, ""):
                out.append((float(v["lat"]), float(v["long"])))
        except (TypeError, ValueError):
            continue
    return out


def _state(r):
    st = (r.get("state") or "").split(",")[0].strip().upper()
    if st: return st
    cs = (r.get("city_states") or [""])[0]
    return cs.split(", ")[-1].strip().upper() if ", " in cs else None


def to_event(r):
    raw = r.get("raw") or {}
    g = raw.get("gender")
    return Event(name=r.get("name") or "", start=_date(r.get("start_date")), state=_state(r), organizer=r.get("organizer_name"),
                 sites=set(r.get("venue_ids") or []), points=_points(r.get("venues")),
                 teams=r.get("expected_teams") or r.get("team_count"),
                 gender={"both": "coed"}.get(g, g))


def load_app_rows(export_dir, today=None):
    """Upcoming lacrosse rows from the export: (placeholders, listings)."""
    today = today or dt.date.today()
    ph, ls = [], []
    p = pathlib.Path(export_dir) / "event_candidates.jsonl"
    if not p.exists(): return ph, ls
    for line in p.open():
        r = json.loads(line)
        if not (r.get("sport") or "").lower().startswith("lacrosse") or r.get("status") == "duplicate": continue
        s = _date(r.get("start_date"))
        if not s or s < today - dt.timedelta(days=3): continue
        (ph if r.get("source") == "recurrence" else ls).append(r)
    return ph, ls


def match_events(rows, export_dir, today=None):
    """-> (per-event results, summary). rows are crawl.to_row() dicts."""
    placeholders, listings = load_app_rows(export_dir, today)
    listings = [l for l in listings if not str(l.get("source", "")).startswith("org-")]
    crawled = [to_event(r) for r in rows]
    L = [to_event(l) for l in listings]
    P = [to_event(p) for p in placeholders]

    results = []
    for i, (r, e) in enumerate(zip(rows, crawled)):
        best = None
        for j, le in enumerate(L):
            if not le.start or not e.start or abs((le.start - e.start).days) > 10: continue
            res = score(le, e)
            if res["decision"] != "no_match" and (best is None or res["score"] > best[1]["score"]): best = (j, res)
        out = {"dedupe_key": f"{r['source']}:{r['source_event_id']}", "name": r["name"], "start_date": r["start_date"]}
        if best and best[1]["decision"] == "confirmed":
            out.update(duplicate_of=listings[best[0]]["dedupe_key"], duplicate_score=best[1]["score"], duplicate_reasons=best[1]["reasons"])
        elif best:
            out.update(maybe_duplicate_of=listings[best[0]]["dedupe_key"], duplicate_score=best[1]["score"], duplicate_reasons=best[1]["reasons"])
        results.append(out)

    pairs = {}
    for pi, pe in enumerate(P):
        for ci, ce in enumerate(crawled):
            if pe.start and ce.start and abs((pe.start - ce.start).days) <= 56:
                res = score(pe, ce)
                if res["decision"] != "no_match": pairs[(pi, ci)] = res
    for pi, (cis, res) in assign(P, crawled, pairs).items():
        outcome = ("moved" if res.get("moved") else "confirmed") if res["decision"] == "confirmed" else "review"
        for ci in cis:
            results[ci]["placeholder"] = {"dedupe_key": placeholders[pi]["dedupe_key"], "name": placeholders[pi]["name"],
                                          "start_date": placeholders[pi]["start_date"], "outcome": outcome,
                                          "score": res["score"], "reasons": res["reasons"]}
    n = len(results)
    summary = {"crawled": n, "already_in_app": sum(1 for x in results if x.get("duplicate_of")),
               "maybe_in_app": sum(1 for x in results if x.get("maybe_duplicate_of")),
               "new": sum(1 for x in results if not x.get("duplicate_of")),
               "placeholders_confirmed": sum(1 for x in results if x.get("placeholder", {}).get("outcome") == "confirmed"),
               "placeholders_moved": sum(1 for x in results if x.get("placeholder", {}).get("outcome") == "moved"),
               "placeholders_review": sum(1 for x in results if x.get("placeholder", {}).get("outcome") == "review"),
               "placeholders_in_window": len(placeholders)}
    return results, summary
