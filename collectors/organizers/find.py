"""Find Organizer: who runs this event series, and what is their website?

Input: one past edition of an event (an event_history row: name, dates, state, venues, documents,
listing_url). Output: the organizer's name and website, how it was found, and whether a page on the
web independently proves it.

Cheapest evidence first; each stage only runs if the earlier ones did not settle it:

  1. tm_listing   Tourney Machine lists every CURRENT event with its organizer ("customer"), but not past
                  ones. Search for this year's edition, prove it is the same event with event_match
                  (organizer/name/venue/date evidence, never name alone), and take its organizer.   Free.
  2. documents    The event's rules / welcome-letter PDFs usually print the organizer's email or website.
                  Free.
  3. research     Claude with web search + web fetch, given everything above as hints.   Paid, optional.

Anything from stages 2 and 3 must pass verify(): we fetch the page ourselves and check it names this
event AND its town or venue. A model's word is never enough on its own.

    python -m collectors.organizers.find --fixture tests/fixtures/find_organizer_truth.json --stages tm,documents
"""
from __future__ import annotations
import io, re, os, sys, json, time, argparse, datetime as dt, urllib.parse
import requests

from ..common import log
from ..event_match import Event, score, distinctive_tokens, core_tokens

TM_SEARCH = "https://api.tourneymachine.com/v2/Tournaments/Search?per_page=100&page=1&query={q}"
TM_EVENT = "https://www.tourneymachine.com/Public/Results/Tournament.aspx?IDTournament={id}"
TM_H = {"accept": "application/json", "origin": "https://www.tourneymachine.com", "referer": "https://www.tourneymachine.com/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/152.0 Safari/537.36"}
BROWSER = {"user-agent": TM_H["user-agent"], "accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8"}

# Listing / registration / social platforms: never "the organizer's website" (a club's own subdomain on
# leagueapps or sportngin is fine and is kept by host, not by registered domain).
PLATFORMS = re.compile(r"(^|\.)(tourneymachine|sportsengine|exposureevents|travelsports|gotsport|playbook365|eventbrite|"
                       r"facebook|instagram|twitter|x|tiktok|youtube|google|goo|bit|linktr|usalacrosse|laxallstars|"
                       r"insidelacrosse|laxnumbers|maxpreps|apple|microsoft|adobe|wix|squarespace|godaddy|"
                       r"versantprivacy|versantmedia|sportngin|teamsnap|demosphere|stack|ussportscamps)\.(com|org|net|me|ly|gl|ee|co)$", re.I)
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com", "icloud.com", "comcast.net", "verizon.net",
             "msn.com", "live.com", "me.com", "optonline.net", "att.net", "sbcglobal.net", "protonmail.com"}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL = re.compile(r"\b(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|us|club|io|co|events|sports))\b(/[^\s)\"'<>]*)?", re.I)
HOSTED_BY = re.compile(r"(?:hosted|presented|run|organized|brought to you)\s+by\s+(?:the\s+)?([A-Z][\w&'.-]*(?:\s+[A-Z][\w&'.-]*){1,5})")
STATES = {"AL": "Alabama", "AZ": "Arizona", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
          "FL": "Florida", "GA": "Georgia", "IL": "Illinois", "IN": "Indiana", "KY": "Kentucky", "MA": "Massachusetts", "MD": "Maryland",
          "ME": "Maine", "MI": "Michigan", "MN": "Minnesota", "MO": "Missouri", "NC": "North Carolina", "NH": "New Hampshire", "NJ": "New Jersey",
          "NY": "New York", "OH": "Ohio", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "TN": "Tennessee",
          "TX": "Texas", "UT": "Utah", "VA": "Virginia", "VT": "Vermont", "WA": "Washington", "WI": "Wisconsin", "CO ": "Colorado"}


# ----------------------------------------------------------------------------------------- helpers
def _get(url, headers=None, timeout=25):
    try:
        r = requests.get(url, headers=headers or BROWSER, timeout=timeout, allow_redirects=True)
        return r if r.status_code == 200 else None
    except requests.RequestException as e:
        log.info("fetch failed %s: %s", url, e)
        return None


def host_of(url):
    if not url: return None
    if "://" not in url: url = "https://" + url
    h = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def is_platform(url_or_host):
    h = host_of(url_or_host) or ""
    reg = ".".join(h.split(".")[-2:])
    return bool(PLATFORMS.search(reg))


def homepage(url):
    h = host_of(url)
    return f"https://{h}/" if h else None


