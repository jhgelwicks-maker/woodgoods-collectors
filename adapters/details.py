"""Age divisions and contacts from an event's own page, for every organizer scraper.

grad_years()      'Grad Years Eligible: 2027-2035/2036', 'Divisions 2027 - 2036', 'Boys & Girls: 18U-8U' -> '2027–2036'
page_contacts()   the emails (with a name and phone when the page pairs them) an event page gives, each with a role:
                  vendor (the page says to write there as a vendor or sponsor), director (a named person, or the page
                  calls it the director's), organizer (the organizer's general inbox)
enrich()          fills an Event's age_range and contacts from its page when the scraper didn't, reusing the page the
                  scraper already fetched (BaseCollector caches it)"""
from __future__ import annotations
import re, datetime
from bs4 import BeautifulSoup

MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# dates and copyright years, removed before reading grad years so 'November 7, 2026' is never a division
DATES = re.compile(rf"\b{MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*(?:[-–&]|to|and)\s*(?:{MONTH}\s+)?\d{{1,2}}(?:st|nd|rd|th)?)?,?\s*20\d\d"
                   rf"|\b\d{{1,2}}/\d{{1,2}}/20\d\d|©\s*20\d\d|\b20\d\d\s+(?:season|rules|nfhs)|\b(?:spring|summer|fall|winter)\s+20\d\d\b"
                   rf"|\b{MONTH}\s*[-–]\s*{MONTH},?\s*20\d\d", re.I)
AGE_KEY = re.compile(r"grad(?:uation)?\s*(?:years?|class(?:es)?|yrs?)|class(?:es)?\s*(?:of)?\s*:?|divisions?|age\s*(?:groups?|divisions?|guidelines?)"
                     r"|\bages?\b|eligib\w*|\bboys?\b|\bgirls?\b", re.I)
GRAD = re.compile(r"\b(20[2-4]\d)s?\b")
UAGE = re.compile(r"\b(?:U-?(\d{1,2})|(\d{1,2})\s*U)\b")
GRADE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.I)
GRADE_PAIR = re.compile(r"(?<![\d/])(\d)\s*/\s*(\d)(?![\d/])")          # '7/8', '5/6': grades
HS = re.compile(r"\b(?:HS|high\s*school|varsity)\b", re.I)


def grad_years(text, when=None):
    """The age divisions a page states, as a grad-year range ('2027–2036'), else None. Read next to words like 'grad
    years', 'divisions', 'ages', 'boys': grad years, grades ('8th', '7/8'), age groups ('U14', '14U') and 'HS', each
    turned into grad years for the school year the event falls in (a November 2026 event is in the 2026-27 year, so
    its oldest players are the class of 2027). Years outside that school year to 14 years on are ignored."""
    if not text: return None
    when = when or datetime.date.today()
    end = when.year + 1 if when.month >= 8 else when.year        # the school year ending next spring
    t = DATES.sub(" ", text)
    grads = set()
    for m in AGE_KEY.finditer(t):
        window = t[m.end():m.end() + 90]
        window = re.split(r"(?<=[a-z])\.\s|\bregistration\b|\$", window, maxsplit=1, flags=re.I)[0]
        grads.update(int(g) for g in GRAD.findall(window))
        grads.update(end + 18 - int(a or b) for a, b in UAGE.findall(window) if 6 <= int(a or b) <= 19)
        grads.update(end + 12 - int(g) for g in GRADE.findall(window) if 1 <= int(g) <= 12)
        for a, b in GRADE_PAIR.findall(window):
            grads.update(end + 12 - int(g) for g in (a, b) if 1 <= int(g) <= 8)
        if HS.search(window):
            grads.update({end, end + 3})
    grads = {g for g in grads if end <= g <= end + 14}
    if not grads: return None
    lo, hi = min(grads), max(grads)
    return f"{lo}" if lo == hi else f"{lo}–{hi}"


EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?([2-9]\d{2})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)")
JUNK_EMAIL = re.compile(r"(?:\.(?:png|jpe?g|gif|webp|svg)$|@(?:sentry|wixpress|example|domain|email|godaddy|yourdomain)\.|"
                        r"^(?:no-?reply|privacy|webmaster|abuse|postmaster|wordpress|name|your\.?name|you|email|first\.?last)@)", re.I)
# hotel partners the pages list ('team@athleteshospitality.com'): not the organizer
HOTEL = re.compile(r"hotel|hospitality|lodging|housing|staytoplay", re.I)
VENDOR = re.compile(r"vendor|sponsor|exhibitor|merchandis|booth", re.I)
DIRECTOR = re.compile(r"director|tournament\s+contact|event\s+contact|questions\s+(?:about|regarding)\s+the\s+event", re.I)
NAME_BEFORE = re.compile(r"([A-Z][a-z]+\s+[A-Z][a-z'\-]+)\s*(?:[·•|:,\-–]\s*)?(?:email\s*:?\s*)?$")
NOT_NAME = re.compile(r"\b(email|contact|questions|info|inquir\w*|director|register\w*|tournaments?|lacrosse|events?|sports?|click|send|us|"
                      r"view|recap|details|help|more|learn|read|here|staff|team|now|youth|our|the|classic|nation|cup|showcase|"
                      r"invitational|league|camp|clinic|hotel|travel|schedule|support|office|general|media|press)\b", re.I)


