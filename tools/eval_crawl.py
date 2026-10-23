"""Accuracy check for Crawl Organizer: run the generic crawler on organizers that already have a dedicated scraper
(adapters/) and compare. The scraper's events are the answer key.

    python tools/eval_crawl.py                 # all 14
    python tools/eval_crawl.py apex nxt        # some

Per organizer: events the scraper found, events the crawler found, how many crawler events match a scraper event
(collectors.event_match, same organizer), and of those how often dates / state / venue agree. Prints totals:
recall = matched / scraper events, precision = matched / crawler events. No contacts are printed."""
import sys, os, time, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors import woodgoods_lacrosse as wl
from collectors.event_match import score
from collectors.organizers.registry import build as build_registry
from collectors.organizers.crawl import crawl_organizer, to_row, Fetcher
from collectors.organizers.match import to_event


def main(slugs):
    orgs = [o for o in build_registry(None) if o["scraper"] and (not slugs or o["slug"] in slugs)]
    tot = {"key": 0, "found": 0, "matched": 0, "dates": 0, "state": 0, "venue": 0}
    print(f"{'organizer':12} {'scraper':>7} {'crawler':>7} {'matched':>7} {'dates ok':>8} {'state ok':>8} {'venue':>6} {'pages':>5} {'secs':>5}")
    for o in orgs:
        t0 = time.time()
        key_rows, _ = wl.collect(only=[o["scraper"]])
        key_rows = [r for r in key_rows if r.get("start_date") and r["start_date"] >= dt.date.today() - dt.timedelta(days=1)]
        fetch = Fetcher(max_pages=40, render=True)
        try:
            res = crawl_organizer(o, fetch=fetch)
        finally:
            fetch.close()
        got = [to_row(o, e) for e in res["events"]]
        K = [to_event({**r, "organizer_name": o["name"]}) for r in key_rows]
        G = [to_event(r) for r in got]
        used, m = set(), {"matched": 0, "dates": 0, "state": 0, "venue": 0}
        for gi, ge in enumerate(G):
            best = None
            for ki, ke in enumerate(K):
                if ki in used or not ke.start or not ge.start: continue
                r = score(ke, ge)
                if r["decision"] != "no_match" and (best is None or r["score"] > best[1]["score"]): best = (ki, r)
            if not best: continue
            used.add(best[0]); m["matched"] += 1
            kr, gr = key_rows[best[0]], got[gi]
            m["dates"] += kr["start_date"] == gr["start_date"] and (kr.get("end_date") or kr["start_date"]) == gr["end_date"]
            m["state"] += (to_event(kr).state or "") == (to_event(gr).state or "") and bool(to_event(gr).state)
            m["venue"] += bool(gr.get("venues"))
        print(f"{o['slug']:12} {len(K):7} {len(G):7} {m['matched']:7} {m['dates']:8} {m['state']:8} {m['venue']:6} {res['pages']:5} {time.time() - t0:5.0f}"
              + (f"   {'; '.join(res['notes'])}" if res["notes"] else ""))
        tot["key"] += len(K); tot["found"] += len(G)
        for k in ("matched", "dates", "state", "venue"): tot[k] += m[k]
    rec = tot["matched"] / tot["key"] if tot["key"] else 0
    prec = tot["matched"] / tot["found"] if tot["found"] else 0
    print(f"\nTOTAL: scraper {tot['key']}, crawler {tot['found']}, matched {tot['matched']}  ->  recall {rec:.0%}, precision {prec:.0%}; "
          f"of matched: dates agree {tot['dates']}, state agrees {tot['state']}, venue captured {tot['venue']}")


if __name__ == "__main__":
    main(sys.argv[1:])
