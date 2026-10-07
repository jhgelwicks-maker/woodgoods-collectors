"""Run the Tourney Machine backfill on this Mac instead of GitHub Actions, saving every row to a private
file outside the repo as well as sending it to the app.

    python -m collectors.backfill_local START END [--shard 1500] [--parallel 4]

Settings come from ~/WoodGoodsAI/.ingest.env (INGEST_URL=..., INGEST_KEY=...), which is never committed.
Rows go to ~/WoodGoodsAI/backup/backfill/R<start>-<end>.jsonl, one file per shard. A shard that finished
leaves a .done file next to it and is skipped on the next run, so an interrupted backfill resumes."""
import os, sys, argparse, subprocess, time
from concurrent.futures import ThreadPoolExecutor

HOME = os.path.expanduser("~/WoodGoodsAI")
ENV_FILE = os.path.join(HOME, ".ingest.env")
BACKUP_DIR = os.path.join(HOME, "backup", "backfill")
LOG_DIR = os.path.join(HOME, "backup", "logs")

def load_env():
    env = dict(os.environ)
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    env.setdefault("INGEST_URL", "https://opswg.lovable.app/api/public/ingest")
    if not env.get("INGEST_KEY"):
        sys.exit(f"INGEST_KEY missing: put it in {ENV_FILE}")
    env["BACKUP_DIR"] = BACKUP_DIR
    return env

def run_shard(a, b, env):
    done = os.path.join(BACKUP_DIR, f"R{a}-{b}.done")
    if os.path.exists(done):
        print(f"R{a}-{b}: already done, skipped", flush=True); return 0
    logf = os.path.join(LOG_DIR, f"R{a}-{b}.log")
    t0 = time.time()
    with open(logf, "a") as lf:
        rc = subprocess.call([sys.executable, "-m", "collectors.backfill_tourneymachine", str(a), str(b)],
                             env=env, stdout=lf, stderr=subprocess.STDOUT, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    last = ""
    try: last = [l for l in open(logf) if "backfill R" in l or "STOPPED" in l][-1].split("INFO ")[-1].split("ERROR ")[-1].strip()
    except IndexError: pass
    if rc == 0: open(done, "w").write(last + "\n")
    print(f"R{a}-{b}: {'ok' if rc == 0 else 'FAILED'} in {(time.time() - t0) / 60:.0f} min | {last}", flush=True)
    return rc

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start", type=int); ap.add_argument("end", type=int)
    ap.add_argument("--shard", type=int, default=1500); ap.add_argument("--parallel", type=int, default=4)
    o = ap.parse_args()
    env = load_env()
    os.makedirs(BACKUP_DIR, exist_ok=True); os.makedirs(LOG_DIR, exist_ok=True)
    shards = [(a, min(a + o.shard, o.end)) for a in range(o.start, o.end, o.shard)][::-1]   # newest first
    print(f"{len(shards)} shards, {o.parallel} at a time. Files: {BACKUP_DIR}", flush=True)
    with ThreadPoolExecutor(o.parallel) as ex:
        codes = list(ex.map(lambda s: run_shard(s[0], s[1], env), shards))
    failed = sum(1 for c in codes if c)
    print(f"finished: {len(shards) - failed} ok, {failed} failed", flush=True)
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
