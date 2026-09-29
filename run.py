import os, sys, datetime, importlib, traceback
from collectors import common
from collectors.common import log

COLLECTORS = ["tourneymachine", "mayouthsoccer", "fairsandfestivals", "eventeny"]   # add a module name here per new source
NEEDS_ANTHROPIC = {"mayouthsoccer"}

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
            n = db.upsert_candidates(rows)
            log.info("%s: %d rows, %d upserted, %d errors", name, len(rows), n, errors)
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
