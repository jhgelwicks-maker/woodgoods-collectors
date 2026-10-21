"""Offline checks for collectors/mayouthsoccer.py. The page HTML below mirrors the real layout (tournaments as table
columns, blocks separated by logo rows) with made-up names and example.org emails. No network."""
from collectors import mayouthsoccer as ma

def _block(*cols):
    rows = [[f'<img src="logo{i}.png">' for i in range(len(cols))]]
    for k in range(len(cols[0])):
        rows.append([c[k] for c in cols])
    return "".join("<tr>" + "".join(f"<td>{x}</td>" for x in r) + "</tr>" for r in rows)

def _t(name, dates, town, email, ages, teams, url):
    return [name, dates, f"Location: {town}", f"Contact: {email}", f"Age Groups: {ages}", teams,
            f'<a href="{url}">Tournament Website</a>']

PAGE = (
    "<html><body><h2>Upcoming Tournaments</h2><table>"
    + _block(_t("Alpha Fall Classic", "October 9-12, 2026", "Natick, MA", "dir@example.org", "U8-U14", "Teams: S3, S4", "https://alpha.example.org/"),
             _t("Beta 4 v 4", "October 11th, 2026", "Holden, MA", "beta@example.org", "U8-U18", "Teams: RT", "https://beta.example.org/"))
    + "</table><table>"
    + _block(_t("Gamma Cup", "November 27th-29th, 2026", "Lancaster, MA", "gamma@example.org", "U12-U18", "Teams S1, S2", "https://gamma.example.org/"))
    + "</table><p>Recommended definitions of teams ...</p></body></html>"
)

GOOD = ("<html><head><title>Alpha Fall Classic 2026</title><meta name='description' content='Three days of youth soccer in Natick.'>"
        "</head><body>" + "Schedule, fields and registration details. " * 20 + "</body></html>")
ERROR_PAGE = "<html><head><title>404 Page Not Found</title></head><body>" + "Sorry, that page is gone. " * 20 + "</body></html>"

def test_parse_reads_each_column_as_a_tournament():
    items = ma.parse_tournaments(PAGE)
    assert [t["name"] for t in items] == ["Alpha Fall Classic", "Beta 4 v 4", "Gamma Cup"]
    alpha, beta, gamma = items
    assert alpha["location"] == "Natick, MA" and alpha["team_levels"] == "S3, S4" and alpha["website"] == "https://alpha.example.org/"
    assert beta["dates_text"] == "October 11th, 2026" and beta["contact"] == "beta@example.org"
    assert gamma["team_levels"] == "S1, S2"          # label without a colon still parsed

def test_collect_visits_websites_and_marks_bad_ones(monkeypatch):
    pages = {ma.URL: PAGE, "https://alpha.example.org/": GOOD, "https://beta.example.org/": None,
             "https://gamma.example.org/": ERROR_PAGE}
    monkeypatch.setattr(ma.common, "polite_get", lambda url, **k: pages.get(url))
    monkeypatch.setattr(ma.time, "sleep", lambda s: None)

    rows, errors = ma.collect()

    assert errors == 0
    by = {r["name"]: r for r in rows}
    alpha, beta, gamma = by["Alpha Fall Classic"], by["Beta 4 v 4"], by["Gamma Cup"]
    assert str(alpha["start_date"]) == "2026-10-09" and str(alpha["end_date"]) == "2026-10-12"
    assert str(beta["start_date"]) == "2026-10-11"                       # ordinal "11th" handled
    assert str(gamma["start_date"]) == "2026-11-27" and str(gamma["end_date"]) == "2026-11-29"
    assert alpha["city_states"] == ["Natick, MA"] and alpha["vendor_contact_email"] == "dir@example.org"
    assert alpha["official_url"] == "https://alpha.example.org/"
    assert alpha["raw"]["website_info"] == "ok" and alpha["raw"]["website_title"] == "Alpha Fall Classic 2026"
    assert alpha["raw"]["website_summary"] == "Three days of youth soccer in Natick."
    assert beta["raw"]["website_info"] == ma.NO_INFO                    # site did not load
    assert gamma["raw"]["website_info"] == ma.NO_INFO                   # loaded, but an error page

def test_empty_page_layout_is_an_error_not_silence(monkeypatch):
    monkeypatch.setattr(ma.common, "polite_get", lambda url, **k: "<html><body>Redesigned page</body></html>")
    assert ma.collect() == ([], 1)