def _cf_decode(hexstr):
    """Cloudflare's email protection ('data-cfemail' / '/cdn-cgi/l/email-protection#...') back to the address."""
    try:
        key = int(hexstr[:2], 16)
        return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))
    except ValueError:
        return ""


def full_text(html):
    """A page's visible text with the footer kept (organizers put their inbox there) and protected emails decoded."""
    soup = BeautifulSoup(html, "lxml")
    for el in soup.select("[data-cfemail]"):
        el.replace_with(_cf_decode(el["data-cfemail"]))
    for a in soup.select('a[href*="email-protection#"]'):
        a.replace_with(_cf_decode(a["href"].split("#", 1)[1]))
    for a in soup.select('a[href^="mailto:"]'):
        addr = a["href"][7:].split("?")[0]
        if addr and addr not in a.get_text():
            a.append(f" {addr}")
    for t in soup(["script", "style", "noscript", "nav"]):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _phone_near(text, i):
    m = PHONE.search(text[max(0, i - 160):i + 200])
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


FREE_MAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "aol.com", "icloud.com", "comcast.net", "verizon.net", "msn.com"}


def site_domain(url_or_email):
    """'https://events.nxtsports.com/x' / 'tom@nxtsports.com' -> 'nxtsports.com'."""
    host = (url_or_email or "").split("@")[-1].split("//")[-1].split("/")[0].split(":")[0].lower()
    return ".".join(host.split(".")[-2:]) if "." in host else ""


def own_domain(email, domains):
    """On the organizer's domain, a variant of it ('info@bukulax.com' for buku.events), or a personal mailbox."""
    d = site_domain(email)
    names = {x.split(".")[0] for x in domains if len(x.split(".")[0]) >= 3}
    return d in domains or d in FREE_MAIL or any(n in d.split(".")[0] for n in names)


def page_contacts(text, domains=None):
    """[{name, email, phone, role}], vendor first, then directors, then the organizer's inbox; one per email. With
    domains, only addresses on the organizer's own domains or a personal mailbox (gmail, ...) are kept: pages also
    list their travel agent (teamtravelsource.com), software help desks (eventbeacon.com) and template leftovers.
    A role is read from the address itself ('vendors@') and the words just before it, back to the previous address,
    so 'vendors@victoryeventseries.com' doesn't make its neighbours vendor contacts too."""
    out, seen, prev = [], set(), 0
    for m in EMAIL.finditer(text or ""):
        email = m.group(0).strip(".").lower()
        before = text[max(prev, m.start() - 140):m.start()]
        prev = m.end()
        if email in seen or JUNK_EMAIL.search(email) or HOTEL.search(email): continue
        if domains and not own_domain(email, domains): continue
        seen.add(email)
        nm = NAME_BEFORE.search(before[-45:])
        name = nm.group(1) if nm and not NOT_NAME.search(nm.group(1)) else None
        local = email.split("@")[0]
        role = ("vendor" if VENDOR.search(local) or VENDOR.search(before[-110:]) else
                "director" if name or DIRECTOR.search(before[-110:]) else "organizer")
        out.append({"name": name, "email": email, "phone": _phone_near(text, m.start()), "role": role})
    rank = {"vendor": 0, "director": 1, "organizer": 2}
    return sorted(out, key=lambda c: rank[c["role"]])


def collector_domains(collector, events):
    """The organizer's own web domains: those of the scraper's listing pages, and the one most of its events live on."""
    found = set()
    for v in vars(type(collector)).values():
        for u in (v if isinstance(v, (list, tuple)) else [v]):
            for x in (u if isinstance(u, (list, tuple)) else [u]):
                if isinstance(x, str) and x.startswith("http"):
                    found.add(site_domain(x))
    hosts = [site_domain(ev.source_url) for ev in events if ev.source_url]
    top = max(set(hosts), key=hosts.count) if hosts else None
    if top and hosts.count(top) > len(hosts) / 2:          # most events, not one page on a production company's site
        found.add(top)
    return found - {""}


def enrich(collector, events):
    """Fill age_range and contacts from each event's own page when the scraper didn't. A page several events share (a
    listing) gives contacts but not ages, which would be another event's."""
    base = lambda u: (u or "").split("#")[0]
    shared = {}
    for ev in events:
        shared[base(ev.source_url)] = shared.get(base(ev.source_url), 0) + 1
    domains = collector_domains(collector, events)
    for ev in events:
        if not ev.source_url or (ev.age_range and ev.contacts): continue
        try:
            text = full_text(collector.get(ev.source_url))
        except Exception:  # noqa: BLE001
            continue
        if not ev.age_range and shared[base(ev.source_url)] <= 2:
            ev.age_range = grad_years(text, ev.start_date)
        if not ev.contacts:
            ev.contacts = page_contacts(text, domains)
    return events
