"""Find the organizer behind each recurring Tourney Machine event, read the organizer's website,
and collect: website, contact info, confirmed next date for this event, and every other event the
organizer hosts.

Input comes from the Mac's database backup (no app access needed):
    ~/WoodGoodsAI/backup/db/current/event_history.jsonl   (+ event_candidates.jsonl for hints)
Output (private, outside the repo; contains contact details):
    ~/WoodGoodsAI/backup/organizers/series.jsonl     one line per event series, resumable
    ~/WoodGoodsAI/backup/organizers/review.csv       one row per series for Justin to eyeball
    ~/WoodGoodsAI/backup/organizers/contacts.csv
    ~/WoodGoodsAI/backup/organizers/other_events.csv
    ~/WoodGoodsAI/backup/organizers/cost.log

Claude does the research (web search + web fetch, server-side) and returns strict JSON.
Key: ANTHROPIC_API_KEY in ~/WoodGoodsAI/.anthropic.env.

    python -m collectors.organizer_discovery --limit 5            # pilot
    python -m collectors.organizer_discovery --sport lacrosse     # all active recurring lacrosse series
    python -m collectors.organizer_discovery --model claude-sonnet-5-5 ..."""
import os, io, re, csv, sys, json, time, argparse, datetime, collections
import requests
import anthropic
from .common import log

HOME = os.path.expanduser("~/WoodGoodsAI")
DB = os.path.join(HOME, "backup", "db", "current")
OUT = os.path.join(HOME, "backup", "organizers")
ENV_FILE = os.path.join(HOME, ".anthropic.env")

PRICE = {  # $ per 1M tokens: input, output  (+ $10 per 1,000 web searches)
    "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}

def _s(desc=None):
    d = {"type": "string"}
    if desc: d["description"] = desc
    return d

# The API allows at most 16 nullable/union-typed fields per schema, so every field is a plain string
# and "" means unknown. clean() turns "" back into None when saving.
NEXT = {"type": "object", "additionalProperties": False,
        "properties": {"start_date": _s("YYYY-MM-DD or empty"), "end_date": _s(), "venue": _s(), "city": _s(), "state": _s(),
                       "status": {"type": "string", "enum": ["confirmed", "tentative", "not_announced"]}, "source_url": _s()},
        "required": ["start_date", "end_date", "venue", "city", "state", "status", "source_url"]}
CONTACT = {"type": "object", "additionalProperties": False,
           "properties": {"name": _s(), "role": _s(), "email": _s(), "phone": _s(),
                          "kind": {"type": "string", "enum": ["vendor", "director", "general", "other"]}, "source_url": _s()},
           "required": ["name", "role", "email", "phone", "kind", "source_url"]}
OTHER = {"type": "object", "additionalProperties": False,
         "properties": {"name": _s(), "start_date": _s("YYYY-MM-DD or empty"), "end_date": _s(), "venue": _s(), "city": _s(), "state": _s(),
                        "sport": _s(), "gender": _s("boys, girls, both or empty"), "url": _s()},
         "required": ["name", "start_date", "end_date", "venue", "city", "state", "sport", "gender", "url"]}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "organizer_name": _s("The company/club that runs this event; empty if not found"),
        "organizer_website": _s("Homepage URL of the organizer; empty if not found"),
        "confidence": {"type": "number", "description": "0-1: how sure you are this organizer runs THIS event"},
        "evidence": _s("One sentence: what proved the organizer runs this event"),
        "event_page_url": _s("The organizer's own page for this specific event"),
        "next_occurrence": NEXT,
        "contacts": {"type": "array", "items": CONTACT},
        "vendor_info": _s("Anything about vendors/sponsors/booths: fees, application link, deadline"),
        "other_events": {"type": "array", "items": OTHER},
        "notes": _s(),
    },
    "required": ["organizer_name", "organizer_website", "confidence", "evidence", "event_page_url", "next_occurrence",
                 "contacts", "vendor_info", "other_events", "notes"],
}

def clean(x):
    """'' -> None, recursively; an all-empty next_occurrence that was never announced -> None."""
    if isinstance(x, dict):
        d = {k: clean(v) for k, v in x.items()}
        if set(d) == set(NEXT["properties"]) and d.get("status") == "not_announced" and not d.get("start_date"): return None
        return d
    if isinstance(x, list): return [clean(v) for v in x]
    return None if x == "" else x

SYSTEM = """You research youth sports tournament organizers for a company that sells custom hats from a vendor booth at tournaments.
Given one recurring tournament (name variants, state, venues, dates and team counts from past years), find out who runs it and read their website.

Rules:
- Use web_search to find the organizer, then web_fetch their site: the event's own page, the events/tournaments list, and the contact page.
- Tourney Machine, SportsEngine, LeagueApps, exposureevents, travelsports etc. are listing/registration platforms, NOT organizers; look past them to the real organizer (the club or company), though an organizer's own registration subdomain (e.g. thinklax.leagueapps.com) is fine as a page source.
- Same-name events run in many states ("Fall Classic"). Only accept an organizer when the state, venue or dates line up with this event's history. Set confidence below 0.5 if you could not confirm.
- next_occurrence: the NEXT edition after the most recent one in the history (usually next year). Only "confirmed" when the organizer's own page or registration lists the date. Dates as YYYY-MM-DD.
- other_events: every tournament/showcase the organizer lists for the coming season, with dates when shown. Skip camps, leagues and clinics. Don't include the target event itself.
- contacts: only what the site states publicly: general info emails, tournament directors, vendor/sponsorship contacts. No guessing, no personal social media.
- Prefer at most 8 fetches. If the organizer cannot be found, say so in notes and return nulls; never invent a website.
Return only the JSON object."""

