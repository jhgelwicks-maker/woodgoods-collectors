"""Two-minute check of the ingest contract: sends one payload of each kind (rows, run, history,
contacts) and prints what the endpoint answered. Every test row uses source "smoke-test" and a name
starting with "SMOKE TEST" so it is easy to find and delete in the app.

    python -m collectors.smoke_ingest"""
import sys, json, datetime, requests
from . import common
from .common import log

def main():
    db = common.Supabase()
    today = datetime.date.today()
    now = datetime.datetime.utcnow().isoformat() + "Z"
    cand = common.normalize({"source": "smoke-test", "source_event_id": "candidate-1", "name": "SMOKE TEST candidate (delete me)",
                             "sport": "lacrosse", "start_date": today + datetime.timedelta(days=60),
                             "end_date": today + datetime.timedelta(days=61), "city_states": ["Norfolk, MA"], "team_count": 1})
    cand["zz_unknown_key"] = "should be ignored, not rejected"
    hist = common.normalize({"source": "smoke-test", "source_event_id": "history-1", "name": "SMOKE TEST history (delete me)",
                             "sport": "lacrosse", "start_date": today - datetime.timedelta(days=30),
                             "end_date": today - datetime.timedelta(days=29), "city_states": ["Norfolk, MA"], "team_count": 1})
    payloads = [
        ("rows", {"source": "smoke-test", "rows": [cand]}),
        ("run", {"source": "smoke-test", "run": {"started_at": now, "finished_at": now, "rows_seen": 1, "rows_upserted": 1,
                                                 "errors": 0, "note": "SMOKE TEST run (delete me)"}}),
        ("history", {"source": "smoke-test", "history": [hist]}),
        ("contacts", {"contacts": [{"dedupe_key": cand["dedupe_key"], "name": "SMOKE TEST contact (delete me)", "role": "test",
                                    "email": "smoke-test@example.com", "phone": None, "source_url": None,
                                    "confidence": 0, "enriched_via": "smoke-test"}]}),
    ]
    failed = []
    for kind, body in payloads:
        res = requests.post(db.url, headers=db.h, data=json.dumps(body, default=str), timeout=60)
        ok = res.status_code < 300
        log.info("%-8s -> %s %s %s", kind, res.status_code, "OK" if ok else "REJECTED", res.text[:300])
        if not ok: failed.append(kind)
    if failed:
        log.error("ingest contract FAILED for: %s", ", ".join(failed)); sys.exit(1)
    log.info("ingest contract OK: all four payload kinds accepted")

if __name__ == "__main__":
    main()
