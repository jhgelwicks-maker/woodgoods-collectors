# Woodgoods event collectors

Weekly crawl of tournament sources into the Woodgoods Ops Supabase database. Store everything, filter in the database, enrich (Clay) only what passes the gate, dashboard reads live.

## Setup (15 minutes)
1. Push this folder to a GitHub repo.
2. Repo → Settings → Secrets and variables → Actions. Add `INGEST_URL` (the app's ingest edge function URL, shown by Lovable), `INGEST_KEY` (the secret Lovable generated), `ANTHROPIC_API_KEY`.
3. Schema is applied by Lovable (Lovable Cloud project). `sql/001_collector_schema.sql` is kept for reference only.
4. Actions → weekly-event-collection → Run workflow. First run takes ~5 minutes (2,500 Tourney Machine events with team counts).
5. Clay: paste your Clay webhook table URL into the app's Settings; the app posts gate-passed rows. (Reference: a Supabase webhook on `event_candidates` (insert, update) → your Clay webhook URL (or a Zapier catch hook that filters on `gate_passed = true and clay_sent_at is null`).

## Adding a source
Copy `collectors/mayouthsoccer.py` (HTML page → Claude → rows) or `collectors/tourneymachine.py` (JSON endpoint → rows). Return `(rows, errors)` with the keys documented in `collectors/common.py`. Add the module name to `COLLECTORS` in `run.py`.

## Filters
Edit rows in `filter_rules` (or the Lovable admin screen), then run `select regate_all();`. Nothing is deleted; `gate_passed` just flips.

## Schedule
Mondays 06:00 UTC. Change the cron in `.github/workflows/weekly.yml`. Run one source by hand with the `only` input.
