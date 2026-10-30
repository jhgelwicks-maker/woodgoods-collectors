"""Confirm online: for lacrosse events no organizer site we scrape lists (the grey "not checked" badge), search the web
for the event, read the organizer's page and check it against the app's dates and place.

    python tools/web_confirm.py --limit 30                 # dry run: writes a CSV and badge records, sends nothing
    python tools/web_confirm.py --limit 30 --send          # also sends the badge records

For each event:
  1. Search (Brave Search API): '<event name> lacrosse <state>'. Listing and social sites (Tourney Machine, Facebook,
     travelsports, ...) are skipped: the organizer's own page is what counts.
  2. Verify (organizers.find.verify): we fetch the page ourselves; it must name the event AND its town or venue. A search
     result alone never confirms anything.
  3. Read the page: the event's dates nearest its name, venue/town, age divisions, boys/girls, contacts.
  4. Badge: 🟢 'Confirmed · event website' only when the page shows the same weekend and place; 🔴 when it shows another
     date; 🟡 when the page is the event's but shows no date we can read. Nothing found: the row stays grey.

Costs nothing within Brave's monthly free credit: searches are counted in ~/WoodGoodsAI/brave_usage.json and stop at
--cap a month (default 800, about $4 of the ~$5 credit). The key is BRAVE_API_KEY, from the environment or
~/WoodGoodsAI/.search.env (never the repo)."""
import os, re, sys, csv, json, time, argparse, pathlib, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from collectors.organizers import find
from collectors.event_match import distinctive_tokens, core_tokens
from core.model import parse_date_range, normalize_gender
from adapters import details

HOME = pathlib.Path(os.path.expanduser("~/WoodGoodsAI"))
USAGE = HOME / "brave_usage.json"
BRAVE = "https://api.search.brave.com/res/v1/web/search"
# a day is one or two digits not followed by more ('November 2026' is a month and a year, not November 20)
DATE = re.compile(r"\b((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?!\d)"
                  r"(?:\s*(?:[-–&.+]|to|and)\s*(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?\d{1,2}(?:st|nd|rd|th)?(?!\d))?"
                  r"(?:,?\s*20\d\d)?)", re.I)
# dates that are deadlines, not the event ('a $10 late fee applies ... after November 1', 'payments due November 1st')
NOT_EVENT = re.compile(r"late fee|deadline|\bdue\b|registration (?:opens|closes|ends)|register by|early[- ]bird|discount|"
                       r"posted on|updated|planner|\bSATs?\b|until|refund|\bby\s*$|before\s*$|after\s*$|(?:registration|entries|portal|sign-?ups?)\s+(?:opens?|closes?)\s*(?:on)?\s*$", re.I)
CACHE = HOME / "web_confirm_cache.json"
# directories and marketplaces that list other people's events: not the organizer, often out of date
THIRD_PARTY = re.compile(r"(^|\.)(opensponsorship|tournamentinsights|tournamentstoday|allevents|eventbrite|sportsrecruits|"
                         r"activekids|active|ussportscamps|lax\.com)\.(com|org|net|in)$", re.I)


