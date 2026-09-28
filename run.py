import sys, datetime, importlib, traceback
from collectors import common
from collectors.common import log

COLLECTORS = ["tourneymachine", "mayouthsoccer"]   # add a module name here per new source

def main():
    only = [a for a in sys.argv[1:] if a]
    db = common.Supabase()
    for name in (only or COLLECTORS):
        started = datetime.datetime.utcnow().isoformat() + "Z"
        try:
            mod = importlib.import_module(f"collectors.{name}")
            rows, errors = mod.collect()
            n = db.upsert_candidates(rows)
            db.log_run(name, len(rows), n, errors, started)
            log.info("%s: %d rows, %d upserted, %d errors", name, len(rows), n, errors)
        except Exception as e:
            log.error("%s failed: %s", name, e)
            db.log_run(name, 0, 0, 1, started, note=traceback.format_exc())

if __name__ == "__main__":
    main()
