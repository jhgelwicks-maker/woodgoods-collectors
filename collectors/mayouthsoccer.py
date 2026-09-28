"""Mass Youth Soccer sanctioned tournaments — plain HTML page, Claude extracts structured rows.
Template for every state-association source: fetch → strip → Claude → normalize."""
import os, re, json, datetime
from . import common
from .common import log

URL = "https://mayouthsoccer.org/events-and-programs/tournaments/"
SCHEMA = ("Return ONLY a JSON array. Each item: {name, dates_text, city, state, contact_email, contact_name, "
          "age_groups, team_levels, tournament_website}. Use null when absent. dates_text exactly as written on the page.")

def extract_with_claude(text):
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(model="claude-sonnet-4-6", max_tokens=4000,
        system="You extract youth sports tournament listings from web page text into strict JSON. No prose, no markdown fences.",
        messages=[{"role": "user", "content": f"{SCHEMA}\n\nPAGE TEXT:\n{text[:60000]}"}])
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    return json.loads(raw)

def collect():
    html = common.polite_get(URL)
    if not html: return [], 1
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text); text = re.sub(r"\s+", " ", text)
    items = extract_with_claude(text)
    rows, errors = [], 0
    year = datetime.date.today().year
    for it in items:
        s, e = common.parse_date_range(it.get("dates_text"), default_year=year)
        if s and s < datetime.date.today() - datetime.timedelta(days=30): s, e = common.parse_date_range(it.get("dates_text"), default_year=year + 1)
        if not it.get("name"): errors += 1; continue
        city, st = it.get("city"), it.get("state") or "MA"
        rows.append({
            "source": "mayouthsoccer", "source_event_id": re.sub(r"[^a-z0-9]+", "-", f"{it['name']}-{s}".lower()),
            "name": it["name"], "sport": "soccer", "organizer_name": None,
            "start_date": s, "end_date": e, "city_states": [f"{city}, {st}"] if city else [],
            "vendor_contact_email": it.get("contact_email"), "vendor_contact_name": it.get("contact_name"),
            "age_groups": it.get("age_groups"), "listing_url": URL, "official_url": it.get("tournament_website"),
            "raw": it,
        })
    return rows, errors