def page_text(html):
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    import html as _h
    return re.sub(r"\s+", " ", _h.unescape(t))


def edition_event(ed, shift_days=0):
    """event_history row -> event_match.Event (optionally moved forward to compare with next year)."""
    s = ed["start"] if isinstance(ed["start"], dt.date) else dt.date.fromisoformat(ed["start"])
    pts = [(float(v["lat"]), float(v["long"])) for v in (ed.get("venues") or []) if v.get("lat") and v.get("long")]
    return Event(name=ed["name"], start=s + dt.timedelta(days=shift_days), state=(ed.get("state") or "").upper() or None,
                 organizer=ed.get("organizer_name"), points=pts, teams=ed.get("teams"), gender=ed.get("gender"))


def place_words(ed):
    """Town names, venue names and state, for checking a page is about THIS event."""
    words = set()
    for cs in ed.get("city_states") or []:
        city = cs.split(",")[0].strip().lower()
        if len(city) > 2: words.add(city)
    for v in ed.get("venues") or []:
        n = (v.get("venue_name") or "").lower()
        toks = [w for w in re.findall(r"[a-z0-9]+", n) if w not in {"the", "field", "fields", "park", "complex", "sports", "school", "high"} and len(w) > 2]
        if toks: words.add(" ".join(toks[:2]))
    return words


# ------------------------------------------------------------------------------- stage 1: tm listing
MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"