def load_env():
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"'))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit(f"ANTHROPIC_API_KEY missing: put it in {ENV_FILE}")

def iter_jsonl(path):
    with open(path) as f:
        for line in f:
            if line.strip(): yield json.loads(line)

def build_series(sport, min_years=2, active_since="2025-01-01"):
    """Group non-league history rows by fingerprint; keep series seen in >= min_years years and still active."""
    groups = collections.defaultdict(list)
    for r in iter_jsonl(os.path.join(DB, "event_history.jsonl")):
        if r.get("source") != "tourneymachine" or r.get("sport") != sport or not r.get("event_fingerprint"): continue
        s, e = r.get("start_date"), r.get("end_date")
        if not s or (e and (datetime.date.fromisoformat(e) - datetime.date.fromisoformat(s)).days > 3): continue
        groups[r["event_fingerprint"]].append(r)
    hints = collections.defaultdict(dict)
    for c in iter_jsonl(os.path.join(DB, "event_candidates.jsonl")):
        fp = c.get("event_fingerprint")
        if fp in groups and c.get("status") != "duplicate":
            if c.get("organizer_name"): hints[fp]["organizer_name"] = c["organizer_name"]
            if c.get("official_url"): hints[fp]["official_url"] = c["official_url"]
            if c.get("start_date", "") >= str(datetime.date.today()) and c.get("source") == "tourneymachine":
                hints[fp]["upcoming_listing"] = f"{c['name']} {c['start_date']} (Tourney Machine)"
    out = []
    for fp, rows in groups.items():
        years = sorted({r["start_date"][:4] for r in rows})
        if len(years) < min_years or max(r["start_date"] for r in rows) < active_since: continue
        rows.sort(key=lambda r: r["start_date"])
        latest = rows[-1]
        out.append({"fingerprint": fp, "sport": sport, "state": latest.get("state"),
                    "names": sorted({r["name"] for r in rows}),
                    "years": [{"year": r["start_date"][:4], "dates": f"{r['start_date']}..{r.get('end_date') or r['start_date']}",
                               "teams": r.get("team_count"), "venue": (r.get("venues") or [{}])[0].get("venue_name"),
                               "city": (r.get("city_states") or [None])[0]} for r in rows],
                    "listing_url": latest.get("listing_url"), "documents": latest.get("documents") or [],
                    "max_teams": max((r.get("team_count") or 0) for r in rows), **hints.get(fp, {})})
    out.sort(key=lambda s: -s["max_teams"])
    return out

def pdf_hint(docs):
    """First lines of the first PDF (rules/flyer): usually names the organizer."""
    for d in docs:
        u = d.get("url") or ""
        if not u.lower().endswith(".pdf"): continue
        try:
            from pypdf import PdfReader
            r = requests.get(u, timeout=60); r.raise_for_status()
            t = " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages[:2])
            t = re.sub(r"\s+", " ", t).strip()
            if t: return t[:600]
        except Exception as e:  # noqa: BLE001
            log.warning("pdf hint failed %s: %s", u, e)
    return None

def prompt_for(s):
    lines = [f"Event: {s['names'][-1]}  (sport: {s['sport']}, state: {s['state'] or 'unknown'})",
             "Name variants seen on Tourney Machine: " + " | ".join(s["names"][:6]),
             "History (year: dates, teams, venue, city):"]
    for y in s["years"][-6:]:
        lines.append(f"  {y['year']}: {y['dates']}, {y['teams'] or '?'} teams, {y['venue'] or '?'}, {y['city'] or '?'}")
    if s.get("organizer_name"): lines.append(f"Organizer named on this year's listing: {s['organizer_name']}")
    if s.get("official_url"): lines.append(f"Official URL on the listing: {s['official_url']}")
    if s.get("upcoming_listing"): lines.append(f"Already listed for next season: {s['upcoming_listing']}")
    if s.get("pdf_hint"): lines.append(f"Start of the event's rules/flyer PDF: \"{s['pdf_hint'][:400]}\"")
    lines.append(f"Tourney Machine page: {s['listing_url']}")
    return "\n".join(lines)