def brave_key():
    if os.environ.get("BRAVE_API_KEY"):
        return os.environ["BRAVE_API_KEY"]
    f = HOME / ".search.env"
    for line in (f.read_text().splitlines() if f.exists() else []):
        if line.startswith("BRAVE_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    sys.exit(f"BRAVE_API_KEY missing: put BRAVE_API_KEY=... in {f}")


class Budget:
    """Searches this month, kept on disk so the cap holds across runs."""
    def __init__(self, cap, path=USAGE):
        self.cap, self.path, self.month = cap, path, dt.date.today().strftime("%Y-%m")
        data = json.loads(path.read_text()) if path.exists() else {}
        self.used = data.get(self.month, 0)

    def take(self):
        if self.used >= self.cap:
            return False
        self.used += 1
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        data[self.month] = self.used
        self.path.write_text(json.dumps(data))
        return True


def search(q, key, budget):
    """[(url, title)] of web results (remembered, so a rerun spends no search), None when the monthly cap is reached."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    if q in cache:
        return [tuple(x) for x in cache[q]]
    if not budget.take():
        return None
    try:
        r = requests.get(BRAVE, params={"q": q, "count": 10, "country": "us"}, timeout=25,
                         headers={"X-Subscription-Token": key, "Accept": "application/json"})
        time.sleep(1.1)
        hits = [(x["url"], x.get("title", "")) for x in (r.json().get("web") or {}).get("results", [])] if r.ok else []
    except (requests.RequestException, ValueError):
        return []
    cache[q] = hits
    CACHE.write_text(json.dumps(cache))
    return hits


def query(name, state):
    full = find.STATES.get((state or "").upper(), state or "")
    return re.sub(r"\s+", " ", f"{find._clean_query(name)} lacrosse {full}").strip()


def current_page(text, near, name=""):
    """Does the page speak about this season? Last season's page ('February 7th 2026' for a February 2027 event, a 2022
    news post) confirms nothing, since last year's dates land right on the app's guess. This season's page shows it by
    a date with this year near the event's date, a '2026-27 season', this year just before a date ('2026 Tournament
    Information ... November 7-8'), or this year next to the event's name ('TURKEY SHOOTOUT 2026')."""
    # judged by dates, not bare years: divisions ('2031/2032') and grad years are years too
    for m in DATE.finditer(text):
        if re.search(r"20\d\d", m.group(1)):
            s, _ = parse_date_range(m.group(1))
            if s and abs((s - near).days) <= 150:
                return True
    seasons = re.findall(r"\b20(\d\d)\s*[-/–]\s*(?:20)?(\d\d)\s*(?:season|winter|fall|indoor|league|event)", text, re.I)
    if any(2000 + int(b) >= near.year >= 2000 + int(a) for a, b in seasons):
        return True
    for m in DATE.finditer(text):                            # the last year written just before a date without one
        if re.search(r"20\d\d", m.group(1)):
            continue
        before = re.findall(r"\b(20\d\d)\b(?![-/]\d)", text[max(0, m.start() - 100):m.start()])
        if before and int(before[-1]) == near.year:
            return True
    words = [w for w in sorted(core_tokens(name), key=len, reverse=True) if len(w) > 3][:1]
    return bool(words) and bool(re.search(rf"\b{near.year}\W+(?:\w+\W+){{0,3}}{words[0]}\b|\b{words[0]}\W+(?:\w+\W+){{0,3}}{near.year}\b",
                                          text, re.I))


def event_dates(text, name, near):
    """(start, end) of the dates nearest the event's name on the page, within 120 days of the app's date, skipping
    deadlines; None if none."""
    words = distinctive_tokens(name)
    words = sorted(words if len(words) >= 2 else core_tokens(name), key=len, reverse=True)[:3]   # 'IL Fall Cup': all three
    low = text.lower()
    # mentions of the event: its words together, as a name is written ('IL Fall Cup', not 'IL Fall Derby ... IL Fall Cup')
    first = [m.start() for m in re.finditer(rf"\b{re.escape(words[0])}\b", low)] if words else []
    spots = [p for p in first if all(re.search(rf"\b{re.escape(w)}\b", low[max(0, p - 30):p + 40]) for w in words[1:])] or first or [0]
    best = None
    for m in DATE.finditer(text):
        if NOT_EVENT.search(text[max(0, m.start() - 60):m.start()]):
            continue
        s, e = parse_date_range(m.group(1), default_year=near.year)
        if s and not re.search(r"20\d\d", m.group(1)) and abs((s - near).days) > 180:     # a year-less date in the next year
            s, e = parse_date_range(m.group(1), default_year=near.year + (1 if s < near else -1))
        if not s or abs((s - near).days) > 120:
            continue
        # most pages write the name, then its date; a date before the name counts four times as far, measured from where
        # it ends ('December 29-31: The Holiday Bowl' is right next to its name)
        dist = min((m.start() - p) if m.start() >= p else 4 * max(0, p - m.end()) for p in spots)
        dist -= 500 if str(near.year) in m.group(1) else 0                                              # this year written in: preferred
        if best is None or dist < best[0]:
            best = (dist, s, e or s)
    return (best[1], best[2]) if best else None


def own_page(url, title, name):
    """Is the page the event's own (its words in the web address or the page title), not a list of many events or a
    team schedule? Only then may its date overrule the app's."""
    words = distinctive_tokens(name) or core_tokens(name)
    where = (url + " " + (title or "")).lower()
    return bool(words) and sum(1 for w in words if w in where) >= min(2, len(words))