def _clean_query(name):
    n = re.sub(r"\([^)]*\)", " ", name)                                   # (Grade 4-6), (BOX lacrosse)
    n = re.sub(r"\b(19|20)\d\d\b", " ", n)
    n = re.sub(rf"\b{MONTHS}\.?\s+\d{{1,2}}(st|nd|rd|th)?\b", " ", n, flags=re.I)   # May 16th
    n = re.sub(r"\b\d{1,2}/\d{1,2}(/\d{2,4})?\b", " ", n)
    n = re.sub(r"\bgrades?\s*\d+(\s*-\s*\d+)?\b", " ", n, flags=re.I)
    n = re.sub(r"[-–|:(),/]+", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def _queries(name):
    """Most specific first; Tourney Machine search returns nothing if any word is missing from the listing."""
    qs = [_clean_query(name)]
    dist = " ".join(sorted(distinctive_tokens(name)))
    if dist: qs.append(dist)
    core = [w for w in _clean_query(name).lower().split() if w not in {"lax", "lacrosse", "tournament", "the"}]
    if len(core) >= 2: qs.append(" ".join(core[:2]))
    return list(dict.fromkeys(q for q in qs if q))


def _year_shift(past_start, cur_start):
    """Days to move the past edition forward so it lands on the same weekday 1-3 years later, or None."""
    gap = (cur_start - past_start).days
    for years in (1, 2, 3):
        if abs(gap - 364 * years) <= 90:
            return round(gap / 7) * 7
    return None


def listing_candidates(ed, pool):
    """Same idea as tm_candidates but against listings the app already stored (including ones whose date has
    passed, which Tourney Machine's search no longer returns). pool: [{name, start, state, organizer, url, pts}]."""
    st = (ed.get("state") or "").upper()
    past = edition_event(ed)
    out = []
    for x in pool:
        if not x.get("organizer") or (st and (x.get("state") or "").upper() != st): continue
        s = x["start"] if isinstance(x["start"], dt.date) else dt.date.fromisoformat(x["start"])
        shift = _year_shift(past.start, s)
        if shift is None: continue
        cur = Event(name=x["name"], start=s, state=st, organizer=x["organizer"], points=[tuple(p) for p in (x.get("pts") or [])])
        res = score(edition_event(ed, shift), cur)
        if res["decision"] == "no_match": continue
        out.append({"method": "stored_listing", "name": x["organizer"], "website": None, "evidence_url": x.get("url"),
                    "listing": x["name"], "listing_date": str(s), "match": res["decision"], "match_score": res["score"],
                    "reasons": res["reasons"], "confidence": 0.95 if res["decision"] == "confirmed" else 0.6})
    out.sort(key=lambda c: (-c["confidence"], -c["match_score"]))
    return out


def tm_candidates(ed, max_pages_fetch=4):
    """Find this year's Tourney Machine listing of the series; return its organizer if event_match agrees."""
    st = (ed.get("state") or "").upper()
    seen, rows = set(), []
    for q in _queries(ed["name"]):
        try:
            r = requests.get(TM_SEARCH.format(q=urllib.parse.quote(q)), headers=TM_H, timeout=25)
            data = r.json().get("result") or [] if r.status_code == 200 else []
        except (requests.RequestException, ValueError):
            data = []
        for x in data:
            tid = x["link"].split("=")[-1]
            if tid in seen: continue
            seen.add(tid)
            if st and not any(c.strip().endswith(", " + st) or c.strip().endswith(" " + st) for c in x.get("cityStates") or []): continue
            if (x.get("icon") or "").lower() not in ("lacrosse", ed.get("sport", "lacrosse")): continue
            rows.append(x)
        time.sleep(0.4)
    from ..common import parse_date_range
    past = edition_event(ed)
    scored = []
    for x in rows:
        s, e = parse_date_range(x["date"])
        if not s: continue
        weeks = _year_shift(past.start, s)                       # same weekday, 1-3 years later
        if weeks is None: continue
        cur = Event(name=x["title"], start=s, state=st, organizer=x.get("customer"))
        scored.append([score(edition_event(ed, weeks), cur), x, s, weeks])
    scored.sort(key=lambda t: -t[0]["score"])
    # Venue evidence needs the listing page (search results carry no coordinates): fetch only the top few.
    from ..tourneymachine import parse_complexes
    out = []
    for res, x, s, weeks in scored[:max_pages_fetch]:
        if res["decision"] == "no_match" and res["score"] < 1: continue
        tid = x["link"].split("=")[-1]
        page = _get(TM_EVENT.format(id=tid), headers={**TM_H, "accept": "text/html"})
        pts = [(float(c["lat"]), float(c["long"])) for c in parse_complexes(page.text if page else "") if c.get("lat") and c.get("long")]
        cur = Event(name=x["title"], start=s, state=st, organizer=x.get("customer"), points=pts)
        res = score(edition_event(ed, weeks), cur)
        out.append({"method": "tm_listing", "name": x.get("customer"), "website": None,
                    "evidence_url": TM_EVENT.format(id=tid), "listing": x["title"], "listing_date": str(s),
                    "match": res["decision"], "match_score": res["score"], "reasons": res["reasons"],
                    "confidence": 0.95 if res["decision"] == "confirmed" else 0.6 if res["decision"] == "review" else 0.2})
        time.sleep(0.4)
    out.sort(key=lambda c: -c["confidence"])
    return out


# ------------------------------------------------------------------------------- stage 2: documents
def document_candidates(ed, max_docs=3):
    """Emails, websites and 'hosted by ...' lines printed in the event's PDFs."""
    from pypdf import PdfReader
    docs = [d.get("url") for d in (ed.get("documents") or []) if (d.get("url") or "").lower().endswith(".pdf")]
    docs.sort(key=lambda u: 0 if re.search(r"rule|welcome|letter|info|packet|guide", u, re.I) else 1)
    found = {}
    names = []
    for u in docs[:max_docs]:
        r = _get(u, timeout=40)
        if not r: continue
        try:
            text = " ".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(r.content)).pages[:3])
        except Exception as e:  # noqa: BLE001
            log.info("pdf unreadable %s: %s", u, e); continue
        for em in set(EMAIL.findall(text)):
            dom = em.split("@")[1].lower().rstrip(".")
            if dom in FREE_MAIL or is_platform(dom): continue
            found.setdefault(dom, {"emails": set(), "urls": set(), "source": u})["emails"].add(em.lower())
        for m in URL.finditer(text):
            dom = m.group(1).lower()
            if dom in FREE_MAIL or is_platform(dom) or dom.endswith((".pdf",)): continue
            found.setdefault(dom, {"emails": set(), "urls": set(), "source": u})["urls"].add(dom + (m.group(2) or ""))
        names += [n.strip(" .") for n in HOSTED_BY.findall(text)]
    out = []
    for dom, v in found.items():
        out.append({"method": "documents", "name": None, "website": f"https://{dom}/", "evidence_url": v["source"],
                    "emails": sorted(v["emails"]), "confidence": 0.6 if v["emails"] else 0.45})
    out.sort(key=lambda c: (-len(c["emails"]), c["website"]))
    if names:
        for c in out: c.setdefault("hosted_by", names[:3])
        if not out: out.append({"method": "documents", "name": names[0], "website": None, "evidence_url": docs[0], "confidence": 0.4})
    return out


