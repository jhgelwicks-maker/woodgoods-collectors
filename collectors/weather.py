"""Open-Meteo (no key). Two uses:
  actual(lat, lon, start, end)      -> what the weather was on those event days (for history / revenue normalization)
  climatology(lat, lon, start, end) -> 20-year, ±7-day base rates for an upcoming event (rain probability, highs, sunshine)
Reanalysis (ERA5) is a ~9-25 km grid: fine for "this weekend in this county", not for a single field at 2pm."""
import datetime, statistics, time
from .common import log

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
VARS = "temperature_2m_max,temperature_2m_min,precipitation_sum,rain_sum,sunshine_duration,wind_speed_10m_max,weather_code,shortwave_radiation_sum"
UV_PER_MJ = 0.33   # rough: daily shortwave MJ/m2 -> peak UV index (19 MJ ~ UV 6). Labelled an estimate; the forecast UV replaces it inside 16 days.
RAIN_MM = 2.5    # a day with more than this is a "rain day" for a vendor tent

def _fetch(lat, lon, start, end):
    import requests
    for i in range(3):
        try:
            r = requests.get(ARCHIVE, params={"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
                                               "daily": VARS, "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "America/New_York"}, timeout=40)
            if r.status_code == 200: return r.json().get("daily") or {}
            time.sleep(2 * (i + 1))
        except Exception as e:
            log.warning("open-meteo failed: %s", e); time.sleep(2 * (i + 1))
    return {}

def actual(lat, lon, start, end):
    """Weather on the event days themselves. Returns None if the dates are in the future or data is missing."""
    if not (lat and lon and start): return None
    end = end or start
    if end >= datetime.date.today() - datetime.timedelta(days=5): return None   # archive lags ~5 days
    d = _fetch(lat, lon, start, end)
    if not d or not d.get("time"): return None
    n = len(d["time"]); rain = d.get("rain_sum") or d.get("precipitation_sum") or []
    sun_h = [s / 3600 for s in (d.get("sunshine_duration") or []) if s is not None]
    rad = [x for x in (d.get("shortwave_radiation_sum") or []) if x is not None]
    return {"days": n,
            "radiation_mj_avg": round(statistics.mean(rad), 1) if rad else None,
            "uv_index_est": round(statistics.mean(rad) * UV_PER_MJ, 1) if rad else None,
            "rain_days": sum(1 for x in rain if (x or 0) > RAIN_MM),
            "rain_total_mm": round(sum(x or 0 for x in rain), 1),
            "high_f": round(max(x for x in d["temperature_2m_max"] if x is not None), 0) if d.get("temperature_2m_max") else None,
            "avg_high_f": round(statistics.mean(x for x in d["temperature_2m_max"] if x is not None), 0) if d.get("temperature_2m_max") else None,
            "sun_hours_avg": round(statistics.mean(sun_h), 1) if sun_h else None,
            "wind_max_mph": round(max(x for x in d["wind_speed_10m_max"] if x is not None), 0) if d.get("wind_speed_10m_max") else None,
            "summary": "rain" if any((x or 0) > RAIN_MM for x in rain) else ("sunny" if sun_h and statistics.mean(sun_h) >= 7 else "dry")}

_SERIES = {}   # (lat2, lon2) -> {"time":[...], ...} 20-year daily series, fetched once per venue per run

def _series(lat, lon, years=20):
    key = (round(lat, 2), round(lon, 2))
    if key not in _SERIES:
        end = datetime.date.today() - datetime.timedelta(days=6)
        start = datetime.date(end.year - years, 1, 1)
        _SERIES[key] = _fetch(lat, lon, start, end) or {}
        time.sleep(0.2)
    return _SERIES[key]

def climatology(lat, lon, start, end=None, years=20, window_days=7):
    """Base rates for the same time of year: P(rain day), expected high, expected sunshine.
    One archive call per venue (20 years daily), then every event's ±window is sliced in memory."""
    if not (lat and lon and start): return None
    end = end or start
    span = (end - start).days + 1
    d = _series(lat, lon, years)
    if not d.get("time"): return None
    idx = {datetime.date.fromisoformat(t): i for i, t in enumerate(d["time"])}
    rain = d.get("rain_sum") or d.get("precipitation_sum") or []
    highs = d.get("temperature_2m_max") or []; suns = d.get("sunshine_duration") or []; rads = d.get("shortwave_radiation_sum") or []
    rain_days = total = 0; H = []; S = []; R = []
    for y in range(datetime.date.today().year - years, datetime.date.today().year):
        try: s0 = start.replace(year=y) - datetime.timedelta(days=window_days)
        except ValueError: continue
        for k in range(span + 2 * window_days):
            i = idx.get(s0 + datetime.timedelta(days=k))
            if i is None: continue
            total += 1
            if (rain[i] or 0) > RAIN_MM: rain_days += 1
            if i < len(highs) and highs[i] is not None: H.append(highs[i])
            if i < len(suns) and suns[i] is not None: S.append(suns[i] / 3600)
            if i < len(rads) and rads[i] is not None: R.append(rads[i])
    if not total: return None
    p_rain = rain_days / total
    exp_high = statistics.mean(H) if H else None
    exp_sun = statistics.mean(S) if S else None
    exp_rad = statistics.mean(R) if R else None
    uv_est = round(exp_rad * UV_PER_MJ, 1) if exp_rad is not None else None
    sun_score = None
    if exp_high is not None and exp_sun is not None:
        t = max(0, min(1, (exp_high - 45) / 40))         # 45F -> 0, 85F -> 1
        s_ = max(0, min(1, (exp_sun - 3) / 7))           # 3h -> 0, 10h -> 1
        u = max(0, min(1, ((uv_est or 0) - 2) / 7))       # UV 2 -> 0, UV 9 -> 1
        sun_score = round(100 * (0.45 * t + 0.30 * s_ + 0.25 * u) * (1 - 0.5 * p_rain))
    return {"years": years, "window_days": window_days, "sample_days": total,
            "p_rain_day": round(p_rain, 2),
            "p_rain_event": round(1 - (1 - p_rain) ** span, 2),
            "expected_high_f": round(exp_high, 0) if exp_high is not None else None,
            "expected_sun_hours": round(exp_sun, 1) if exp_sun is not None else None,
            "expected_radiation_mj": round(exp_rad, 1) if exp_rad is not None else None,
            "uv_index_est": uv_est,
            "sun_score": sun_score,
            "wet_season": p_rain >= 0.45}


FORECAST = "https://api.open-meteo.com/v1/forecast"
def forecast(lat, lon, start, end=None):
    """Real forecast inside 16 days: daily UV index max, precip probability, high. None if out of range."""
    import requests
    end = end or start
    if not (lat and lon and start) or start > datetime.date.today() + datetime.timedelta(days=15): return None
    try:
        r = requests.get(FORECAST, params={"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
                                            "daily": "uv_index_max,precipitation_probability_max,temperature_2m_max,sunshine_duration",
                                            "temperature_unit": "fahrenheit", "timezone": "America/New_York"}, timeout=30)
        d = r.json().get("daily") if r.status_code == 200 else None
        if not d or not d.get("time"): return None
        uv = [x for x in d.get("uv_index_max") or [] if x is not None]
        return {"uv_index_max": max(uv) if uv else None,
                "precip_probability_max": max([x for x in d.get("precipitation_probability_max") or [] if x is not None] or [None]),
                "high_f": max([x for x in d.get("temperature_2m_max") or [] if x is not None] or [None]),
                "sun_hours_avg": round(statistics.mean([x / 3600 for x in d.get("sunshine_duration") or [] if x is not None]), 1) if d.get("sunshine_duration") else None}
    except Exception as e:
        log.warning("forecast failed: %s", e); return None