def research(client, model, s, max_continuations=4):
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 6},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 10, "max_content_tokens": 20000}]
    messages = [{"role": "user", "content": prompt_for(s)}]
    usage = {"in": 0, "out": 0, "searches": 0}
    for _ in range(max_continuations + 1):
        with client.messages.stream(model=model, max_tokens=16000, system=SYSTEM, tools=tools, messages=messages,
                                    output_config={"format": {"type": "json_schema", "schema": SCHEMA}}) as stream:
            resp = stream.get_final_message()
        usage["in"] += resp.usage.input_tokens + (resp.usage.cache_read_input_tokens or 0) + (resp.usage.cache_creation_input_tokens or 0)
        usage["out"] += resp.usage.output_tokens
        st = getattr(resp.usage, "server_tool_use", None)
        if st: usage["searches"] = (st.web_search_requests or 0)
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content}); continue
        if resp.stop_reason == "refusal":
            return {"error": f"refusal: {getattr(resp.stop_details, 'explanation', '')}"}, usage
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text: return {"error": f"no text ({resp.stop_reason})"}, usage
        try: return clean(json.loads(text)), usage
        except json.JSONDecodeError: return {"error": "bad json", "raw": text[:500]}, usage
    return {"error": "still paused after continuations"}, usage

def cost(model, u):
    pi, po = PRICE.get(model, (4.0, 20.0))
    return u["in"] / 1e6 * pi + u["out"] / 1e6 * po + u["searches"] * 0.01

def write_csvs(done):
    with open(os.path.join(OUT, "review.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["fingerprint", "event", "state", "years", "max_teams", "organizer", "website", "confidence",
                                       "next_start", "next_status", "contacts", "other_events", "evidence", "notes"])
        for s in done:
            r = s.get("result") or {}; nx = r.get("next_occurrence") or {}
            w.writerow([s["fingerprint"], s["names"][-1], s["state"], len(s["years"]), s["max_teams"], r.get("organizer_name"), r.get("organizer_website"),
                        r.get("confidence"), nx.get("start_date"), nx.get("status"), len(r.get("contacts") or []), len(r.get("other_events") or []),
                        r.get("evidence"), r.get("notes") or r.get("error")])
    with open(os.path.join(OUT, "contacts.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["organizer", "event", "name", "role", "email", "phone", "kind", "source_url"])
        for s in done:
            r = s.get("result") or {}
            for c in r.get("contacts") or []:
                w.writerow([r.get("organizer_name"), s["names"][-1], c.get("name"), c.get("role"), c.get("email"), c.get("phone"), c.get("kind"), c.get("source_url")])
    with open(os.path.join(OUT, "other_events.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["organizer", "website", "name", "start_date", "end_date", "venue", "city", "state", "sport", "gender", "url"])
        seen = set()
        for s in done:
            r = s.get("result") or {}
            for e in r.get("other_events") or []:
                k = (r.get("organizer_name"), e.get("name"), e.get("start_date"))
                if k in seen: continue
                seen.add(k); w.writerow([r.get("organizer_name"), r.get("organizer_website"), e.get("name"), e.get("start_date"), e.get("end_date"),
                                         e.get("venue"), e.get("city"), e.get("state"), e.get("sport"), e.get("gender"), e.get("url")])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="lacrosse"); ap.add_argument("--limit", type=int); ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--min-teams", type=int, default=0); ap.add_argument("--dry-run", action="store_true")
    o = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    series = [s for s in build_series(o.sport) if s["max_teams"] >= o.min_teams]
    path = os.path.join(OUT, "series.jsonl")
    done = list(iter_jsonl(path)) if os.path.exists(path) else []
    done = [d for d in done if not (d.get("result") or {}).get("error")]   # failed ones are retried
    done_fp = {d["fingerprint"] for d in done}
    todo = [s for s in series if s["fingerprint"] not in done_fp]
    if o.limit: todo = todo[:o.limit]
    log.info("%d %s series qualify; %d already done; %d to research with %s", len(series), o.sport, len(done), len(todo), o.model)
    if o.dry_run:
        for s in todo[:3]: print(prompt_for(s), "\n")
        return
    load_env(); client = anthropic.Anthropic()
    total = 0.0
    for i, s in enumerate(todo, 1):
        s["pdf_hint"] = pdf_hint(s["documents"])
        t0 = time.time()
        try:
            result, usage = research(client, o.model, s)
        except anthropic.APIError as e:
            result, usage = {"error": f"api: {e}"}, {"in": 0, "out": 0, "searches": 0}
        c = cost(o.model, usage); total += c
        rec = {**{k: v for k, v in s.items() if k != "documents"}, "result": result, "usage": usage, "cost_usd": round(c, 4),
               "model": o.model, "researched_at": datetime.datetime.now().isoformat(timespec="seconds")}
        with open(path, "a") as f: f.write(json.dumps(rec) + "\n")
        done.append(rec)
        org = result.get("organizer_name") or result.get("error")
        log.info("%d/%d %s -> %s (conf %s, %d other events, %d contacts) $%.3f %.0fs", i, len(todo), s["names"][-1][:40], org,
                 result.get("confidence"), len(result.get("other_events") or []), len(result.get("contacts") or []), c, time.time() - t0)
        with open(os.path.join(OUT, "cost.log"), "a") as f: f.write(f"{rec['researched_at']} {o.model} {s['fingerprint']} ${c:.4f}\n")
    write_csvs(done)
    log.info("done: %d researched this run, $%.2f; %d series in %s", len(todo), total, len(done), OUT)

if __name__ == "__main__":
    main()