def confirm(row, ed, key, budget, fetch=find._get):
    """One grey row -> result dict (status, url, dates, details) or None when the monthly cap is reached."""
    q = query(row["name"], row.get("state"))
    hits = search(q, key, budget)
    if hits is None:
        return None
    out = {"query": q, "status": "not_found", "url": None}
    for url, title in [h for h in hits if not find.is_platform(h[0]) and not THIRD_PARTY.search(find.host_of(h[0]) or "")][:3]:
        v = find.verify({"evidence_url": url, "website": find.homepage(url)}, ed, fetch)
        if not v.get("verified"):
            continue
        page = fetch(v["page"])
        text = details.full_text(page.text) if page else ""
        near = dt.date.fromisoformat(row["start_date"][:10])
        dates = event_dates(text, row["name"], near)
        out.update(url=v["page"], organizer_site=find.homepage(v["page"]), title=title, verified_place=v.get("place_named"),
                   age_range=details.grad_years(text, near), contacts=details.page_contacts(text, {find.host_of(v["page"])}),
                   gender=normalize_gender(f"{row['name']} {title}"))
        if not current_page(text, near, row["name"]):
            out["status"] = "site_old"                     # last season's page: date and place prove nothing yet
        elif not dates:
            out["status"] = "site_no_date"
        else:
            s, e = dates
            off = (s - near).days
            out.update(found_start=str(s), found_end=str(e), days_off=off)
            if abs(off) <= 2:
                out["status"] = "confirmed"
            elif abs(off) <= 21 and "league" not in row["name"].lower() and own_page(v["page"], title, row["name"]):
                out["status"] = "date_moved"
            else:
                # a big jump, a league (a season of dates) or a page about many events: show the date, claim nothing
                out["status"] = "date_unsure"
        break
    return out


