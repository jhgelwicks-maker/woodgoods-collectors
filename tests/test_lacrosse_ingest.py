"""Offline checks for the woodgoods_lacrosse crash (2026-10-12/19/21: 'can't multiply sequence by non-int of type
float') and the mixed-source batching that would have silently dropped most rows. No network."""
import datetime
import pytest
from collectors import common

def _row(source, n, team_count=None):
    return {"source": source, "source_event_id": f"ev-{n}", "name": f"Event {n}", "sport": "lacrosse",
            "start_date": datetime.date(2026, 11, 7), "end_date": datetime.date(2026, 11, 8),
            "city_states": ["Crofton, MD"], "team_count": team_count, "raw": {"gender": "boys"}}

@pytest.mark.parametrize("value, want", [
    ("97 college coaches", None), ("48", 48), (" 1,024 ", 1024), (48, 48), (0, 0), (48.0, 48),
    (None, None), ("", None), (True, None), (-3, None), ("about 40", None),
])
def test_team_int(value, want):
    assert common.team_int(value) == want

def test_text_team_count_no_longer_crashes_normalize():
    r = common.normalize(_row("wg-nxt", 1, "97 college coaches"))
    assert r["team_count"] is None and r.get("expected_attendance") is None

def test_numeric_text_team_count_still_estimates_attendance():
    r = common.normalize(_row("wg-apex", 2, "40"))
    assert r["team_count"] == 40 and r["expected_attendance"] > 0

def test_one_batch_per_source_and_saved_count_from_the_reply(monkeypatch):
    monkeypatch.setenv("INGEST_URL", "https://example.invalid/ingest")
    monkeypatch.setenv("INGEST_KEY", "test")
    db = common.Supabase()
    sent = []
    def fake_post(body):
        sent.append(body)
        rows = body["rows"]
        ok = [r for r in rows if r["source"] == body["source"]]
        return {"ok": True, "received": len(rows), "upserted": len(ok), "rejected": len(rows) - len(ok)}
    monkeypatch.setattr(db, "_post", fake_post)

    rows = [_row("wg-nxt", 1), _row("wg-apex", 2), _row("wg-nxt", 3), _row("wg-mlt", 4, "97 college coaches")]
    n = db.upsert_candidates(rows)

    assert sorted(b["source"] for b in sent) == ["wg-apex", "wg-mlt", "wg-nxt"]
    assert all(r["source"] == b["source"] for b in sent for r in b["rows"])
    assert n == 4
