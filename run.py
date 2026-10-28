import os, sys, datetime, importlib, traceback
from collectors import common
from collectors.common import log

COLLECTORS = ["tourneymachine", "mayouthsoccer", "fairsandfestivals", "eventeny", "woodgoods_lacrosse"]   # add a module name here per new source
NEEDS_ANTHROPIC = set()   # collectors that call Claude; skipped when ANTHROPIC_API_KEY is missing

def main():
    only = [a for a in sys.argv[1:] if a]
    for k in ("INGEST_URL", "INGEST_KEY"):
        if not os.environ.get(k):
            log.error("Missing GitHub secret %s. Settings -> Secrets and variables -> Actions.", k); sys.exit(1)
    db = common.Supabase()
    failures = 0
    for name in (only or COLLECTORS):
        if name in NEEDS_ANTHROPIC and not os.environ.get("ANTHROPIC_API_KEY"):
            log.warning("%s skipped: ANTHROPIC_API_KEY not set", name); continue
        started = datetime.datetime.utcnow().isoformat() + "Z"
        rows, errors, n, note = [], 0, 0, ""
        try:
            mod = importlib.import_module(f"collectors.{name}")
            rows, errors = mod.collect()
            if os.environ.get("WG_ROWS_OUT"):                     # the weekly badge step reads them (weekly.yml)
                import json
                with open(os.environ["WG_ROWS_OUT"], "w") as f:
                    for r in rows: f.write(json.dumps(r, default=str) + "\n")
            n = db.upsert_candidates(rows)
            log.info("%s: %d rows, %d upserted, %d errors", name, len(rows), n, errors)
            if hasattr(mod, "collect_history"):                      # archive just-finished events into event_history
                hrows, herr = mod.collect_history()
                errors += herr
                for i in range(0, len(hrows), 150):
                    db._post({"source": f"{name}-history", "history": [{k: (v.isoformat() if isinstance(v, datetime.date) else v) for k, v in r.items()} for r in hrows[i:i+150]]})
                log.info("%s: %d finished events archived to history", name, len(hrows))
            if hasattr(mod, "contact_updates"):                      # contacts the organizer pages give, after their rows
                try:
                    cs = mod.contact_updates(rows)
                    for i in range(0, len(cs), 200):
                        db._post({"contacts": cs[i:i+200]})
                    if cs: log.info("%s: %d contacts sent", name, len(cs))
                except Exception as e:
                    log.warning("%s contacts step failed (rows are already saved): %s", name, e)
            if hasattr(mod, "weather_updates"):                      # last: a slow weather site can't hold back rows or history
                try:
                    w = mod.weather_updates(rows)
                    if w: log.info("%s: %d forecasts sent, %d saved", name, len(w), db.update_fields(name, w))
                except Exception as e:
                    log.warning("%s weather step failed (rows and history are already saved): %s", name, e)
        except Exception as e:
            failures += 1; note = traceback.format_exc()
            log.error("%s failed: %s", name, e)
        try:
            db.log_run(name, len(rows), n, errors + (1 if note else 0), started, note=note)
        except Exception as e:
            log.error("could not write source_runs for %s: %s", name, e)
    sys.exit(1 if failures else 0)

if __name__ == "__main__":
    main()