# ------------------------------------------------------------------------------- stage 3: research
RESEARCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "organizer_name": {"type": "string", "description": "Company/club that runs this event; empty if not found"},
        "organizer_website": {"type": "string", "description": "Organizer's own homepage; empty if not found. Never a listing platform."},
        "evidence_url": {"type": "string", "description": "A page that names this event AND this organizer (ideally on the organizer's site)"},
        "confidence": {"type": "number", "description": "0-1"},
        "evidence": {"type": "string", "description": "One sentence: what on that page proves it"},
    },
    "required": ["organizer_name", "organizer_website", "evidence_url", "confidence", "evidence"],
}
RESEARCH_SYSTEM = """You identify who organizes a youth sports tournament. You are given one past edition (name, dates, state, venues) plus any hints.
Rules:
- Use web_search, then web_fetch to read pages. At most 5 searches and 6 fetches.
- Tourney Machine, SportsEngine, LeagueApps, Exposure Events, TravelSports, GotSport, Playbook365, Facebook are platforms, not organizers. Look past them to the club or company. A club's own subdomain on a platform (e.g. thinklax.leagueapps.com) is acceptable as organizer_website only if the club has no site of its own.
- Different organizers reuse the same event names in different states ("Fall Brawl", "Fall Classic"). Only answer if the page you cite matches this event's state and its town or venue. Otherwise return empty strings and confidence 0.
- evidence_url must be a page you actually fetched that names both the event and the organizer.
- Never invent a website. Return only the JSON."""


def research_candidates(ed, hints, client, model="claude-sonnet-5-5"):
    lines = [f"Event: {ed['name']} (sport: {ed.get('sport', 'lacrosse')}, state: {ed.get('state') or '?'})",
             f"Dates: {ed['start']}..{ed.get('end') or ed['start']}, teams: {ed.get('teams') or '?'}",
             "Venues: " + "; ".join(f"{v.get('venue_name')} ({v.get('address') or '?'})" for v in (ed.get("venues") or [])[:4]),
             f"Tourney Machine page: {ed.get('listing_url')}"]
    for h in hints[:5]:
        lines.append(f"Hint ({h['method']}): name={h.get('name') or '?'} website={h.get('website') or '?'} emails={','.join(h.get('emails') or []) or '?'}")
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 5},
             {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 6, "max_content_tokens": 15000}]
    messages = [{"role": "user", "content": "\n".join(lines)}]
    usage = {"in": 0, "out": 0, "searches": 0}
    for _ in range(4):
        resp = client.messages.create(model=model, max_tokens=4000, system=RESEARCH_SYSTEM, tools=tools, messages=messages,
                                      output_config={"format": {"type": "json_schema", "schema": RESEARCH_SCHEMA}})
        usage["in"] += resp.usage.input_tokens; usage["out"] += resp.usage.output_tokens
        st = getattr(resp.usage, "server_tool_use", None)
        if st: usage["searches"] += st.web_search_requests or 0
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content}); continue
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            j = json.loads(text)
        except json.JSONDecodeError:
            return [], usage
        if not j.get("organizer_name") and not j.get("organizer_website"): return [], usage
        return [{"method": "research", "name": j["organizer_name"] or None, "website": j["organizer_website"] or None,
                 "evidence_url": j["evidence_url"] or None, "confidence": float(j.get("confidence") or 0),
                 "evidence": j.get("evidence")}], usage
    return [], usage


# --------------------------------------------------------------------------------------- verify
def verify(cand, ed, fetch=_get):
    """Fetch the candidate's pages ourselves. Proven = a page on the organizer's site (or the cited page)
    names this event (distinctive words, or its whole name for generic names) AND its town or venue."""
    urls = [u for u in [cand.get("evidence_url"), cand.get("website")] if u and not u.lower().endswith(".pdf")]
    if cand.get("website"): urls += [urllib.parse.urljoin(cand["website"], p) for p in ("events", "tournaments", "events/")]
    dist, core = distinctive_tokens(ed["name"]), core_tokens(ed["name"])
    places = place_words(ed)
    st_full = STATES.get((ed.get("state") or "").upper(), "").lower()
    best = {"verified": False, "page": None, "event_named": False, "place_named": False, "on_org_site": False}
    for u in dict.fromkeys(urls):
        r = fetch(u)
        if not r: continue
        t = page_text(r.text).lower()
        words = set(re.findall(r"[a-z0-9]+", t))
        named = (len(dist & words) >= max(1, len(dist) - (len(dist) >= 3))) if dist else all(w in words for w in core)
        placed = any(p in t for p in places) or (st_full and st_full in t)
        on_site = bool(cand.get("website")) and host_of(r.url) == host_of(cand["website"])
        res = {"verified": named and placed, "page": r.url, "event_named": named, "place_named": placed, "on_org_site": on_site}
        if res["verified"] and (on_site or not best["verified"]): best = res
        if best["verified"] and best["on_org_site"]: break
        if not best["verified"] and (named or placed): best = res
    return best


