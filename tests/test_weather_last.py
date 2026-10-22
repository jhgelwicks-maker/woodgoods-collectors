"""Offline checks for "send first, weather last": forecasts fail fast, are fetched after the main rows and the history
archive, and are sent as weather-only updates that cannot overwrite other fields. No network."""
import datetime
import pytest
import requests

from collectors import weather, common
from collectors import tourneymachine as tm

TODAY = datetime.date.today()

@pytest.fixture(autouse=True)
def fresh_forecast_state():
    weather._fc_state.update(fails=0, down=False, skipped=0)
    weather._fc_cache.clear()
    yield
    weather._fc_state.update(fails=0, down=False, skipped=0)
    weather._fc_cache.clear()

class _Resp:
    status_code = 200
    def json(self):
        return {"daily": {"time": ["d1"], "uv_index_max": [7.1], "precipitation_probability_max": [20],
                          "temperature_2m_max": [78], "sunshine_duration": [36000]}}

def test_forecast_stops_after_three_failures_in_a_row(monkeypatch):
    calls = []
    def timeout(*a, **k):
        calls.append(k.get("timeout")); raise requests.Timeout("read timed out")
    monkeypatch.setattr(requests, "get", timeout)
    for i in range(10):
        assert weather.forecast(39.0 + i / 100, -76.7, TODAY, TODAY) is None
    assert len(calls) == 3 and calls[0] == weather.FORECAST_TIMEOUT    # 3 real attempts, 10s each, not 10 x 30s
    assert weather._fc_state["down"] and weather._fc_state["skipped"] == 7

def test_forecast_success_resets_the_failure_count_and_is_reused(monkeypatch):
    seq = iter([requests.Timeout("blip"), _Resp(), _Resp()])
    calls = []
    def get(*a, **k):
        calls.append(1); x = next(seq)
        if isinstance(x, Exception): raise x
        return x
    monkeypatch.setattr(requests, "get", get)
    assert weather.forecast(39.0, -76.7, TODAY, TODAY) is None           # one blip
    fc = weather.forecast(39.0, -76.7, TODAY, TODAY)
    assert fc["uv_index_max"] == 7.1 and weather._fc_state["fails"] == 0
    assert weather.forecast(39.0, -76.7, TODAY, TODAY) == fc               # same venue + dates: no new call
    assert len(calls) == 2

def _row(n, start, lat="39.0"):
    return {"source": "tourneymachine", "source_event_id": f"h{n}", "name": f"Event {n}", "sport": "lacrosse",
            "start_date": start, "end_date": start + datetime.timedelta(days=1), "split_venue": True,
            "venues": [{"venue_name": "Park", "lat": lat, "long": "-76.7"}] if lat else None}

def test_weather_updates_only_near_events_with_coordinates_and_only_weather_fields(monkeypatch):
    monkeypatch.setattr(weather, "forecast", lambda *a, **k: {"uv_index_max": 8.0})
    rows = [_row(1, TODAY + datetime.timedelta(days=5)), _row(2, TODAY + datetime.timedelta(days=40)),
            _row(3, TODAY + datetime.timedelta(days=5), lat=None)]
    out = tm.weather_updates(rows)
    assert [r["source_event_id"] for r in out] == ["h1"]
    assert set(out[0]) == {"source", "source_event_id", "name", "sport", "start_date", "end_date", "weather_forecast", "uv_index"}
    assert out[0]["uv_index"] == 8.0

def test_update_fields_skips_normalize_defaults(monkeypatch):
    monkeypatch.setenv("INGEST_URL", "https://example.invalid/ingest"); monkeypatch.setenv("INGEST_KEY", "test")
    db, sent = common.Supabase(), []
    monkeypatch.setattr(db, "_post", lambda body: sent.append(body) or {"upserted": len(body["rows"])})
    n = db.update_fields("tourneymachine", [{"source": "tourneymachine", "source_event_id": "h1", "name": "E",
                                             "weather_forecast": {"uv_index_max": 8.0}, "uv_index": 8.0}])
    row = sent[0]["rows"][0]
    assert n == 1 and row["dedupe_key"] == "tourneymachine:h1"
    assert "split_venue" not in row and "is_league" not in row and "venue_count" not in row

def test_collect_no_longer_calls_the_forecast(monkeypatch):
    def boom(*a, **k): raise AssertionError("forecast must not run inside collect()")
    monkeypatch.setattr(weather, "forecast", boom)
    d = TODAY + datetime.timedelta(days=5)
    monkeypatch.setattr(tm, "search_all", lambda: {"h" + "1" * 31: {"date": d.strftime("%b %d, %Y"), "title": "Near", "icon": "lacrosse",
                                                                  "customer": "X", "cityStates": ["Crofton, MD"], "link": "x", "location": "Crofton, MD"}})
    monkeypatch.setattr(tm, "team_counts", lambda ids, workers=3: {i: {"team_count": 40, "complexes": [{"venue_name": "P", "lat": "39", "long": "-76"}]} for i in ids})
    rows, errors = tm.collect()
    assert len(rows) == 1 and "weather_forecast" not in rows[0]

def test_run_sends_rows_then_history_then_weather_and_survives_weather_failure(monkeypatch):
    import run
    monkeypatch.setenv("INGEST_URL", "https://example.invalid/ingest"); monkeypatch.setenv("INGEST_KEY", "test")
    order = []
    monkeypatch.setattr(common.Supabase, "_post", lambda self, body: order.append(
        "run" if "run" in body else "history" if "history" in body else "rows") or {"upserted": len(body.get("rows", []))})
    monkeypatch.setattr(tm, "collect", lambda: ([_row(1, TODAY + datetime.timedelta(days=5))], 0))
    monkeypatch.setattr(tm, "collect_history", lambda: ([{"source": "tourneymachine", "source_event_id": "old", "name": "Old"}], 0))
    def weather_down(rows): order.append("weather"); raise requests.Timeout("open-meteo down")
    monkeypatch.setattr(tm, "weather_updates", weather_down)
    monkeypatch.setattr(run.sys, "argv", ["run.py", "tourneymachine"])
    with pytest.raises(SystemExit) as done:
        run.main()
    assert done.value.code == 0                                   # weather failing is not a collector failure
    assert order == ["rows", "history", "weather", "run"]
