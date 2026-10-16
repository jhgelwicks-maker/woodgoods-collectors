"""Grade find_organizer results against the answer key (organizer named on this year's Tourney Machine listing).

    python -m collectors.organizers.find --fixture tests/fixtures/find_organizer_truth.json --stages documents --out r.jsonl
    python tools/eval_find_organizer.py r.jsonl

Correct  = the organizer we returned is the true one (name words overlap, or the website domain contains them).
Wrong    = we returned a DIFFERENT organizer. This is the number that must stay near zero.
Not found= we returned nothing. Acceptable: the next stage gets a chance.
"""
import re, sys, json, collections

STOP = set("llc inc co the of and lacrosse lax events event tournaments tournament series sports sport club league association "
           "youth girls boys elite academy foundation booster boosters new".split())


def words(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 1}


def same_org(found_name, found_site, truth):
    t = words(truth)
    if not t: return False
    f = words(found_name)
    if f and (f & t or "".join(sorted(f)) == "".join(sorted(t))): return True
    host = re.sub(r"^https?://(www\.)?", "", found_site or "").split("/")[0].replace("-", "")
    return bool(host) and any(w in host for w in t if len(w) > 3)


PRICE = {"in": 2.0, "out": 10.0}   # $/1M tokens, claude-sonnet-5-5; web searches $0.01 each


def usage(rows):
    u = collections.Counter()
    for r in rows:
        for k, v in (r.get("usage") or {}).items(): u[k] += v
    cost = u["in"] / 1e6 * PRICE["in"] + u["out"] / 1e6 * PRICE["out"] + u["searches"] * 0.01
    n = len(rows) or 1
    print(f"usage: {u['in']:,} input tokens, {u['out']:,} output tokens, {u['searches']} searches "
          f"= ${cost:.2f} total, ${cost / n:.3f} per event")


def main(path):
    rows = [json.loads(l) for l in open(path) if l.strip()]
    if "--usage" in sys.argv:
        return usage(rows)
    tally = collections.Counter()
    for r in rows:
        if r["status"] == "not_found": k = "not_found"
        else:
            ok = same_org(r["organizer_name"], r["website"], r["truth"])
            k = f"{'correct' if ok else 'WRONG'}:{r['status']}"
        tally[k] += 1
        print(f"{k:28} {r['edition']['name'][:38]:38} -> {str(r['organizer_name'])[:28]:28} {str(r['website'] or '')[:30]:30} | truth: {r['truth']}")
    print("\n", dict(tally), f"  n={len(rows)}")
    wrong = sum(v for k, v in tally.items() if k.startswith("WRONG:found_verified"))
    print(f"verified-but-wrong: {wrong}   (must be 0)")


if __name__ == "__main__":
    main(sys.argv[1])