# ----------------------------------------------------------------------------------------- driver
def find_organizer(ed, stages=("stored", "tm", "documents", "research"), client=None, model="claude-sonnet-5-5",
                   fetch=_get, pool=None):
    """-> {status, organizer_name, website, method, confidence, verification, candidates, usage}
    status: found_verified | needs_review | not_found"""
    cands, usage = [], {"in": 0, "out": 0, "searches": 0}
    for stage, fn in (("stored", lambda: listing_candidates(ed, pool or [])), ("tm", lambda: tm_candidates(ed))):
        if stage not in stages: continue
        got = fn()
        cands += got
        if got and got[0]["match"] == "confirmed":
            c = got[0]
            return {"status": "found_verified", "organizer_name": c["name"], "website": None, "method": c["method"],
                    "confidence": c["confidence"], "verification": {"verified": True, "page": c["evidence_url"],
                    "why": f"listing '{c['listing']}' ({c['listing_date']}) proven to be the next edition"},
                    "candidates": cands, "usage": usage}
    if "documents" in stages:
        cands += document_candidates(ed)
    if "research" in stages and client is not None:
        try:
            rc, usage = research_candidates(ed, cands, client, model)
            cands += rc
        except Exception as e:  # noqa: BLE001  one failed lookup must not stop the batch
            msg = str(e)
            if "workspace" in msg or "authentication" in msg.lower() or "credit" in msg.lower(): raise   # config problems: stop now
            log.warning("research failed for %s: %s", ed["name"], msg[:200])
            cands.append({"method": "research", "error": msg[:300], "confidence": 0})
    # Only a VERIFIED candidate is an answer. A listing that only partly matches is "needs_review".
    # Unverified website/PDF/model hits are kept as hints (shown in the review screen, fed to later stages).
    LISTING = ("tm_listing", "stored_listing")
    for c in sorted([c for c in cands if c["method"] not in LISTING], key=lambda c: -c["confidence"]):
        c["verification"] = v = verify(c, ed, fetch)
        if v.get("verified"):
            return {"status": "found_verified", "organizer_name": c.get("name") or (c.get("hosted_by") or [None])[0],
                    "website": homepage(c.get("website") or v.get("page")), "method": c["method"], "confidence": c["confidence"],
                    "verification": v, "candidates": cands, "usage": usage}
    review = [c for c in cands if c["method"] in LISTING and c["match"] == "review"]
    if review:
        c = review[0]
        return {"status": "needs_review", "organizer_name": c["name"], "website": None, "method": c["method"],
                "confidence": c["confidence"], "verification": {"verified": False, "why": "; ".join(c["reasons"])},
                "candidates": cands, "usage": usage}
    return {"status": "not_found", "organizer_name": None, "website": None, "method": None, "confidence": 0,
            "verification": None, "candidates": cands, "usage": usage}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", required=True, help="JSON list of past editions (event_history rows)")
    ap.add_argument("--stages", default="stored,tm,documents,research")
    ap.add_argument("--pool", help="JSON list of stored listings [{name,start,state,organizer,url,pts}] for the 'stored' stage")
    ap.add_argument("--limit", type=int); ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--out", default="find_results.jsonl")
    o = ap.parse_args()
    eds = json.load(open(o.fixture))[: o.limit]
    pool = json.load(open(o.pool)) if o.pool else []
    stages = tuple(o.stages.split(","))
    client = None
    if "research" in stages and os.environ.get("ANTHROPIC_API_KEY"):
        from ..anthropic_client import make_client
        client = make_client()
    elif "research" in stages:
        log.warning("ANTHROPIC_API_KEY not set: research stage skipped")
    with open(o.out, "w") as f:
        for i, ed in enumerate(eds, 1):
            t0 = time.time()
            r = find_organizer(ed, stages, client, o.model, pool=pool)
            f.write(json.dumps({"edition": {k: ed.get(k) for k in ("name", "start", "state", "listing_url", "fingerprint")},
                                "truth": ed.get("truth"), **r}, default=str) + "\n")
            log.info("%d/%d %s -> %s %s (%s) %.0fs", i, len(eds), ed["name"][:40], r["status"], r["organizer_name"], r["method"], time.time() - t0)


if __name__ == "__main__":
    main()