def ambiguous(results):
    """One page and one date for several of our events that the app has on different dates (Gold Standard's four
    'Autumn Gold Showcase' days all landing on one page's 'Nov 14'): the page doesn't say which is which, so none is
    corrected."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r, res in results:
        if res["status"] == "date_moved":
            groups[(res["url"], res["found_start"])].append((r, res))
    for g in groups.values():
        if len({r["start_date"] for r, _ in g}) > 1:
            for _, res in g:
                res["status"] = "date_unsure"


def badge_record(row, res, now):
    """The organizer-check record for a found event (None when nothing was found)."""
    url = res.get("url")
    if not url:
        return None
    fmt = lambda s, e: (dt.date.fromisoformat(s).strftime("%b %-d") + ("" if e == s else "–" + dt.date.fromisoformat(e).strftime("%b %-d")))
    app = fmt(row["start_date"][:10], (row.get("end_date") or row["start_date"])[:10])
    if res["status"] == "confirmed":
        code, bdg = "confirmed", "🟢 Confirmed · event website"
        desc = f"The event's own website ({find.host_of(url)}) lists it the same weekend at the same place."
    elif res["status"] == "date_moved":
        found = fmt(res["found_start"], res["found_end"])
        code, bdg = "date_moved", f"🔴 Date corrected · app guessed {app}, event website says {found}"
        desc = f"The event's own website ({find.host_of(url)}) gives {found}; the app's date ({app}) is a guess from last year."
    elif res["status"] == "site_old":
        return None                                          # last season's page: the row stays grey
    elif res["status"] == "date_unsure":
        found = fmt(res["found_start"], res["found_end"])
        code, bdg = "review", f"🟡 Possible match, check · event website says {found}"
        desc = (f"A website ({find.host_of(url)}) names this event and gives {found}, but it may be a list of several events, a "
                f"league season or a team schedule, so the app's date ({app}) isn't changed. Check it by hand.")
    else:
        code, bdg = "review", "🟡 Possible match, check · event website"
        desc = f"Found the event's website ({find.host_of(url)}), but it shows no date we could read; check it by hand."
    return {"app_dedupe_key": row["dedupe_key"], "status": code, "badge": bdg, "badge_description": desc, "details": desc,
            "tags": ["FOUND ONLINE"], "checked_at": now, "organizer_url": url, "organizer_site": res.get("organizer_site"),
            "organizer_start": res.get("found_start"), "organizer_end": res.get("found_end"),
            "organizer_age_divisions": res.get("age_range"), "organizer_contacts": res.get("contacts") or [],
            "found_via": "web search", "search_query": res.get("query")}


def grey_rows(export, conf_path, gate_only=True):
    """Upcoming lacrosse rows whose current badge is the grey 'not checked' one (optionally only those passing the gate)."""
    conf = {json.loads(l)["app_dedupe_key"]: json.loads(l) for l in open(conf_path)}
    out = []
    for line in open(export / "event_candidates.jsonl"):
        r = json.loads(line)
        if not (r.get("sport") or "").lower().startswith("lacrosse") or r.get("status") in ("duplicate", "stale", "rejected"):
            continue
        if (r.get("end_date") or r.get("start_date") or "") < dt.date.today().isoformat():
            continue
        if gate_only and not r.get("gate_passed"):
            continue
        c = conf.get(r["dedupe_key"]) or {}
        if c.get("status") == "unchecked" and not c.get("organizer_scraped"):
            out.append(r)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export-dir", type=pathlib.Path, required=True, help="has event_candidates.jsonl and event_confirmations.jsonl")
    ap.add_argument("--history", type=pathlib.Path, help="event_history.jsonl (default: in --export-dir)")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--cap", type=int, default=800, help="searches a month (Brave's free credit is ~1,000)")
    ap.add_argument("--out", type=pathlib.Path, default=HOME / "reports" / f"web_confirm_{dt.date.today()}.csv")
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args(argv)
    rows = grey_rows(args.export_dir, args.export_dir / "event_confirmations.jsonl")
    rows.sort(key=lambda r: -(r.get("expected_teams") or r.get("team_count") or 0))          # the biggest events first
    rows = rows[: args.limit]
    hids = {(r.get("raw") or {}).get("history_id") for r in rows} - {None}
    hist = {}
    for line in open(args.history or args.export_dir / "event_history.jsonl"):
        h = json.loads(line)
        if h["id"] in hids: hist[h["id"]] = h
    key, budget = brave_key(), Budget(args.cap)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    results, records = [], []
    for i, r in enumerate(rows, 1):
        h = hist.get((r.get("raw") or {}).get("history_id")) or r
        ed = {"name": r["name"], "start": r["start_date"], "state": r.get("state"), "city_states": h.get("city_states") or r.get("city_states") or [],
              "venues": h.get("venues") or r.get("venues") or [], "sport": "lacrosse"}
        res = confirm(r, ed, key, budget)
        if res is None:
            print(f"monthly search cap ({args.cap}) reached; stopping")
            break
        results.append((r, res))
        print(f"{i}/{len(rows)} {res['status']:12} | {r['name'][:44]:44} | {res.get('url') or ''}", flush=True)
    ambiguous(results)
    records = [rec for r, res in results for rec in [badge_record(r, res, now)] if rec]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "event", "app_start", "state", "teams", "found_start", "found_end", "days_off", "website", "age_divisions",
                    "contacts", "search_query", "app_page"])
        for r, res in results:
            w.writerow([res["status"], r["name"], r["start_date"], r.get("state"), r.get("expected_teams") or r.get("team_count") or "",
                        res.get("found_start") or "", res.get("found_end") or "", res.get("days_off", ""), res.get("url") or "",
                        res.get("age_range") or "", "; ".join(c["email"] for c in res.get("contacts") or []), res["query"],
                        f"https://opswg.lovable.app/admin/candidates/{r['id']}"])
    args.out.with_suffix(".checks.json").write_text(json.dumps(records, indent=1, default=str))
    from collections import Counter
    print("results:", dict(Counter(res["status"] for _, res in results)), f"| searches this month: {budget.used}/{args.cap}")
    print("wrote", args.out)
    if args.send and records:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import lacrosse_match_report as L
        L.send_checks(records)


if __name__ == "__main__":
    main()
