"""Resend backfill rows saved on disk to the ingest endpoint, e.g. after the app rejected a batch.
Safe to run more than once: the app upserts on dedupe_key.

    python -m collectors.replay_history ~/WoodGoodsAI/backup/backfill/R184000-185500.jsonl [more files...]"""
import sys, json
from . import common
from .common import log

BATCH = 150

def main():
    db = common.Supabase()
    total = 0
    for path in sys.argv[1:]:
        with open(path) as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for i in range(0, len(rows), BATCH):
            db._post({"source": "tourneymachine-history", "history": rows[i:i+BATCH]})
        log.info("replayed %s: %d rows", path, len(rows))
        total += len(rows)
    log.info("replay done: %d rows from %d files", total, len(sys.argv[1:]))

if __name__ == "__main__":
    main()
